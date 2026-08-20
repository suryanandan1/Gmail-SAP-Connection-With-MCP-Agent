# Gmail + SAP MCP Assistant

A Streamlit application that lets you read and manage Gmail, browse SAP
Business Partner / Sales / Billing / Product data, and ask an AI assistant
natural-language questions about your inbox (and SAP records) — all
through a single [Model Context Protocol](https://modelcontextprotocol.io)
(MCP) server.

```
                              Streamlit UI (app.py)
                                       │
                       ┌───────────────┴───────────────┐
                       ▼                               ▼
           Gmail / SAP / Admin tabs                  Ai tab
           (direct button click)              EmailAssistant (ai_agent.py)
                       │                       → Mistral LLM decides which
                       │                         tool(s) to call, if any
                       │                                │
                       └───────────────┬────────────────┘
                                       ▼
                     MCPClient (mcp_client.py) — one persistent
                     background event loop for every call, either path
                                        │
                                        │  Streamable HTTP transport
                                        ▼
                        FastMCP server (server.py) — dispatches
                              the tool call by name
                                        │
                       ┌────────────────┴────────────────┐
                       ▼                                 ▼
                GmailService                        SAPService
              (gmail_service.py)                  (sap_service.py)
                       │                                  │
                       ▼                                  ▼
                  Gmail API                          SAP OData API
                (OAuth credentials)                  (API key, sandbox)
```

The UI never talks to Gmail or SAP directly — every action, whether it's a
button click or something the AI assistant decided to do, goes through the
same `MCPClient` → FastMCP server → service layer path. This keeps a single,
auditable boundary between the app and the two external systems.

## Request flow

There are two ways a request enters the system, and they converge at
`MCPClient`.

### 1. Direct UI action (Gmail / SAP / Admin tabs)

1. You click a button (e.g. "Search" in the Gmail tab). `app.py` calls a
   method directly on the shared `MCPClient` instance, e.g.
   `mcp_client.gmail_search_messages(query, max_results)`.
2. `MCPClient` schedules that coroutine on its dedicated background event
   loop and blocks until it completes (`_loop.run(...)` in `mcp_client.py`)
   — this is what keeps the MCP session safe across Streamlit's
   rerun-per-interaction model.
3. The FastMCP server (`server.py`) receives the tool call over Streamable
   HTTP and dispatches it to the matching function, e.g.
   `gmail_search_messages()`.
4. That function calls into `GmailService` (`gmail_service.py`) or
   `SAPService` (`sap_service.py`), which makes the real network call —
   the Gmail API (OAuth) or the SAP OData API (API key) — and returns
   parsed, plain-dict results.
5. The result travels back up: MCP server → `MCPClient._unwrap()` →
   `app.py`, which stores it in `st.session_state` and renders it.

Errors at any layer (`MCPToolError`, `MCPConnectionError`, or anything
unexpected) are caught centrally by `run_action()` in `app.py` and shown
as an `st.error`, so a failed call never crashes the UI.

### 2. AI Assistant question (Ai tab)

This is the same pipeline with a decision loop in front of it:

1. Your question (plus chat history) goes to `EmailAssistant.ask()` in
   `ai_agent.py`, which builds a system prompt via `prompts.py` (injected
   with today's/tomorrow's dates) and sends it to Mistral along with the
   `TOOLS` schema.
2. Mistral decides whether it needs a tool. If so, `ai_agent.py` runs the
   call through the **same** `MCPClient` instance used by the manual tabs
   — `EmailAssistant` never talks to Gmail/SAP itself — and feeds the
   JSON result back into the conversation.
3. Steps 1–2 repeat (search, then maybe read a specific message, then
   maybe another search) up to `MISTRAL_MAX_STEPS` times (default 12).
4. Once Mistral has enough information, it stops calling tools and
   returns a final natural-language answer, which `app.py` renders in the
   chat — with the full tool-call trace available in a collapsible
   "Tool calls used" expander for debugging.

The agent's tool list is deliberately narrow (read-only Gmail/SAP
lookups), so no matter what you ask it, it cannot send, delete, or modify
anything. Only the manual flows in the Gmail tab can do that, and each of
those requires an explicit confirmation step before the destructive
action fires.

### 3. App startup (once per process)

`get_mcp_client()` in `app.py` is wrapped in `@st.cache_resource`, so it
runs exactly once: it creates the `MCPClient`, calls `.connect()`, which
starts the background thread + event loop and opens the Streamable HTTP
session to the FastMCP server. That session stays alive for the life of
the Streamlit process — it is *not* recreated on every rerun.

## Features

### Gmail (via the "Gmail" tab)
- List / search messages using Gmail's native search syntax
- Read a full message (body, headers, attachments)
- Send email, create drafts, move messages to Trash (each gated behind an
  explicit confirmation step in the UI)
- Mark messages read/unread, list labels, fetch attachment bytes

### SAP (via the "SAP" tab)
- Check configuration / connectivity to the SAP OData sandbox
- List and search Business Partners, view a single partner
- List email addresses on file
- (Read-only by default — see [SAP writes](#sap-writes) below)

### AI Assistant (via the "Ai" tab)
A Mistral-powered agent (`ai_agent.py` / `EmailAssistant`) that answers
natural-language questions such as:

- "Summarize today's important emails"
- "Find emails containing attachments"
- "Do I have any interviews scheduled tomorrow?"
- "Summarize emails from the last 7 days"

The agent is intentionally **read-only**: it can only call
`gmail_search_messages`, `gmail_read_message`, and `gmail_list_messages`
(plus the read-only SAP lookups described in `prompts.py`). It has no
tool for sending, replying to, or deleting anything, and the system
prompt explicitly instructs it to say so if asked — every fact in its
answers must trace back to an actual tool result, never an invented one.

### Administration tab
Live MCP connection status, the list of tools the server currently
exposes, and the last-fetched Gmail/SAP configuration — useful for
verifying the server is reachable and correctly configured.

## Project layout

| File | Responsibility |
|---|---|
| `app.py` | Streamlit UI — tabs for AI, Gmail, SAP, and Admin |
| `mcp_client.py` | Streamlit-safe MCP client (single persistent background event loop; see the module docstring for why) |
| `ai_agent.py` | Agent loop: sends the user's question + tool results to Mistral until it produces a final answer |
| `prompts.py` | System prompt: tool permissions, date context, Gmail search syntax reference, answer-style guidance |
| `server.py` | FastMCP server exposing Gmail and SAP operations as MCP tools |
| `gmail_service.py` | Gmail API wrapper: OAuth flow, message parsing, send/draft/delete/label operations |
| `sap_service.py` | SAP OData wrapper: Business Partner, Sales Order, Billing Document, Product reads (writes implemented but disabled by default) |
| `requirements.txt` | Python dependencies |

## Prerequisites

- Python 3.10+
- A Google Cloud project with the Gmail API enabled and an OAuth
  **Desktop app** client (`credentials.json`)
- Access to an SAP OData sandbox (e.g. the SAP Business Accelerator Hub)
  with an API key, if you want the SAP tab/tools to work
- A [Mistral](https://mistral.ai) API key, if you want the AI Assistant
  tab to work

## Setup

1. **Install dependencies**

   ```bash
   python -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Gmail OAuth credentials**

   Download your OAuth client's `credentials.json` from Google Cloud
   Console and place it at `credentials/credentials.json` (or point
   `GMAIL_CREDENTIALS_FILE` at a different path). The first time the
   server calls a Gmail tool, it opens a browser window for you to
   authorize; the resulting token is written to `credentials/token.json`.

3. **Environment variables**

   Create a `.env` file in the project root:

   ```dotenv
   # --- MCP server ---
   MCP_SERVER_HOST=127.0.0.1
   MCP_SERVER_PORT=8000
   LOG_LEVEL=INFO

   # --- MCP client (Streamlit side) ---
   MCP_SERVER_URL=http://127.0.0.1:8000/mcp
   MCP_CONNECT_TIMEOUT_SECONDS=20
   MCP_CALL_TIMEOUT_SECONDS=60

   # --- Gmail ---
   GMAIL_CREDENTIALS_FILE=credentials/credentials.json
   GMAIL_TOKEN_FILE=credentials/token.json

   # --- SAP ---
   SAP_MODE=sandbox
   SAP_BASE_URL=https://sandbox.api.sap.com/s4hanacloud/sap/opu/odata/sap/API_BUSINESS_PARTNER
   SAP_API_KEY=your-sap-api-key
   SAP_VERIFY_SSL=true
   SAP_WRITE_ENABLED=false
   SAP_TIMEOUT_SECONDS=30

   # --- AI Assistant ---
   MISTRAL_API_KEY=your-mistral-api-key
   MISTRAL_MODEL=mistral-large-latest
   MISTRAL_MAX_STEPS=12
   ```

   Every variable has a sane default except the credentials/API keys
   themselves — see the top of each `*_service.py` / `*_client.py` file
   for the exact fallback values.

4. **Run the MCP server**

   ```bash
   python server.py
   ```

   This starts the FastMCP server on `MCP_SERVER_HOST:MCP_SERVER_PORT`
   using the Streamable HTTP transport.

5. **Run the Streamlit app** (in a second terminal)

   ```bash
   streamlit run app.py
   ```

   Open the URL Streamlit prints (typically `http://localhost:8501`).

## SAP writes

`SAPService` already implements `create_business_partner` and
`update_business_partner` against SAP's OData write endpoints, but the
corresponding MCP tools are commented out in `server.py` and the calls
are hard-gated behind `SAP_WRITE_ENABLED=true` in `sap_service.py`. To
enable them:

1. Set `SAP_WRITE_ENABLED=true` in `.env`.
2. Uncomment the `sap_create_business_partner` / `sap_update_business_partner`
   tool definitions in `server.py`.
3. Restart the MCP server.

Until then, all SAP access is read-only regardless of the flag.

## Safety notes

- The AI Assistant can only *read* Gmail — it has no send/delete/modify
  tool, enforced both in `ai_agent.py`'s `TOOLS` list and in the system
  prompt in `prompts.py`.
- Sending email, creating drafts, and deleting/trashing messages in the
  **Gmail tab** each require an explicit confirmation checkbox or a
  "search → open → confirm" flow before the destructive action fires.
- SAP writes are off by default (`SAP_WRITE_ENABLED=false`) and the
  tools that would perform them aren't even registered on the MCP
  server unless you opt in.

## Troubleshooting

- **"Could not connect to the MCP server"** — make sure `server.py` is
  running and `MCP_SERVER_URL` in your `.env` matches its host/port.
- **Gmail OAuth errors mentioning a stale/invalid token** — delete the
  file at `GMAIL_TOKEN_FILE` and retry; the app will re-run the OAuth
  flow.
- **SAP requests failing with HTTP errors** — some SAP sandbox tenants
  don't have every OData service (e.g. `API_SALES_ORDER_SRV`,
  `API_BILLING_DOCUMENT_SRV`) activated by default; check the error
  body against your API catalog.
- **"AI assistant is not available"** — `MISTRAL_API_KEY` is missing or
  empty in `.env`.
