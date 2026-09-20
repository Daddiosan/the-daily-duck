"""Read-only Gmail API adapter for the Phase 3B-2A1 receiver."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Protocol


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class GmailClientError(RuntimeError):
    """Base error for controlled Gmail API failures."""


class StaleHistoryError(GmailClientError):
    """The saved Gmail history cursor is no longer available."""


@dataclass(frozen=True)
class HistoryBatch:
    """Changed Gmail message IDs returned by one complete history walk."""

    message_ids: tuple[str, ...]
    latest_history_id: str | None = None


class GmailReader(Protocol):
    """Narrow read/watch contract used by the receiver."""

    def watch(self, topic_name: str) -> Mapping[str, Any]: ...

    def list_history(self, start_history_id: str) -> HistoryBatch: ...

    def get_message(self, message_id: str) -> Mapping[str, Any]: ...

    def list_messages(self, query: str, max_results: int) -> tuple[str, ...]: ...

    def get_profile(self) -> Mapping[str, Any]: ...


def _http_status(exc: BaseException) -> int | None:
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


class GmailClient:
    """Gmail API wrapper exposing only watch and read methods."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def watch(self, topic_name: str) -> Mapping[str, Any]:
        result = (
            self._service.users()
            .watch(
                userId="me",
                body={"topicName": topic_name, "labelIds": ["INBOX"]},
            )
            .execute()
        )
        return dict(result or {})

    def list_history(self, start_history_id: str) -> HistoryBatch:
        page_token: str | None = None
        message_ids: list[str] = []
        seen: set[str] = set()
        latest_history_id: str | None = None
        try:
            while True:
                request = self._service.users().history().list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                    pageToken=page_token,
                )
                result = dict(request.execute() or {})
                raw_latest = result.get("historyId")
                if raw_latest is not None:
                    latest_history_id = str(raw_latest)
                for history in result.get("history", ()):
                    candidates = list(history.get("messagesAdded", ()))
                    if not candidates:
                        candidates = [
                            {"message": message}
                            for message in history.get("messages", ())
                        ]
                    for candidate in candidates:
                        message = candidate.get("message", {})
                        message_id = str(message.get("id", "")).strip()
                        if message_id and message_id not in seen:
                            seen.add(message_id)
                            message_ids.append(message_id)
                page_token = result.get("nextPageToken")
                if not page_token:
                    break
        except Exception as exc:
            if _http_status(exc) == 404:
                raise StaleHistoryError("Gmail history cursor is stale.") from exc
            raise GmailClientError("Gmail history lookup failed.") from exc
        return HistoryBatch(tuple(message_ids), latest_history_id)

    def get_message(self, message_id: str) -> Mapping[str, Any]:
        try:
            result = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="raw")
                .execute()
            )
        except Exception as exc:
            raise GmailClientError("Gmail message fetch failed.") from exc
        return dict(result or {})

    def list_messages(self, query: str, max_results: int) -> tuple[str, ...]:
        if max_results < 1:
            return ()
        page_token: str | None = None
        ids: list[str] = []
        seen: set[str] = set()
        try:
            while len(ids) < max_results:
                result = dict(
                    self._service.users()
                    .messages()
                    .list(
                        userId="me",
                        q=query,
                        labelIds=["INBOX"],
                        includeSpamTrash=False,
                        maxResults=min(100, max_results - len(ids)),
                        pageToken=page_token,
                    )
                    .execute()
                    or {}
                )
                for message in result.get("messages", ()):
                    message_id = str(message.get("id", "")).strip()
                    if message_id and message_id not in seen:
                        seen.add(message_id)
                        ids.append(message_id)
                        if len(ids) >= max_results:
                            break
                page_token = result.get("nextPageToken")
                if not page_token:
                    break
        except Exception as exc:
            raise GmailClientError("Bounded Gmail message search failed.") from exc
        return tuple(ids)

    def get_profile(self) -> Mapping[str, Any]:
        try:
            result = self._service.users().getProfile(userId="me").execute()
        except Exception as exc:
            raise GmailClientError("Gmail profile lookup failed.") from exc
        return dict(result or {})


def build_gmail_client_from_env(
    env: Mapping[str, str] | None = None,
) -> GmailClient:
    """Build a Gmail reader without logging any OAuth material."""

    values = os.environ if env is None else env
    client_json = str(values.get("GMAIL_OAUTH_CLIENT_JSON", "")).strip()
    refresh_token = str(values.get("GMAIL_OAUTH_REFRESH_TOKEN", "")).strip()
    if not client_json or not refresh_token:
        raise GmailClientError("Gmail OAuth configuration is incomplete.")
    try:
        client_config = json.loads(client_json)
        installed = client_config.get("installed") or client_config.get("web")
        client_id = str(installed["client_id"])
        client_secret = str(installed["client_secret"])
        token_uri = str(
            installed.get("token_uri", "https://oauth2.googleapis.com/token")
        )
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise GmailClientError("Gmail OAuth client JSON is malformed.") from exc

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=token_uri,
            client_id=client_id,
            client_secret=client_secret,
            scopes=[GMAIL_READONLY_SCOPE],
        )
        service = build(
            "gmail", "v1", credentials=credentials, cache_discovery=False
        )
    except ImportError as exc:  # pragma: no cover - deployment dependency check
        raise GmailClientError("Google Gmail dependencies are unavailable.") from exc
    return GmailClient(service)
