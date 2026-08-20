from __future__ import annotations

import base64
import html
import os
import re
import tempfile
from email.header import decode_header
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

load_dotenv()


SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
]
BASE_DIR = Path(__file__).resolve().parent

CREDENTIALS_FILE = BASE_DIR / os.getenv("GMAIL_CREDENTIALS_FILE","credentials/credentials.json",)
TOKEN_FILE = BASE_DIR / os.getenv("GMAIL_TOKEN_FILE","credentials/token.json",)
API_RETRIES = 2

def write_token_atomically(credentials: Credentials) -> None:
    """Persist OAuth credentials without leaving a partial token file."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=TOKEN_FILE.parent,
        prefix=f".{TOKEN_FILE.name}.",
        suffix=".tmp",
        text=True,
    )

    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as token_file:
            token_file.write(credentials.to_json())
            token_file.flush()
            os.fsync(token_file.fileno())

        os.replace(temporary_name, TOKEN_FILE)
    finally:
        try:
            Path(temporary_name).unlink(missing_ok=True)
        except OSError:
            pass

def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    decoded_parts: list[str] = []

    for part, encoding in decode_header(value):
        if isinstance(part, bytes):
            decoded_parts.append(
                part.decode(encoding or "utf-8", errors="replace")
            )
        else:
            decoded_parts.append(part)

    return "".join(decoded_parts)

def decode_base64url(data: str | None) -> str:
    if not data:
        return ""
    try:
        padding = "=" * (-len(data) % 4)
        decoded = base64.urlsafe_b64decode(data + padding)
        return decoded.decode("utf-8", errors="replace")
    except Exception:
        return ""

def html_to_text(content: str) -> str:
    content = re.sub(
        r"<(script|style).*?>.*?</\1>",
        "",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    content = re.sub(r"<br\s*/?>", "\n", content, flags=re.IGNORECASE)
    content = re.sub(r"</p\s*>", "\n\n", content, flags=re.IGNORECASE)
    content = re.sub(r"<[^>]+>", "", content)
    return html.unescape(content).strip()

def extract_body(payload: dict[str, Any]) -> str:

    plain_parts: list[str] = []
    html_parts: list[str] = []

    def walk_part(part: dict[str, Any]) -> None:
        mime_type = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")

        if data:
            decoded = decode_base64url(data)

            if mime_type == "text/plain":
                plain_parts.append(decoded)
            elif mime_type == "text/html":
                html_parts.append(decoded)

        for child in part.get("parts", []) or []:
            walk_part(child)

    walk_part(payload)

    if plain_parts:
        return "\n\n".join(plain_parts).strip()

    if html_parts:
        return html_to_text("\n".join(html_parts))

    # Some simple emails store content directly in payload.body.
    direct_data = payload.get("body", {}).get("data")
    return decode_base64url(direct_data).strip()


def get_header(headers: list[dict[str, str]], name: str) -> str:
    """Get a single header from Gmail's header list."""
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return decode_mime_header(header.get("value"))

    return ""

def build_raw_message(to: str,subject: str,body: str,cc: str = "",bcc: str = "",) -> str:
    recipients = getaddresses([to, cc, bcc])
    if not any(address.strip() for _, address in recipients):
        raise ValueError("at least one recipient is required")

    message = EmailMessage()
    if to.strip():
        message["To"] = to.strip()
    if cc.strip():
        message["Cc"] = cc.strip()
    if bcc.strip():
        message["Bcc"] = bcc.strip()
    message["Subject"] = subject
    message.set_content(body)

    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

class GmailService:
    """Service wrapper around the Gmail API."""

    def __init__(self) -> None:
        self._service: Resource | None = None

    def authenticate(self) -> Credentials:
        """
        Authenticate through Google OAuth.

        The first execution opens a browser. Later executions use token.json.
        """
        credentials: Credentials | None = None

        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)

        if TOKEN_FILE.exists():
            try:
                credentials = Credentials.from_authorized_user_file(
                    str(TOKEN_FILE),
                    SCOPES,
                )
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    f"Saved Gmail token is unreadable: {TOKEN_FILE}. "
                    "Move or delete it, then run authentication again."
                ) from exc

        if credentials and not credentials.has_scopes(SCOPES):
            credentials = None

        if credentials and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except RefreshError as exc:
                raise RuntimeError(
                    "Google rejected the saved Gmail refresh token. "
                    f"Move or delete {TOKEN_FILE}, then run authentication again."
                ) from exc

        if not credentials or not credentials.valid:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"OAuth credentials file not found: {CREDENTIALS_FILE}"
                )

            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE),
                SCOPES,
            )

            credentials = flow.run_local_server(
                port=0,
                access_type="offline",
                prompt="consent",
            )

        write_token_atomically(credentials)
        return credentials

    def get_service(self) -> Resource:
        """Create or reuse an authenticated Gmail API client."""
        if self._service is None:
            credentials = self.authenticate()
            self._service = build("gmail","v1",credentials=credentials,cache_discovery=False,)
        return self._service

    def auth_status(self) -> dict[str, Any]:
        """Return local Gmail authentication status."""
        result: dict[str, Any] = {
            "credentials_file_exists": CREDENTIALS_FILE.exists(),
            "token_file_exists": TOKEN_FILE.exists(),
            "credentials_file": str(CREDENTIALS_FILE),
            "token_file": str(TOKEN_FILE),
            "scopes": SCOPES,
            "authenticated": False,
        }

        try:
            service = self.get_service()
            profile = service.users().getProfile(userId="me").execute(
                num_retries=API_RETRIES
            )

            result.update(
                {
                    "authenticated": True,
                    "email_address": profile.get("emailAddress"),
                    "messages_total": profile.get("messagesTotal"),
                    "threads_total": profile.get("threadsTotal"),
                }
            )
        except Exception as exc:
            result["error_type"] = type(exc).__name__
            result["error"] = str(exc)
        return result

    def list_messages(self,max_results: int = 10,query: str = "",label: str = "INBOX",) -> list[dict[str, Any]]:
        """List Gmail messages and return useful metadata."""
        max_results = max(1, min(max_results, 100))

        request_data: dict[str, Any] = {
            "userId": "me",
            "maxResults": max_results,
        }
        if query.strip():
            request_data["q"] = query.strip()
        if label.strip():
            request_data["labelIds"] = [label.strip()]

        try:
            service = self.get_service()
            response = (service.users().messages().list(**request_data).execute(num_retries=API_RETRIES))
            messages = response.get("messages", [])
            results: list[dict[str, Any]] = []
            for item in messages:
                message = (
                    service.users()
                    .messages()
                    .get(
                        userId="me",
                        id=item["id"],
                        format="metadata",
                        metadataHeaders=[
                            "From",
                            "To",
                            "Subject",
                            "Date",
                        ],
                    )
                    .execute(num_retries=API_RETRIES)
                )

                payload = message.get("payload", {})
                headers = payload.get("headers", [])

                results.append(
                    {
                        "message_id": message.get("id"),
                        "thread_id": message.get("threadId"),
                        "from": get_header(headers, "From"),
                        "to": get_header(headers, "To"),
                        "subject": get_header(headers, "Subject"),
                        "date": get_header(headers, "Date"),
                        "snippet": message.get("snippet", ""),
                        "label_ids": message.get("labelIds", []),
                    }
                )
            return results
        except HttpError as exc:
            raise RuntimeError(f"Gmail API error: {exc}") from exc

    def send_message(self,to: str,subject: str,body: str,cc: str = "",bcc: str = "",) -> dict[str, Any]:
        raw = build_raw_message(to, subject, body, cc, bcc)
        try:
            message = (self.get_service().users().messages().send(userId="me", body={"raw": raw}).execute(num_retries=API_RETRIES))
            return {
                "status": "sent","message_id": message.get("id"),"thread_id": message.get("threadId"),"label_ids": message.get("labelIds", []),
            }
        except HttpError as exc:
            raise RuntimeError(f"Gmail API error: {exc}") from exc

    def create_draft(self,to: str,subject: str,body: str,cc: str = "",bcc: str = "",) -> dict[str, Any]:
        raw = build_raw_message(to, subject, body, cc, bcc)
        try:
            draft = (
                self.get_service()
                .users()
                .drafts()
                .create(userId="me", body={"message": {"raw": raw}})
                .execute(num_retries=API_RETRIES)
            )
            draft_message = draft.get("message", {})
            return {
                "status": "draft_created",
                "draft_id": draft.get("id"),
                "message_id": draft_message.get("id"),
                "thread_id": draft_message.get("threadId"),
            }
        except HttpError as exc:
            raise RuntimeError(f"Gmail API error: {exc}") from exc

    def delete_message(self, message_id: str) -> dict[str, Any]:
        message_id = message_id.strip()
        if not message_id:
            raise ValueError("message_id cannot be empty")
        try:
            message = (
                self.get_service()
                .users()
                .messages()
                .trash(userId="me", id=message_id)
                .execute(num_retries=API_RETRIES)
            )
            return {
                "status": "moved_to_trash",
                "message_id": message.get("id", message_id),
                "thread_id": message.get("threadId"),
                "label_ids": message.get("labelIds", []),
            }
        except HttpError as exc:
            raise RuntimeError(f"Gmail API error: {exc}") from exc

    def list_labels(self) -> list[dict[str, Any]]:
        """List all labels on the gmail account."""
        try:
            response = (
                self.get_service()
                .users()
                .labels()
                .list(userId="me")
                .execute(num_retries=API_RETRIES)
            )
            return [
                {
                    "id": label.get("id"),
                    "name": label.get("name"),
                    "type": label.get("type"),
                }
                for label in response.get("labels", [])
            ]
        except HttpError as exc:
            raise RuntimeError(f"Gmail API error: {exc}") from exc

    def _modify_labels(self, message_id: str, add: list[str], remove: list[str]) -> dict[str, Any]:
        message_id = message_id.strip()
        if not message_id:
            raise ValueError("message_id cannot be empty")
        try:
            message = (
                self.get_service()
                .users()
                .messages()
                .modify(
                    userId="me",
                    id=message_id,
                    body={"addLabelIds": add, "removeLabelIds": remove},
                )
                .execute(num_retries=API_RETRIES)
            )
            return {
                "message_id": message.get("id", message_id),
                "label_ids": message.get("labelIds", []),
            }
        except HttpError as exc:
            raise RuntimeError(f"Gmail API error: {exc}") from exc

    def mark_read(self, message_id: str) -> dict[str, Any]:
        result = self._modify_labels(message_id, add=[], remove=["UNREAD"])
        return {"status": "marked_read", **result}

    def mark_unread(self, message_id: str) -> dict[str, Any]:
        result = self._modify_labels(message_id, add=["UNREAD"], remove=[])
        return {"status": "marked_unread", **result}

    def get_attachment(
        self, message_id: str, attachment_id: str
    ) -> dict[str, Any]:
        message_id = message_id.strip()
        attachment_id = attachment_id.strip()
        if not message_id or not attachment_id:
            raise ValueError("message_id and attachment_id are required")
        try:
            attachment = (
                self.get_service()
                .users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=attachment_id)
                .execute(num_retries=API_RETRIES)
            )
            raw = attachment.get("data", "")
            padding = "=" * (-len(raw) % 4)
            raw_bytes = base64.urlsafe_b64decode(raw + padding) if raw else b""
            return {
                "message_id": message_id,
                "attachment_id": attachment_id,
                "size": attachment.get("size", len(raw_bytes)),
                "data_base64": base64.b64encode(raw_bytes).decode("ascii"),
            }
        except HttpError as exc:
            raise RuntimeError(f"Gmail API error: {exc}") from exc

    def read_message(self, message_id: str) -> dict[str, Any]:
        message_id = message_id.strip()
        if not message_id:
            raise ValueError("message_id cannot be empty")
        try:
            service = self.get_service()
            message = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="full",
                )
                .execute(num_retries=API_RETRIES)
            )
            payload = message.get("payload", {})
            headers = payload.get("headers", [])
            attachments: list[dict[str, Any]] = []

            def collect_attachments(part: dict[str, Any]) -> None:
                filename = part.get("filename", "")
                body = part.get("body", {})

                if filename:
                    attachments.append(
                        {
                            "filename": filename,
                            "mime_type": part.get("mimeType"),
                            "size": body.get("size", 0),
                            "attachment_id": body.get("attachmentId"),
                        }
                    )
                for child in part.get("parts", []) or []:
                    collect_attachments(child)
            collect_attachments(payload)

            return {
                "message_id": message.get("id"),
                "thread_id": message.get("threadId"),
                "from": get_header(headers, "From"),
                "to": get_header(headers, "To"),
                "cc": get_header(headers, "Cc"),
                "subject": get_header(headers, "Subject"),
                "date": get_header(headers, "Date"),
                "snippet": message.get("snippet", ""),
                "body": extract_body(payload),
                "attachments": attachments,
                "label_ids": message.get("labelIds", []),
            }
        except HttpError as exc:
            raise RuntimeError(f"Gmail API error: {exc}") from exc