"""Minimal read-only Gmail adapter for relay routing metadata."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.utils import parseaddr
from typing import Any, Callable, Mapping, Protocol


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class GmailReaderError(RuntimeError):
    """A Gmail history or message read could not safely complete."""


@dataclass(frozen=True)
class HistoryBatch:
    message_ids: tuple[str, ...]


@dataclass(frozen=True)
class MessageMetadata:
    message_id: str
    subject: str | None
    sender: str | None


class GmailReader(Protocol):
    def list_history(self, start_history_id: str) -> HistoryBatch: ...

    def get_message_metadata(self, message_id: str) -> MessageMetadata: ...


def _decoded_header(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError, ValueError):
        return None


def _normalized_sender(value: object) -> str | None:
    """Mirror the existing A1 exact parseaddr/strip/lower semantics."""

    if not isinstance(value, str) or "\r" in value or "\n" in value:
        return None
    address = parseaddr(value, strict=True)[1].strip().casefold()
    if (
        not address
        or "@" not in address
        or parseaddr(address, strict=True)[1].casefold() != address
    ):
        return None
    return address


def decode_message_metadata(message: Mapping[str, Any]) -> MessageMetadata:
    message_id = str(message.get("id", "")).strip()
    if not message_id:
        raise GmailReaderError("Gmail message metadata has no message id.")
    payload = message.get("payload")
    headers = payload.get("headers") if isinstance(payload, Mapping) else None
    if not isinstance(headers, list):
        return MessageMetadata(message_id, None, None)

    subjects: list[object] = []
    senders: list[object] = []
    for header in headers:
        if not isinstance(header, Mapping):
            continue
        name = str(header.get("name", "")).casefold()
        if name == "subject":
            subjects.append(header.get("value"))
        elif name == "from":
            senders.append(header.get("value"))
    subject = _decoded_header(subjects[0]) if len(subjects) == 1 else None
    sender = _normalized_sender(senders[0]) if len(senders) == 1 else None
    return MessageMetadata(message_id, subject, sender)


class FakeGmailReader:
    """Network-free test double."""

    def __init__(
        self,
        *,
        history: HistoryBatch | Exception | None = None,
        messages: Mapping[str, MessageMetadata | Exception] | None = None,
    ) -> None:
        self.history = history or HistoryBatch(())
        self.messages = dict(messages or {})
        self.list_history_calls: list[str] = []
        self.get_message_calls: list[str] = []

    def list_history(self, start_history_id: str) -> HistoryBatch:
        self.list_history_calls.append(start_history_id)
        if isinstance(self.history, Exception):
            raise self.history
        return self.history

    def get_message_metadata(self, message_id: str) -> MessageMetadata:
        self.get_message_calls.append(message_id)
        value = self.messages[message_id]
        if isinstance(value, Exception):
            raise value
        return value


class RealGmailReader:
    """Gmail API reader limited to history ids and Subject/From metadata."""

    def __init__(self, service: Any, *, max_pages: int = 500) -> None:
        self._service = service
        self._max_pages = max_pages

    def list_history(self, start_history_id: str) -> HistoryBatch:
        message_ids: list[str] = []
        seen_ids: set[str] = set()
        seen_tokens: set[str] = set()
        page_token: str | None = None

        for _ in range(self._max_pages):
            kwargs: dict[str, Any] = {"userId": "me"}
            if page_token is None:
                kwargs["startHistoryId"] = start_history_id
                kwargs["historyTypes"] = ["messageAdded"]
            else:
                kwargs["pageToken"] = page_token
            try:
                response = self._service.users().history().list(**kwargs).execute()
            except Exception as exc:  # noqa: BLE001 - preserve no partial success
                raise GmailReaderError("Gmail history retrieval failed.") from exc
            if not isinstance(response, Mapping):
                raise GmailReaderError("Gmail history response is malformed.")
            history = response.get("history", [])
            if not isinstance(history, list):
                raise GmailReaderError("Gmail history entries are malformed.")
            for entry in history:
                if not isinstance(entry, Mapping):
                    continue
                added_items = entry.get("messagesAdded", [])
                if not isinstance(added_items, list):
                    raise GmailReaderError("Gmail messagesAdded is malformed.")
                for added in added_items:
                    message = added.get("message") if isinstance(added, Mapping) else None
                    message_id = (
                        str(message.get("id", "")).strip()
                        if isinstance(message, Mapping)
                        else ""
                    )
                    if message_id and message_id not in seen_ids:
                        seen_ids.add(message_id)
                        message_ids.append(message_id)

            raw_next = response.get("nextPageToken")
            if raw_next is None or raw_next == "":
                return HistoryBatch(tuple(message_ids))
            if not isinstance(raw_next, str) or raw_next in seen_tokens:
                raise GmailReaderError(
                    "Gmail history pagination returned an invalid or repeated token."
                )
            seen_tokens.add(raw_next)
            page_token = raw_next
        raise GmailReaderError("Gmail history pagination exceeded the page limit.")

    def get_message_metadata(self, message_id: str) -> MessageMetadata:
        try:
            response = (
                self._service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["Subject", "From"],
                )
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            raise GmailReaderError("Gmail message metadata retrieval failed.") from exc
        if not isinstance(response, Mapping):
            raise GmailReaderError("Gmail message metadata response is malformed.")
        metadata = decode_message_metadata(response)
        if metadata.message_id != message_id:
            raise GmailReaderError("Gmail returned metadata for an unexpected message.")
        return metadata


def build_gmail_service_from_env(env: Mapping[str, str] | None = None) -> Any:
    """Create one Gmail service from the existing read-only OAuth settings."""

    values = os.environ if env is None else env
    client_json = str(values.get("RELAY_GMAIL_OAUTH_CLIENT_JSON", "")).strip()
    refresh_token = str(values.get("RELAY_GMAIL_OAUTH_REFRESH_TOKEN", "")).strip()
    if not client_json or not refresh_token:
        raise GmailReaderError("Gmail OAuth configuration is incomplete.")
    try:
        client_config = json.loads(client_json)
        installed = client_config.get("installed") or client_config.get("web")
        client_id = str(installed["client_id"])
        client_secret = str(installed["client_secret"])
        token_uri = str(installed.get("token_uri", "https://oauth2.googleapis.com/token"))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise GmailReaderError("Gmail OAuth client JSON is malformed.") from exc
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - container dependency
        raise GmailReaderError("Google Gmail dependencies are unavailable.") from exc
    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
        scopes=[GMAIL_READONLY_SCOPE],
    )
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def build_gmail_reader_from_env(env: Mapping[str, str] | None = None) -> RealGmailReader:
    """Create the Gmail API adapter; the first API request happens later."""

    return RealGmailReader(build_gmail_service_from_env(env))


class LazyGmailReader:
    """Defers credentials/client construction until the first request."""

    def __init__(self, factory: Callable[[], GmailReader]) -> None:
        self._factory = factory
        self._reader: GmailReader | None = None

    def _get(self) -> GmailReader:
        if self._reader is None:
            self._reader = self._factory()
        return self._reader

    def list_history(self, start_history_id: str) -> HistoryBatch:
        return self._get().list_history(start_history_id)

    def get_message_metadata(self, message_id: str) -> MessageMetadata:
        return self._get().get_message_metadata(message_id)
