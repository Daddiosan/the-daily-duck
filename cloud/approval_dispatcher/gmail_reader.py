"""Minimal A2-specific Gmail reader for cloud/approval_dispatcher (A2).

Deliberately NOT a copy of cloud/approval_receiver/gmail_client.py: A2 is an
independent sibling service (see docs/phase3b2/A2_SHADOW_RUNBOOK.md and the
Phase B.7 design review) and must not import from or depend on
cloud/approval_receiver/'s implementation files -- tests/
test_approval_receiver_contract.py enforces that directory as an exact,
isolated file set, and tests/test_approval_dispatcher_contract.py enforces
the same for this one.

This module implements only the narrow read surface A2's shadow
classification needs: walk Gmail history since a cursor to find changed
message ids, and fetch one message's raw content. This is Gmail API
plumbing, not approval-command parsing -- all command extraction and
validation continues to go through scripts.approval_domain and
scripts.a2_dispatch exclusively. Duplicating this thin, rarely-changing API
wrapper was a deliberate Phase B.7 decision; duplicating the parser was not
and does not happen anywhere in this module.

No periodic polling: this module only reacts to a single Pub/Sub-delivered
notification's history id at a time (see main.py's DispatcherService). It
never loops or schedules its own Gmail calls.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from typing import Any, Mapping, Protocol


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class GmailReaderError(RuntimeError):
    """Base error for controlled Gmail API failures."""


class TemporaryGmailError(GmailReaderError):
    """Retryable: rate limiting, transient network/server errors."""


class PermanentGmailError(GmailReaderError):
    """Non-retryable: authentication failure, invalid configuration.

    Must never be retried automatically -- see main.py's ack/nack mapping,
    which acknowledges these to avoid an infinite Pub/Sub redelivery loop
    while still surfacing them for a human/reconciliation to notice.
    """


class StaleHistoryError(GmailReaderError):
    """The saved history cursor is no longer valid; a full resync against
    the current Gmail profile historyId is required."""


@dataclass(frozen=True)
class HistoryBatch:
    """Changed Gmail message ids returned by one history walk."""

    message_ids: tuple[str, ...]
    latest_history_id: str | None = None


class GmailReader(Protocol):
    """The only Gmail operations A2 needs. Read-only by construction: no
    method here can mutate mailbox state."""

    def get_profile(self) -> Mapping[str, Any]: ...

    def list_history(self, start_history_id: str) -> HistoryBatch: ...

    def get_message(self, message_id: str) -> Mapping[str, Any]: ...


class FakeGmailReader:
    """Test double. Never performs network I/O.

    results_by_history_id maps a starting history id to either a
    HistoryBatch or an exception instance to raise, so tests can script
    multi-call sequences (e.g. a StaleHistoryError followed by a
    successful full-resync path).
    """

    def __init__(
        self,
        *,
        profile_history_id: str = "100",
        history_results: Mapping[str, HistoryBatch | Exception] | None = None,
        messages: Mapping[str, Mapping[str, Any]] | None = None,
        message_errors: Mapping[str, Exception] | None = None,
        profile_error: Exception | None = None,
    ) -> None:
        self._profile_history_id = profile_history_id
        self._profile_error = profile_error
        self._history_results = dict(history_results or {})
        self._messages = dict(messages or {})
        self._message_errors = dict(message_errors or {})
        self.get_message_calls: list[str] = []
        self.list_history_calls: list[str] = []

    def get_profile(self) -> Mapping[str, Any]:
        if self._profile_error is not None:
            raise self._profile_error
        return {"historyId": self._profile_history_id}

    def list_history(self, start_history_id: str) -> HistoryBatch:
        self.list_history_calls.append(start_history_id)
        result = self._history_results.get(start_history_id)
        if isinstance(result, Exception):
            raise result
        if result is not None:
            return result
        return HistoryBatch(message_ids=())

    def get_message(self, message_id: str) -> Mapping[str, Any]:
        self.get_message_calls.append(message_id)
        if message_id in self._message_errors:
            raise self._message_errors[message_id]
        return self._messages[message_id]


@dataclass(frozen=True)
class DecodedMessageFields:
    """MIME-decoded fields extracted from one raw Gmail message resource.

    This is RFC822/MIME decoding via Python's standard email library --
    not approval-command parsing. It exists only to produce the plaintext
    a2_dispatch.FetchedGmailMessage needs; the wire-command text inside
    body still goes through scripts.approval_domain's extractors
    unchanged.
    """

    sender: str
    subject: str
    body: str


def _decode_raw(raw: object) -> bytes:
    if not isinstance(raw, str) or not raw:
        raise GmailReaderError("Gmail message has no raw RFC822 payload.")
    try:
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, TypeError) as exc:
        raise GmailReaderError("Gmail raw payload is malformed.") from exc


def _plain_body(message: Any) -> str:
    parts = message.walk() if message.is_multipart() else (message,)
    values: list[str] = []
    for part in parts:
        if part.is_multipart():
            continue
        if part.get_content_type() != "text/plain":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace")
        values.append(str(content))
    return "\n".join(values)


def decode_message_fields(gmail_message: Mapping[str, Any]) -> DecodedMessageFields:
    """Decode sender/subject/plaintext-body from a raw Gmail message
    resource. Never returns quoted-history stripping or command parsing;
    that remains scripts.approval_domain's job."""

    parsed = BytesParser(policy=policy.default).parsebytes(
        _decode_raw(gmail_message.get("raw"))
    )
    sender = parseaddr(str(parsed.get("From", "")))[1].strip().lower()
    subject = str(parsed.get("Subject", ""))
    body = _plain_body(parsed)
    return DecodedMessageFields(sender=sender, subject=subject, body=body)


def _http_status(exc: BaseException) -> int | None:
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


class RealGmailReader:
    """Production Gmail API reader. Never constructed or called by any
    test in this repository -- see
    tests/test_approval_dispatcher_contract.py's real-network prohibition
    and A2_SHADOW_RUNBOOK.md. Mirrors
    cloud/approval_receiver/gmail_client.py's GmailClient error-mapping
    shape, but is an independent implementation (see this module's
    docstring for why: A2 must not import from cloud.approval_receiver).
    """

    def __init__(self, service: Any) -> None:
        self._service = service

    def get_profile(self) -> Mapping[str, Any]:
        try:
            result = self._service.users().getProfile(userId="me").execute()
        except Exception as exc:  # noqa: BLE001 - mapped below
            status = _http_status(exc)
            if status in (401, 403):
                raise PermanentGmailError("Gmail profile lookup was refused.") from exc
            raise TemporaryGmailError("Gmail profile lookup failed.") from exc
        return dict(result or {})

    def list_history(self, start_history_id: str) -> HistoryBatch:
        try:
            response = (
                self._service.users()
                .history()
                .list(userId="me", startHistoryId=start_history_id)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 - mapped below
            status = _http_status(exc)
            if status == 404:
                raise StaleHistoryError("Gmail history cursor is no longer valid.") from exc
            if status in (401, 403):
                raise PermanentGmailError("Gmail history list was refused.") from exc
            raise TemporaryGmailError("Gmail history list failed.") from exc

        message_ids: list[str] = []
        for entry in response.get("history", []) or []:
            for added in entry.get("messagesAdded", []) or []:
                message = added.get("message") or {}
                message_id = message.get("id")
                if message_id:
                    message_ids.append(str(message_id))
        latest = response.get("historyId")
        return HistoryBatch(
            message_ids=tuple(dict.fromkeys(message_ids)),
            latest_history_id=str(latest) if latest is not None else None,
        )

    def get_message(self, message_id: str) -> Mapping[str, Any]:
        try:
            result = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="raw")
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 - mapped below
            status = _http_status(exc)
            if status in (401, 403):
                raise PermanentGmailError("Gmail message fetch was refused.") from exc
            raise TemporaryGmailError("Gmail message fetch failed.") from exc
        return dict(result or {})


def build_gmail_reader_from_env(
    env: Mapping[str, str] | None = None,
) -> RealGmailReader:
    """Build a production Gmail reader without logging any OAuth material.

    Reads A2_GMAIL_OAUTH_CLIENT_JSON / A2_GMAIL_OAUTH_REFRESH_TOKEN --
    per the Phase B.7/B.8 credential decision, these hold the SAME
    underlying client/refresh-token values as A1's
    GMAIL_OAUTH_CLIENT_JSON / GMAIL_OAUTH_REFRESH_TOKEN during Phase C
    shadow, stored in a separate Secret Manager secret with its own IAM
    grant scoped to A2's runtime service account only. Never constructed
    or called by any test in this repository.
    """

    values = os.environ if env is None else env
    client_json = str(values.get("A2_GMAIL_OAUTH_CLIENT_JSON", "")).strip()
    refresh_token = str(values.get("A2_GMAIL_OAUTH_REFRESH_TOKEN", "")).strip()
    if not client_json or not refresh_token:
        raise GmailReaderError("Gmail OAuth configuration is incomplete.")
    try:
        client_config = json.loads(client_json)
        installed = client_config.get("installed") or client_config.get("web")
        client_id = str(installed["client_id"])
        client_secret = str(installed["client_secret"])
        token_uri = str(
            installed.get("token_uri", "https://oauth2.googleapis.com/token")
        )
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise GmailReaderError("Gmail OAuth client JSON is malformed.") from exc

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise GmailReaderError("Google Gmail dependencies are unavailable.") from exc

    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
        scopes=[GMAIL_READONLY_SCOPE],
    )
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    return RealGmailReader(service)
