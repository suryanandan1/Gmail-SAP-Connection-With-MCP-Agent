from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any

from dotenv import load_dotenv
from mistralai import Mistral

from mcp_client import MCPClient, MCPConnectionError, MCPToolError, READ_ONLY_PREFIXES
from prompts import build_system_prompt

load_dotenv()

logger = logging.getLogger("ai_agent")

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "").strip()
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-large-latest").strip()
MAX_AGENT_STEPS = int(os.getenv("MISTRAL_MAX_STEPS", "12"))

# The read-only/mutating boundary is enforced once, in MCPClient
# (discover_safe_tools() / READ_ONLY_PREFIXES) - not duplicated here. This
# agent only ever asks the client for the pre-filtered "safe" tool set, so
# there is exactly one place in the codebase that decides what counts as
# read-only. See readme.md "Safety notes".


def _mcp_tool_to_mistral_function(tool: dict[str, Any]) -> dict[str, Any]:
    """
    MCP discovery metadata -> Mistral tool-calling schema.

    MCP:      {"name": ..., "description": ..., "inputSchema": {...}}
    Mistral:  {"type": "function", "function": {"name", "description", "parameters"}}

    inputSchema is already a JSON-schema dict (mcp.types.Tool.inputSchema),
    so it maps directly onto Mistral's "parameters" with no reshaping.
    """
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description") or "",
            "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
        },
    }


# Static fallback used only if live discovery fails (e.g. MCP server
# unreachable when EmailAssistant is constructed). Kept in the exact shape
# MCPClient.discover_tools() produces - {"name", "description",
# "inputSchema"} - and run through the same _mcp_tool_to_mistral_function()
# converter as live-discovered tools, so this is the *only* place a
# hand-maintained list still exists, and only as a degraded-mode fallback.
_FALLBACK_TOOLS_METADATA: list[dict[str, Any]] = [
    {
        "name": "gmail_search_messages",
        "description": "Search Gmail messages.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "gmail_read_message",
        "description": "Read a Gmail message.",
        "inputSchema": {
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
    },
    {
        "name": "gmail_list_messages",
        "description": "List Gmail messages.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "default": 10},
                "label": {"type": "string", "default": "INBOX"},
            },
        },
    },
    {
        "name": "sap_test_connection",
        "description": "Check SAP connectivity.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sap_list_business_partners",
        "description": "List SAP business partners.",
        "inputSchema": {
            "type": "object",
            "properties": {"top": {"type": "integer", "default": 10}},
        },
    },
    {
        "name": "sap_search_business_partners",
        "description": "Search SAP business partners.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "top": {"type": "integer", "default": 10},
            },
            "required": ["name"],
        },
    },
    {
        "name": "sap_get_business_partner",
        "description": "Get SAP business partner details.",
        "inputSchema": {
            "type": "object",
            "properties": {"business_partner_id": {"type": "string"}},
            "required": ["business_partner_id"],
        },
    },
    {
        "name": "sap_list_sales_orders",
        "description": "List SAP sales orders.",
        "inputSchema": {
            "type": "object",
            "properties": {"top": {"type": "integer", "default": 10}},
        },
    },
    {
        "name": "sap_get_sales_order",
        "description": "Get SAP sales order details.",
        "inputSchema": {
            "type": "object",
            "properties": {"sales_order_id": {"type": "string"}},
            "required": ["sales_order_id"],
        },
    },
    {
        "name": "sap_list_invoices",
        "description": "List SAP invoices.",
        "inputSchema": {
            "type": "object",
            "properties": {"top": {"type": "integer", "default": 10}},
        },
    },
    {
        "name": "sap_get_invoice",
        "description": "Get SAP invoice details.",
        "inputSchema": {
            "type": "object",
            "properties": {"billing_document_id": {"type": "string"}},
            "required": ["billing_document_id"],
        },
    },
]

# Guards against the fallback list silently drifting out of sync with the
# authoritative safety boundary in mcp_client.READ_ONLY_PREFIXES - fails
# loudly at import time rather than quietly serving an unsafe fallback tool.
assert all(
    t["name"].startswith(READ_ONLY_PREFIXES) for t in _FALLBACK_TOOLS_METADATA
), "_FALLBACK_TOOLS_METADATA contains a tool name outside READ_ONLY_PREFIXES"


class AgentError(RuntimeError):
    """Raised when the AI agent cannot complete a request."""


class EmailAssistant:
    """
    Agentic email assistant.

    Streamlit UI -> EmailAssistant -> Mistral (decides tool calls)
                                    -> MCPClient -> MCP server -> Gmail API

    The LLM never touches Gmail directly - every action it takes goes through
    the existing MCPClient tool wrappers, exactly like the rest of the app.
    """

    def __init__(self, mcp_client: MCPClient) -> None:
        if not MISTRAL_API_KEY:
            raise AgentError("MISTRAL_API_KEY is missing from the .env file.")
        self._mcp_client = mcp_client
        self._client = Mistral(api_key=MISTRAL_API_KEY)
        # Discovered once per EmailAssistant instance (i.e. once per
        # Streamlit process, since mcp_client/EmailAssistant are cached
        # resources) rather than per ask() call, so a slow/unreachable
        # server doesn't add latency to every question. Call
        # refresh_tools() explicitly if the server's tool set changes
        # while the app is running.
        self._tools: list[dict[str, Any]] = []
        self._allowed_tool_names: set[str] = set()
        self.refresh_tools()

    def refresh_tools(self) -> None:
        """
        Rebuild the Mistral tool schema from live, safety-filtered MCP
        server metadata.

        Uses ``discover_safe_tools()`` (not ``discover_tools()``) so the
        read-only allowlist in ``mcp_client.READ_ONLY_PREFIXES`` is applied
        before anything reaches this agent - mutating tools are excluded at
        the source, not re-filtered here. Falls back to the static
        read-only tool list if discovery fails (server briefly unreachable,
        etc.) rather than leaving the agent with zero tools; the fallback
        list is itself read-only, so no separate filtering is needed for it.
        """
        try:
            safe_tools = self._mcp_client.discover_safe_tools()
        except (MCPConnectionError, MCPToolError) as exc:
            logger.warning(
                "MCP tool discovery failed (%s); falling back to the static tool list.",
                exc,
            )
            safe_tools = _FALLBACK_TOOLS_METADATA

        self._tools = [_mcp_tool_to_mistral_function(t) for t in safe_tools]
        self._allowed_tool_names = {t["name"] for t in safe_tools}
        logger.info(
            "EmailAssistant tool set ready: %d tool(s) -> %s",
            len(self._allowed_tool_names),
            ", ".join(sorted(self._allowed_tool_names)),
        )

    def _run_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in self._allowed_tool_names:
            # Backstop, not the primary control: discover_safe_tools()
            # already excludes mutating tools before self._tools is built,
            # so the model is never even told this tool exists. This check
            # is what actually prevents execution if the model hallucinates
            # a call to something it was never offered.
            return {"error": f"Tool '{name}' is not available to this agent."}
        try:
            return self._mcp_client.call_tool(name, arguments)
        except (MCPToolError, MCPConnectionError) as exc:
            return {"error": str(exc)}
        except Exception as exc:  # keep the agent loop alive on unexpected errors
            return {"error": f"{type(exc).__name__}: {exc}"}

    def ask(self, user_question: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
        """
        Run the agent loop for one user question.

        Returns {"answer": str, "steps": list[dict]} - "steps" is a trace of
        which tools were called, useful for an optional debug view in the UI.
        """
        now = datetime.now()
        tomorrow = now + timedelta(days=1)
        system_prompt = build_system_prompt(today=now, tomorrow=tomorrow)

        messages: list[Any] = [{"role": "system", "content": system_prompt}]
        for turn in history or []:
            messages.append(turn)
        messages.append({"role": "user", "content": user_question})

        trace: list[dict[str, Any]] = []

        for _ in range(MAX_AGENT_STEPS):
            response = self._client.chat.complete(
                model=MISTRAL_MODEL,
                messages=messages,
                tools=self._tools,
                tool_choice="auto",
            )
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None)

            if not tool_calls:
                return {"answer": message.content, "steps": trace}

            messages.append(message)

            for call in tool_calls:
                try:
                    arguments = json.loads(call.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    arguments = {}
                result = self._run_tool(call.function.name, arguments)
                trace.append({"tool": call.function.name, "arguments": arguments, "result": result})
                messages.append(
                    {
                        "role": "tool",
                        "name": call.function.name,
                        "tool_call_id": call.id,
                        "content": json.dumps(result, default=str),
                    }
                )

        return {
            "answer": (
                "I wasn't able to finish checking your inbox within the allotted "
                "number of steps. Try asking a narrower question, e.g. a specific date range."
            ),
            "steps": trace,
        }