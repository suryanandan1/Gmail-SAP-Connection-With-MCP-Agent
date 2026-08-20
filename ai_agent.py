from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any

from dotenv import load_dotenv
from mistralai import Mistral

from mcp_client import MCPClient, MCPConnectionError, MCPToolError
from prompts import build_system_prompt

load_dotenv()

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "").strip()
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-large-latest").strip()
MAX_AGENT_STEPS = int(os.getenv("MISTRAL_MAX_STEPS", "12"))

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "gmail_search_messages",
            "description": "Search Gmail messages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_read_message",
            "description": "Read a Gmail message.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                },
                "required": ["message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_list_messages",
            "description": "List Gmail messages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {"type": "integer", "default": 10},
                    "label": {"type": "string", "default": "INBOX"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sap_test_connection",
            "description": "Check SAP connectivity.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sap_list_business_partners",
            "description": "List SAP business partners.",
            "parameters": {
                "type": "object",
                "properties": {
                    "top": {"type": "integer", "default": 10}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sap_search_business_partners",
            "description": "Search SAP business partners.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "top": {"type": "integer", "default": 10},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sap_get_business_partner",
            "description": "Get SAP business partner details.",
            "parameters": {
                "type": "object",
                "properties": {
                    "business_partner_id": {"type": "string"}
                },
                "required": ["business_partner_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sap_list_sales_orders",
            "description": "List SAP sales orders.",
            "parameters": {
                "type": "object",
                "properties": {
                    "top": {"type": "integer", "default": 10}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sap_get_sales_order",
            "description": "Get SAP sales order details.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sales_order_id": {"type": "string"}
                },
                "required": ["sales_order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sap_list_invoices",
            "description": "List SAP invoices.",
            "parameters": {
                "type": "object",
                "properties": {
                    "top": {"type": "integer", "default": 10}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sap_get_invoice",
            "description": "Get SAP invoice details.",
            "parameters": {
                "type": "object",
                "properties": {
                    "billing_document_id": {"type": "string"}
                },
                "required": ["billing_document_id"],
            },
        },
    },
]


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
        self._tool_impl = {
            "gmail_search_messages": lambda args:
                self._mcp_client.gmail_search_messages(
                    query=args["query"],
                    max_results=int(args.get("max_results", 10)),
                ),

            "gmail_read_message": lambda args:
                self._mcp_client.gmail_read_message(
                    message_id=args["message_id"]
                ),

            "gmail_list_messages": lambda args:
                self._mcp_client.gmail_list_messages(
                    max_results=int(args.get("max_results", 10)),
                    label=args.get("label", "INBOX"),
                ),

            "sap_test_connection": lambda args:
                self._mcp_client.sap_test_connection(),

            "sap_list_business_partners": lambda args:
                self._mcp_client.sap_list_business_partners(
                    top=int(args.get("top", 10))
                ),

            "sap_search_business_partners": lambda args:
                self._mcp_client.sap_search_business_partners(
                    name=args["name"],
                    top=int(args.get("top", 10)),
                ),

            "sap_get_business_partner": lambda args:
                self._mcp_client.sap_get_business_partner(
                    business_partner_id=args["business_partner_id"]
                ),

            "sap_list_sales_orders": lambda args:
                self._mcp_client.sap_list_sales_orders(
                    top=int(args.get("top", 10))
                ),

            "sap_get_sales_order": lambda args:
                self._mcp_client.sap_get_sales_order(
                    sales_order_id=args["sales_order_id"]
                ),

            "sap_list_invoices": lambda args:
                self._mcp_client.sap_list_invoices(
                    top=int(args.get("top", 10))
                ),

            "sap_get_invoice": lambda args:
                self._mcp_client.sap_get_invoice(
                    billing_document_id=args["billing_document_id"]
                ),
        }

    def _run_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        impl = self._tool_impl.get(name)
        if impl is None:
            return {"error": f"Unknown tool '{name}'"}
        try:
            return impl(arguments)
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
                tools=TOOLS,
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