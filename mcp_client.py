from __future__ import annotations
import asyncio
import json
import os
import threading
from contextlib import AsyncExitStack
from typing import Any

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, TextContent

load_dotenv()

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8000/mcp").strip()
MCP_CALL_TIMEOUT_SECONDS = float(os.getenv("MCP_CALL_TIMEOUT_SECONDS", "60"))
MCP_CONNECT_TIMEOUT_SECONDS = float(os.getenv("MCP_CONNECT_TIMEOUT_SECONDS", "20"))


class MCPToolError(RuntimeError):
    """Raised when the MCP server reports a tool-execution error."""


class MCPConnectionError(RuntimeError):
    """Raised when the client cannot reach or initialize the MCP server."""


class _BackgroundLoop:
    """
    Owns a single asyncio event loop running forever in a dedicated thread.

    Why this exists
    ----------------
    Streamlit re-executes the whole script top-to-bottom on every user
    interaction. A naive integration calls ``asyncio.run(...)`` per
    interaction, which creates a *brand new* event loop each time. If an
    MCP `ClientSession` (or its underlying anyio task group / cancel
    scopes) is opened on loop A during one rerun and something tries to
    close it on loop B during the next rerun, anyio raises
    "Attempted to exit cancel scope in a different task than it was
    entered in". The same problem shows up as "attached to a different
    loop" errors.

    The fix is to never let the session's lifetime cross event loops or
    threads. This class starts exactly one event loop, in exactly one
    background thread, once per process (via ``st.cache_resource`` on the
    Streamlit side). Every coroutine that touches the MCP session -
    connecting, calling tools, closing - is submitted to that same loop
    with ``run_coroutine_threadsafe``. The loop, and therefore every
    cancel scope opened inside it, lives for the lifetime of the
    Streamlit process, not for the lifetime of a single script rerun.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run,name="mcp-client-loop",daemon=True,)
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def run(self, coro: Any, timeout: float | None = None) -> Any:
        """Schedule ``coro`` on the background loop and block for the result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)


def _unwrap(result: CallToolResult) -> Any:
    """Turn a CallToolResult back into the plain dict/list a tool returned."""
    if result.isError:
        message = "MCP tool reported an error."
        for block in result.content:
            if isinstance(block, TextContent):
                message = block.text
                break
        raise MCPToolError(message)

    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
            return structured["result"]
        return structured

    text_blocks = [block.text for block in result.content if isinstance(block, TextContent)]
    if not text_blocks:
        return None

    if len(text_blocks) == 1:
        try:
            return json.loads(text_blocks[0])
        except (json.JSONDecodeError, TypeError):
            return text_blocks[0]

    parsed_items = []
    for block in text_blocks:
        try:
            parsed_items.append(json.loads(block))
        except (json.JSONDecodeError, TypeError):
            return "\n".join(text_blocks)
    return parsed_items


class MCPClient:
    """
    Streamlit-safe MCP client over the Streamable HTTP transport.

    One instance is created per process (see ``get_mcp_client`` in
    streamlit_app.py, wrapped in ``st.cache_resource``). Connecting and every
    subsequent tool call run on the same background event loop, so the
    session's async context managers are always entered and exited from the
    same task - see ``_BackgroundLoop`` for why that matters.
    """

    def __init__(self, server_url: str = MCP_SERVER_URL) -> None:
        self.server_url = server_url
        self._loop = _BackgroundLoop()
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
     
        self._connect_lock = asyncio.Lock()

    #   lifecycle connection

    async def _connect(self) -> None:
        exit_stack = AsyncExitStack()
        try:
            read_stream, write_stream, _get_session_id = await exit_stack.enter_async_context(
                streamablehttp_client(self.server_url)
            )
            session = await exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
        except Exception as exc:  
            await exit_stack.aclose()
            raise MCPConnectionError(
                f"Could not connect to the MCP server at {self.server_url}: {exc}"
            ) from exc

        self._exit_stack = exit_stack
        self._session = session

    async def _ensure_connected(self) -> ClientSession:
        if self._session is not None:
            return self._session
        async with self._connect_lock:
            if self._session is None:
                await self._connect()
        assert self._session is not None
        return self._session

    async def _disconnect(self) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
        self._exit_stack = None
        self._session = None

    def connect(self) -> None:
        """Establish the MCP session. Safe to call once at app startup."""
        self._loop.run(self._ensure_connected(), timeout=MCP_CONNECT_TIMEOUT_SECONDS)

    def close(self) -> None:
        try:
            self._loop.run(self._disconnect(), timeout=MCP_CONNECT_TIMEOUT_SECONDS)
        finally:
            self._loop.stop()

    #  tool calling 

    async def _call_tool_async(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        session = await self._ensure_connected()
        try:
            result = await session.call_tool(tool_name, arguments)
        except Exception as exc:
            await self._disconnect()
            raise MCPConnectionError(
                f"Lost connection to the MCP server while calling '{tool_name}': {exc}"
            ) from exc
        return _unwrap(result)

    def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        """
        Synchronous entry point used by Streamlit code.

        Runs the async tool call on the persistent background loop and blocks
        until it completes, which is exactly what a synchronous Streamlit
        script body needs.
        """
        return self._loop.run(
            self._call_tool_async(tool_name, arguments or {}),
            timeout=MCP_CALL_TIMEOUT_SECONDS,
        )

    async def list_tools(self) -> list[str]:
        session = await self._ensure_connected()
        response = await session.list_tools()
        return [tool.name for tool in response.tools]

    def list_tool_names(self) -> list[str]:
        return self._loop.run(self.list_tools(), timeout=MCP_CALL_TIMEOUT_SECONDS)

    #  Gmail tool wrappers 

    def gmail_auth_status(self) -> dict[str, Any]:
        return self.call_tool("gmail_auth_status", {})

    def gmail_list_messages(self, max_results: int = 10, label: str = "INBOX") -> list[dict[str, Any]]:
        return self.call_tool(
            "gmail_list_messages",
            {"max_results": max_results, "label": label},
        )

    def gmail_search_messages(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        return self.call_tool(
            "gmail_search_messages",
            {"query": query, "max_results": max_results},
        )

    def gmail_read_message(self, message_id: str) -> dict[str, Any]:
        return self.call_tool("gmail_read_message", {"message_id": message_id})

    def gmail_send_email(
        self, to: str, subject: str, body: str, cc: str = "", bcc: str = ""
    ) -> dict[str, Any]:
        return self.call_tool(
            "gmail_send_email",
            {"to": to, "subject": subject, "body": body, "cc": cc, "bcc": bcc},
        )

    def gmail_create_draft(
        self, to: str, subject: str, body: str, cc: str = "", bcc: str = ""
    ) -> dict[str, Any]:
        return self.call_tool(
            "gmail_create_draft",
            {"to": to, "subject": subject, "body": body, "cc": cc, "bcc": bcc},
        )

    def gmail_delete_email(self, message_id: str) -> dict[str, Any]:
        return self.call_tool("gmail_delete_email", {"message_id": message_id})

    def gmail_list_labels(self) -> list[dict[str, Any]]:
        return self.call_tool("gmail_list_labels", {})

    def gmail_mark_read(self, message_id: str) -> dict[str, Any]:
        return self.call_tool("gmail_mark_read", {"message_id": message_id})

    def gmail_mark_unread(self, message_id: str) -> dict[str, Any]:
        return self.call_tool("gmail_mark_unread", {"message_id": message_id})

    def gmail_get_attachment(self, message_id: str, attachment_id: str) -> dict[str, Any]:
        return self.call_tool(
            "gmail_get_attachment",
            {"message_id": message_id, "attachment_id": attachment_id},
        )

    # SAP tool wrappers 

    def sap_configuration_status(self) -> dict[str, Any]:
        return self.call_tool("sap_configuration_status", {})

    def sap_test_connection(self) -> dict[str, Any]:
        return self.call_tool("sap_test_connection", {})

    def sap_list_business_partners(self, top: int = 10) -> dict[str, Any]:
        return self.call_tool("sap_list_business_partners", {"top": top})

    def sap_search_business_partners(self, name: str, top: int = 10) -> dict[str, Any]:
        return self.call_tool("sap_search_business_partners", {"name": name, "top": top})

    def sap_get_business_partner(self, business_partner_id: str) -> dict[str, Any]:
        return self.call_tool(
            "sap_get_business_partner", {"business_partner_id": business_partner_id}
        )

    def sap_list_email_addresses(self, top: int = 10) -> dict[str, Any]:
        return self.call_tool("sap_list_email_addresses", {"top": top})

    def sap_create_business_partner(
        self,
        category: str,
        organization_name: str = "",
        first_name: str = "",
        last_name: str = "",
        search_term: str = "",
        business_partner_id: str = "",
    ) -> dict[str, Any]:
        return self.call_tool(
            "sap_create_business_partner",
            {
                "category": category,
                "organization_name": organization_name,
                "first_name": first_name,
                "last_name": last_name,
                "search_term": search_term,
                "business_partner_id": business_partner_id,
            },
        )

    def sap_list_sales_orders(self, top: int = 10) -> dict[str, Any]:
        return self.call_tool("sap_list_sales_orders", {"top": top})

    def sap_get_sales_order(self, sales_order_id: str) -> dict[str, Any]:
        return self.call_tool("sap_get_sales_order", {"sales_order_id": sales_order_id})

    def sap_list_invoices(self, top: int = 10) -> dict[str, Any]:
        return self.call_tool("sap_list_invoices", {"top": top})

    def sap_get_invoice(self, billing_document_id: str) -> dict[str, Any]:
        return self.call_tool(
            "sap_get_invoice", {"billing_document_id": billing_document_id}
        )

    def sap_list_products(self, top: int = 10) -> dict[str, Any]:
        return self.call_tool("sap_list_products", {"top": top})

    def sap_get_product(self, product_id: str) -> dict[str, Any]:
        return self.call_tool("sap_get_product", {"product_id": product_id})

    def sap_update_business_partner(
        self,
        business_partner_id: str,
        organization_name: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        search_term: str | None = None,
    ) -> dict[str, Any]:
        return self.call_tool(
            "sap_update_business_partner",
            {
                "business_partner_id": business_partner_id,
                "organization_name": organization_name,
                "first_name": first_name,
                "last_name": last_name,
                "search_term": search_term,
            },
        )