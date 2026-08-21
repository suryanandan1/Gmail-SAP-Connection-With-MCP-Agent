from __future__ import annotations

import base64
from typing import Any, Callable

import streamlit as st

from mcp_client import (MCP_CALL_TIMEOUT_SECONDS, MCP_CONNECT_TIMEOUT_SECONDS,MCPClient,MCPConnectionError,
MCPToolError,
)
from ai_agent import AgentError, EmailAssistant

st.set_page_config(page_title="Gmail and SAP MCP",page_icon="",layout="wide",)



@st.cache_resource
def get_mcp_client() -> MCPClient:
    client = MCPClient()
    client.connect()
    return client


@st.cache_resource
def get_email_assistant() -> EmailAssistant:
    return EmailAssistant(get_mcp_client())


def run_action(action: Callable[..., Any], *args: Any, **kwargs: Any) -> Any | None:
    """Call an MCPClient method and surface MCP-specific errors clearly."""
    try:
        return action(*args, **kwargs)
    except MCPToolError as exc:
        st.error(f"MCP tool error: {exc}")
        return None
    except MCPConnectionError as exc:
        st.error(f"MCP connection error: {exc}")
        return None
    except Exception as exc: 
        st.error(f"{type(exc).__name__}: {exc}")
        return None



# Gmail
def show_email(message: dict[str, Any]) -> None:
    st.subheader(message.get("subject") or "(No subject)")
    st.caption(
        f"From: {message.get('from', '')}  \n"
        f"To: {message.get('to', '')}  \n"
        f"Date: {message.get('date', '')}"
    )
    st.text_area(
        "Message body",
        value=message.get("body", ""),
        height=320,
        disabled=True,
        label_visibility="collapsed",
    )
    attachments = message.get("attachments") or []
    if attachments:
        st.caption(f"{len(attachments)} attachment(s)")
        message_id = message.get("message_id", "")
        for att_index, att in enumerate(attachments):
            filename = att.get("filename") or "unnamed"
            size = att.get("size", 0)
            attachment_id = att.get("attachment_id") or ""
            data_key = f"attach_data_{message_id}_{att_index}"

            col1, col2 = st.columns([4, 1])
            col1.write(f" {filename} ({size} bytes)")

            if not attachment_id:
                col2.caption("Not downloadable")
                continue

            if st.session_state.get(data_key) is None:
                if col2.button(
                    "Prepare download",
                    key=f"attach_fetch_{message_id}_{att_index}",
                ):
                    result = run_action(
                        mcp_client.gmail_get_attachment, message_id, attachment_id
                    )
                    if result and result.get("data_base64"):
                        st.session_state[data_key] = base64.b64decode(
                            result["data_base64"]
                        )
                        st.rerun()
            else:
                col2.download_button(
                    "Download",
                    data=st.session_state[data_key],
                    file_name=filename,
                    key=f"attach_dl_{message_id}_{att_index}",
                )


def show_email_results(messages: list[dict[str, Any]] | None, key_prefix: str) -> None:
    if not messages:
        st.info("No emails found.")
        return
    if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
        st.error("Unexpected response shape from the MCP server (expected a list of messages).")
        st.write(messages)
        return
    st.caption(f"{len(messages)} email(s) found")
    for index, message in enumerate(messages):
        message_id = message.get("message_id", "")
        title = message.get("subject") or "(No subject)"
        sender = message.get("from") or "Unknown sender"
        with st.expander(f"{title} — {sender}"):
            st.caption(message.get("date", ""))
            st.write(message.get("snippet") or "No preview available.")
            cols = st.columns(3)
            if cols[0].button("Open", key=f"{key_prefix}_open_{index}_{message_id}"):
                opened = run_action(mcp_client.gmail_read_message, message_id)
                if opened:
                    st.session_state[f"{key_prefix}_opened"] = opened
            if cols[1].button("Mark read", key=f"{key_prefix}_read_{index}_{message_id}"):
                if run_action(mcp_client.gmail_mark_read, message_id):
                    st.success("Marked as read.")
            if cols[2].button("Mark unread", key=f"{key_prefix}_unread_{index}_{message_id}"):
                if run_action(mcp_client.gmail_mark_unread, message_id):
                    st.success("Marked as unread.")
    opened = st.session_state.get(f"{key_prefix}_opened")
    if opened:
        st.divider()
        show_email(opened)


def partner_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "Business Partner": item.get("BusinessPartner", ""),
            "Name": item.get("BusinessPartnerFullName", ""),
            "Category": item.get("BusinessPartnerCategory", ""),
            "Customer": item.get("Customer", ""),
            "Supplier": item.get("Supplier", ""),
        }
        for item in records
    ]


mcp_client = get_mcp_client()

st.title("Gmail + SAP — MCP")
# st.caption("Streamlit talks only to the FastMCP server via MCPClient. No direct Gmail/SAP calls.")

with st.sidebar:
    st.header("MCP Connection")
    st.code(mcp_client.server_url, language=None)
    if st.button("Test MCP connection", use_container_width=True):
        tools = run_action(mcp_client.list_tool_names)
        if tools is not None:
            st.session_state.mcp_tools = tools
            st.success(f"Connected — {len(tools)} tool(s) available.")
    if st.session_state.get("mcp_tools"):
        st.caption(f"{len(st.session_state.mcp_tools)} tool(s) discovered")

    st.divider()
    st.header("AI Assistant")
    st.caption("Try asking:")
    ai_example_questions = [
        "Summarize today's important emails",
        "Find emails containing attachments",
        "Summarize emails from the last 7 days",
    ]
    ai_clicked_example = None
    for question in ai_example_questions:
        if st.button(question, use_container_width=True, key=f"ai_example_{question}"):
            ai_clicked_example = question

tab_ai, tab_gmail, tab_sap, tab_admin = st.tabs(["Ai", "Gmail", "SAP", "Administration"])

with tab_ai:
    st.markdown(
        """
        <style>
        div[data-testid="stChatInput"] {
            position: sticky;
            bottom: 0;
            z-index: 999;
            padding-top: 0.5rem;
            padding-bottom: 0.5rem;
            background-color: var(--background-color, #ffffff);
        }
        section.main > div.block-container {
            padding-bottom: 1rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    if "ai_chat_history" not in st.session_state:
        st.session_state.ai_chat_history = []

    for turn in st.session_state.ai_chat_history:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])

    user_question = st.chat_input("Ask about your emails...") or ai_clicked_example

    if user_question:
        st.session_state.ai_chat_history.append({"role": "user", "content": user_question})
        with st.chat_message("user"):
            st.markdown(user_question)

        with st.chat_message("assistant"):
            answer = ""
            steps: list[dict[str, Any]] = []
            with st.spinner("Checking your inbox via MCP..."):
                try:
                    assistant = get_email_assistant()
                    llm_history = [
                        {"role": t["role"], "content": t["content"]}
                        for t in st.session_state.ai_chat_history[:-1]
                        if t["role"] in ("user", "assistant")
                    ]
                    result = assistant.ask(user_question, history=llm_history)
                    answer = result["answer"]
                    steps = result.get("steps", [])
                except AgentError as exc:
                    answer = f"AI assistant is not available: {exc}"
                except (MCPToolError, MCPConnectionError) as exc:
                    answer = f"MCP error while answering: {exc}"
                except Exception as exc:
                    answer = f"{type(exc).__name__}: {exc}"
            st.markdown(answer)
            if steps:
                with st.expander(f"Tool calls used ({len(steps)})", expanded=False):
                    for step in steps:
                        st.write(f"**{step['tool']}**")
                        st.json({"arguments": step["arguments"], "result": step["result"]})

        st.session_state.ai_chat_history.append({"role": "assistant", "content": answer})
with tab_gmail:
    st.subheader("Gmail")

    if st.button("Check Gmail status"):
        st.session_state.gmail_status = run_action(mcp_client.gmail_auth_status)
    status = st.session_state.get("gmail_status")
    if status:
        if status.get("authenticated"):
            st.success(f"Connected as {status.get('email_address', 'unknown')}")
        else:
            st.warning("Gmail is not authenticated.")

    st.divider()
    gmail_action = st.radio(
        "Action",
        ["List emails", "Search emails", "Read email", "Send email", "Create draft", "Delete email"],
        horizontal=True,
    )

    if gmail_action == "List emails":
        col1, col2 = st.columns([2, 1])
        label = col1.text_input("Label", value="INBOX")
        max_results = col2.number_input("Max results", 1, 100, 10)
        if st.button("Load messages", type="primary"):
            st.session_state.gmail_list = run_action(
                mcp_client.gmail_list_messages, max_results=int(max_results), label=label.strip()
            )
        show_email_results(st.session_state.get("gmail_list"), "gmail_list")

    elif gmail_action == "Search emails":
        col1, col2 = st.columns([3, 1])
        query = col1.text_input("Gmail search query", placeholder="is:unread newer_than:7d")
        max_results = col2.number_input("Max results", 1, 100, 10, key="search_max")
        if st.button("Search", type="primary"):
            if query.strip():
                st.session_state.gmail_search = run_action(
                    mcp_client.gmail_search_messages, query.strip(), max_results=int(max_results)
                )
            else:
                st.warning("Enter a search query.")
        show_email_results(st.session_state.get("gmail_search"), "gmail_search")

    elif gmail_action == "Read email":
        message_id = st.text_input("Message ID")
        if st.button("Read", type="primary"):
            if message_id.strip():
                result = run_action(mcp_client.gmail_read_message, message_id.strip())
                if result:
                    st.session_state.gmail_read_result = result
            else:
                st.warning("Enter a message ID.")
        if st.session_state.get("gmail_read_result"):
            show_email(st.session_state.gmail_read_result)

    elif gmail_action == "Send email":
        with st.form("send_form"):
            to = st.text_input("To")
            cc = st.text_input("CC")
            bcc = st.text_input("BCC")
            subject = st.text_input("Subject")
            body = st.text_area("Message", height=220)
            confirm = st.checkbox("I confirm this email may be sent")
            submitted = st.form_submit_button("Send", type="primary")
        if submitted:
            if not any([to.strip(), cc.strip(), bcc.strip()]):
                st.warning("Enter at least one recipient.")
            elif not confirm:
                st.warning("Confirm before sending.")
            else:
                result = run_action(mcp_client.gmail_send_email, to, subject, body, cc, bcc)
                if result:
                    st.success("Email sent.")
                    st.json(result)

    elif gmail_action == "Create draft":
        with st.form("draft_form"):
            to = st.text_input("To")
            cc = st.text_input("CC")
            bcc = st.text_input("BCC")
            subject = st.text_input("Subject")
            body = st.text_area("Message", height=220)
            submitted = st.form_submit_button("Create draft", type="primary")
        if submitted:
            if not any([to.strip(), cc.strip(), bcc.strip()]):
                st.warning("Enter at least one recipient.")
            else:
                result = run_action(mcp_client.gmail_create_draft, to, subject, body, cc, bcc)
                if result:
                    st.success("Draft created.")
                    st.json(result)

    elif gmail_action == "Delete email":
        st.warning("Search first, open the correct email, then confirm deletion.")
        delete_query = st.text_input("Search by sender, subject, or Gmail query", key="delete_query")
        if st.button("Find emails"):
            if delete_query.strip():
                st.session_state.gmail_delete_results = run_action(
                    mcp_client.gmail_search_messages, delete_query.strip(), max_results=20
                )
            else:
                st.warning("Enter a search query.")
        show_email_results(st.session_state.get("gmail_delete_results"), "gmail_delete")
        opened = st.session_state.get("gmail_delete_opened")
        if opened:
            confirmed = st.checkbox("Yes, move this opened email to Gmail Trash.")
            if st.button("Move to Trash", disabled=not confirmed, type="primary"):
                result = run_action(mcp_client.gmail_delete_email, opened["message_id"])
                if result:
                    st.success("Email moved to Trash.")
                    st.session_state.gmail_delete_opened = None
                    st.rerun()

with tab_sap:
    st.subheader("SAP Business Partner Sandbox")

    if st.button("Check SAP configuration"):
        st.session_state.sap_config = run_action(mcp_client.sap_configuration_status)
    config = st.session_state.get("sap_config")
    if config:
        c1, c2, c3 = st.columns(3)
        c1.metric("Base URL", "Configured" if config.get("base_url_configured") else "Missing")
        c2.metric("API key", "Configured" if config.get("api_key_configured") else "Missing")
        c3.metric("Mode", config.get("mode", "unknown"))
        if config.get("write_enabled"):
            st.info("SAP writes are enabled.")

    st.divider()
    sap_action = st.radio(
        "Action",
        ["Test connection", "List business partners", "Search business partners",
         "View business partner", "List email addresses"],
        horizontal=True,
    )

    if sap_action == "Test connection":
        if st.button("Test SAP connection", type="primary"):
            st.session_state.sap_test = run_action(mcp_client.sap_test_connection)
        if st.session_state.get("sap_test"):
            st.json(st.session_state.sap_test)

    elif sap_action == "List business partners":
        top = st.slider("Maximum records", 1, 50, 10)
        if st.button("Load partners", type="primary"):
            st.session_state.sap_partners = run_action(mcp_client.sap_list_business_partners, top=top)
        result = st.session_state.get("sap_partners")
        if result:
            st.dataframe(partner_rows(result.get("business_partners", [])), use_container_width=True, hide_index=True)

    elif sap_action == "Search business partners":
        col1, col2 = st.columns([3, 1])
        name = col1.text_input("Business-partner name")
        top = col2.slider("Max", 1, 50, 10)
        if st.button("Search", type="primary"):
            if name.strip():
                st.session_state.sap_search = run_action(
                    mcp_client.sap_search_business_partners, name.strip(), top=top
                )
            else:
                st.warning("Enter a name.")
        result = st.session_state.get("sap_search")
        if result:
            st.dataframe(partner_rows(result.get("business_partners", [])), use_container_width=True, hide_index=True)

    elif sap_action == "View business partner":
        partner_id = st.text_input("Business-partner ID")
        if st.button("Get partner", type="primary"):
            if partner_id.strip():
                st.session_state.sap_partner = run_action(mcp_client.sap_get_business_partner, partner_id.strip())
            else:
                st.warning("Enter a business-partner ID.")
        if st.session_state.get("sap_partner"):
            st.json(st.session_state.sap_partner.get("business_partner", {}))

    elif sap_action == "List email addresses":
        top = st.slider("Maximum records", 1, 50, 10, key="sap_email_top")
        if st.button("Load email records", type="primary"):
            st.session_state.sap_emails = run_action(mcp_client.sap_list_email_addresses, top=top)
        result = st.session_state.get("sap_emails")
        if result:
            st.dataframe(result.get("email_addresses", []), use_container_width=True, hide_index=True)
# admin
with tab_admin:
    st.subheader("MCP Connection Status")
    if st.button("Refresh connection status", type="primary"):
        tools = run_action(mcp_client.list_tool_names)
        st.session_state.admin_tools = tools
        st.session_state.admin_connected = tools is not None

    connected = st.session_state.get("admin_connected")
    if connected is True:
        st.success("Connected to the MCP server.")
    elif connected is False:
        st.error("Could not reach the MCP server.")
    else:
        st.info("Click \"Refresh connection status\" to check.")

    st.divider()
    st.subheader("Available MCP Tools")
    tools = st.session_state.get("admin_tools") or st.session_state.get("mcp_tools")
    if tools:
        gmail_tools = sorted(t for t in tools if t.startswith("gmail_"))
        sap_tools = sorted(t for t in tools if t.startswith("sap_"))
        other_tools = sorted(t for t in tools if not t.startswith(("gmail_", "sap_")))
        col1, col2 = st.columns(2)
        with col1:
            st.markdown(f"**Gmail tools ({len(gmail_tools)})**")
            for name in gmail_tools:
                st.write(f"• {name}")
        with col2:
            st.markdown(f"**SAP tools ({len(sap_tools)})**")
            for name in sap_tools:
                st.write(f"• {name}")
        if other_tools:
            st.markdown(f"**Other tools ({len(other_tools)})**")
            for name in other_tools:
                st.write(f"• {name}")
    else:
        st.info("No tool list loaded yet — refresh the connection status above.")

    st.divider()
    st.subheader("Server Information")
    st.write(f"**MCP server URL:** `{mcp_client.server_url}`")
    st.write(f"**Connect timeout:** {MCP_CONNECT_TIMEOUT_SECONDS}s")
    st.write(f"**Call timeout:** {MCP_CALL_TIMEOUT_SECONDS}s")

    sap_config = st.session_state.get("sap_config")
    if sap_config:
        st.write("**SAP configuration (via `sap_configuration_status` tool):**")
        st.json(sap_config)
    else:
        st.caption("Visit the SAP tab and click \"Check SAP configuration\" to populate this.")

    gmail_status = st.session_state.get("gmail_status")
    if gmail_status:
        st.write("**Gmail configuration (via `gmail_auth_status` tool):**")
        st.json(gmail_status)
    else:
        st.caption("Visit the Gmail tab and click \"Check Gmail status\" to populate this.")