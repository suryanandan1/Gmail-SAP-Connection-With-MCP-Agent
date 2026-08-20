from __future__ import annotations
import logging
import os
from typing import Any
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from gmail_service import GmailService
from sap_service import SAPService
load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("gmail_sap_mcp_server")

MCP_SERVER_HOST = os.getenv("MCP_SERVER_HOST", "127.0.0.1")
MCP_SERVER_PORT = int(os.getenv("MCP_SERVER_PORT", "8000"))

mcp = FastMCP(
    "gmail-sap-enterprise-server",
    host=MCP_SERVER_HOST,
    port=MCP_SERVER_PORT,
)
gmail = GmailService()
sap = SAPService()

@mcp.tool()
def gmail_auth_status() -> dict[str, Any]:
    return gmail.auth_status()

@mcp.tool()
def gmail_list_messages(max_results: int = 10, label: str = "INBOX") -> list[dict[str, Any]]:
    return gmail.list_messages(max_results=max_results, label=label)

@mcp.tool()
def gmail_search_messages(query: str, max_results: int = 10) -> list[dict[str, Any]]:
    if not query.strip():
        raise ValueError("query cannot be empty")
    return gmail.list_messages(max_results=max_results, query=query, label="")

@mcp.tool()
def gmail_read_message(message_id: str) -> dict[str, Any]:
    return gmail.read_message(message_id)

@mcp.tool()
def gmail_send_email(to: str, subject: str, body: str, cc: str = "", bcc: str = "") -> dict[str, Any]:
    logger.info("gmail_send_email to=%s subject=%r", to, subject)
    result = gmail.send_message(to, subject, body, cc, bcc)
    logger.info("gmail_send_email ok message_id=%s", result.get("message_id"))
    return result

@mcp.tool()
def gmail_create_draft(to: str, subject: str, body: str, cc: str = "", bcc: str = "") -> dict[str, Any]:
    return gmail.create_draft(to, subject, body, cc, bcc)

@mcp.tool()
def gmail_delete_email(message_id: str) -> dict[str, Any]:
    return gmail.delete_message(message_id)

@mcp.tool()
def gmail_list_labels() -> list[dict[str, Any]]:
    return gmail.list_labels()

@mcp.tool()
def gmail_mark_read(message_id: str) -> dict[str, Any]:
    return gmail.mark_read(message_id)

@mcp.tool()
def gmail_mark_unread(message_id: str) -> dict[str, Any]:
    return gmail.mark_unread(message_id)

@mcp.tool()
def gmail_get_attachment(message_id: str, attachment_id: str) -> dict[str, Any]:
    return gmail.get_attachment(message_id, attachment_id)

@mcp.tool()
def sap_configuration_status() -> dict[str, Any]:
    return sap.configuration_status()

@mcp.tool()
def sap_test_connection() -> dict[str, Any]:
    return sap.test_connection()

@mcp.tool()
def sap_list_business_partners(top: int = 10) -> dict[str, Any]:
    return sap.list_business_partners(top=top)

@mcp.tool()
def sap_search_business_partners(name: str, top: int = 10) -> dict[str, Any]:
    return sap.search_business_partners(name=name, top=top)

@mcp.tool()
def sap_get_business_partner(business_partner_id: str) -> dict[str, Any]:
    return sap.get_business_partner(business_partner_id)

@mcp.tool()
def sap_list_email_addresses(top: int = 10) -> dict[str, Any]:
    return sap.list_email_addresses(top=top)

@mcp.tool()
def sap_list_sales_orders(top: int = 10) -> dict[str, Any]:
    return sap.list_sales_orders(top=top)

@mcp.tool()
def sap_get_sales_order(sales_order_id: str) -> dict[str, Any]:
    return sap.get_sales_order(sales_order_id)

@mcp.tool()
def sap_list_invoices(top: int = 10) -> dict[str, Any]:
    return sap.list_invoices(top=top)

@mcp.tool()
def sap_get_invoice(billing_document_id: str) -> dict[str, Any]:
    return sap.get_invoice(billing_document_id)

@mcp.tool()
def sap_list_products(top: int = 10) -> dict[str, Any]:
    return sap.list_products(top=top)

@mcp.tool()
def sap_get_product(product_id: str) -> dict[str, Any]:
    return sap.get_product(product_id)


# @mcp.tool()
# def sap_create_business_partner(
#     category: str,
#     organization_name: str = "",
#     first_name: str = "",
#     last_name: str = "",
#     search_term: str = "",
#     business_partner_id: str = "",
# ) -> dict[str, Any]:
#     """Create an SAP person (category 1) or organization (category 2). Writes must be enabled."""
#     logger.info("sap_create_business_partner category=%s", category)
#     return sap.create_business_partner(
#         category=category,
#         organization_name=organization_name,
#         first_name=first_name,
#         last_name=last_name,
#         search_term=search_term,
#         business_partner_id=business_partner_id,
#     )


# @mcp.tool()
# def sap_update_business_partner(
#     business_partner_id: str,
#     organization_name: str | None = None,
#     first_name: str | None = None,
#     last_name: str | None = None,
#     search_term: str | None = None,
# ) -> dict[str, Any]:
#     """Update selected name/search fields on an SAP business partner. Writes must be enabled."""
#     logger.info("sap_update_business_partner id=%s", business_partner_id)
#     return sap.update_business_partner(
#         business_partner_id=business_partner_id,
#         organization_name=organization_name,
#         first_name=first_name,
#         last_name=last_name,
#         search_term=search_term,
#     )


if __name__ == "__main__":

    logger.info(
        "starting MCP server on %s:%s (sap_write_enabled=%s)",
        MCP_SERVER_HOST,
        MCP_SERVER_PORT,
        sap.write_enabled,
    )
    mcp.run(transport="streamable-http")