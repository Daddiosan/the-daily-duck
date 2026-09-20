"""Sanitized observations and persistence contracts for Phase 3B-2A1."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from threading import Lock
from typing import Any, Callable, Mapping, Protocol


SCHEMA_VERSION = "phase3b2-a1-gmail-observation/v1"
RECOVERY_PATHS = frozenset({"PUSH", "CATCHUP", "FULL_RESYNC"})


class ObservationError(ValueError):
    """The fetched Gmail message cannot form a safe A1 observation."""


@dataclass(frozen=True)
class CursorState:
    processing_history_id: str | None = None
    watch_history_id: str | None = None
    watch_expiration_ms: int | None = None


class ObservationStore(Protocol):
    def read_cursor(self) -> CursorState: ...

    def compare_and_update_cursor(
        self, expected_history_id: str | None, new_history_id: str
    ) -> bool: ...

    def update_watch(self, history_id: str, expiration_ms: int) -> None: ...

    def insert_observation_if_absent(
        self, observation_id: str, observation: Mapping[str, Any]
    ) -> bool: ...


class InMemoryObservationStore:
    """Thread-safe fake used by unit tests; mirrors the Firestore contract."""

    def __init__(self, cursor: CursorState | None = None) -> None:
        self._cursor = cursor or CursorState()
        self.observations: dict[str, dict[str, Any]] = {}
        self._lock = Lock()

    def read_cursor(self) -> CursorState:
        with self._lock:
            return replace(self._cursor)

    def compare_and_update_cursor(
        self, expected_history_id: str | None, new_history_id: str
    ) -> bool:
        with self._lock:
            if self._cursor.processing_history_id != expected_history_id:
                return False
            self._cursor = replace(
                self._cursor, processing_history_id=str(new_history_id)
            )
            return True

    def update_watch(self, history_id: str, expiration_ms: int) -> None:
        with self._lock:
            self._cursor = replace(
                self._cursor,
                watch_history_id=str(history_id),
                watch_expiration_ms=int(expiration_ms),
            )

    def insert_observation_if_absent(
        self, observation_id: str, observation: Mapping[str, Any]
    ) -> bool:
        with self._lock:
            if observation_id in self.observations:
                return False
            self.observations[observation_id] = dict(observation)
            return True


class FirestoreObservationStore:
    """Firestore-backed cursor and idempotent observation store."""

    def __init__(
        self,
        client: Any,
        *,
        cursor_collection: str,
        cursor_document: str,
        observation_collection: str,
    ) -> None:
        self._client = client
        self._cursor_ref = client.collection(cursor_collection).document(
            cursor_document
        )
        self._observations = client.collection(observation_collection)

    def read_cursor(self) -> CursorState:
        snapshot = self._cursor_ref.get()
        data = dict(snapshot.to_dict() or {}) if snapshot.exists else {}
        return CursorState(
            processing_history_id=_optional_text(
                data.get("processing_history_id")
            ),
            watch_history_id=_optional_text(data.get("watch_history_id")),
            watch_expiration_ms=_optional_int(data.get("watch_expiration_ms")),
        )

    def compare_and_update_cursor(
        self, expected_history_id: str | None, new_history_id: str
    ) -> bool:
        try:
            from google.cloud import firestore
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("Firestore dependency is unavailable.") from exc
        transaction = self._client.transaction()

        @firestore.transactional
        def update(txn: Any) -> bool:
            snapshot = self._cursor_ref.get(transaction=txn)
            data = dict(snapshot.to_dict() or {}) if snapshot.exists else {}
            current = _optional_text(data.get("processing_history_id"))
            if current != expected_history_id:
                return False
            txn.set(
                self._cursor_ref,
                {
                    "processing_history_id": str(new_history_id),
                    "processing_cursor_updated_at": _utc_now(),
                },
                merge=True,
            )
            return True

        return bool(update(transaction))

    def update_watch(self, history_id: str, expiration_ms: int) -> None:
        self._cursor_ref.set(
            {
                "watch_history_id": str(history_id),
                "watch_expiration_ms": int(expiration_ms),
                "watch_updated_at": _utc_now(),
            },
            merge=True,
        )

    def insert_observation_if_absent(
        self, observation_id: str, observation: Mapping[str, Any]
    ) -> bool:
        try:
            from google.cloud import firestore
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("Firestore dependency is unavailable.") from exc
        reference = self._observations.document(observation_id)
        transaction = self._client.transaction()

        @firestore.transactional
        def insert(txn: Any) -> bool:
            snapshot = reference.get(transaction=txn)
            if snapshot.exists:
                return False
            txn.create(reference, dict(observation))
            return True

        return bool(insert(transaction))


def build_firestore_store(
    *,
    cursor_collection: str,
    cursor_document: str,
    observation_collection: str,
) -> FirestoreObservationStore:
    try:
        from google.cloud import firestore
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("Firestore dependency is unavailable.") from exc
    return FirestoreObservationStore(
        firestore.Client(),
        cursor_collection=cursor_collection,
        cursor_document=cursor_document,
        observation_collection=observation_collection,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _decode_raw(raw: object) -> bytes:
    if not isinstance(raw, str) or not raw:
        raise ObservationError("Gmail message has no raw RFC822 payload.")
    try:
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, TypeError) as exc:
        raise ObservationError("Gmail raw payload is malformed.") from exc


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


def _authentication_result(message: Any) -> bool | None:
    values = [str(value).lower() for value in message.get_all(
        "Authentication-Results", []
    )]
    if not values:
        return None
    joined = "\n".join(values)
    if "dkim=pass" in joined or "dmarc=pass" in joined:
        return True
    if "dkim=" in joined or "dmarc=" in joined:
        return False
    return None


def _reply_relationship(message: Any) -> bool | None:
    in_reply_to = str(message.get("In-Reply-To", "")).strip()
    references = str(message.get("References", "")).strip()
    if not in_reply_to and not references:
        return None
    if in_reply_to and references:
        return in_reply_to in references
    return None


def create_sanitized_observation(
    gmail_message: Mapping[str, Any],
    *,
    mailbox_identity: str,
    allowed_senders: tuple[str, ...],
    subject_patterns: tuple[str, ...],
    history_id: str,
    notification_received_at: str,
    recovery_path: str,
    clock: Callable[[], str] = _utc_now,
) -> dict[str, Any]:
    """Create an evidence-only record without retaining message content."""

    if recovery_path not in RECOVERY_PATHS:
        raise ObservationError("Unsupported recovery path.")
    gmail_message_id = _optional_text(gmail_message.get("id"))
    thread_id = _optional_text(gmail_message.get("threadId"))
    internal_date = _optional_text(gmail_message.get("internalDate"))
    if not gmail_message_id or not thread_id or not internal_date:
        raise ObservationError("Gmail message identity is incomplete.")

    fetched_at = clock()
    parsed = BytesParser(policy=policy.default).parsebytes(
        _decode_raw(gmail_message.get("raw"))
    )
    sender = parseaddr(str(parsed.get("From", "")))[1].strip().lower()
    subject = str(parsed.get("Subject", ""))
    body = _plain_body(parsed)
    message_id_header = str(parsed.get("Message-ID", "")).strip().lower()
    mailbox_hash = hashlib.sha256(
        mailbox_identity.strip().lower().encode("utf-8")
    ).hexdigest()
    observation_id = hashlib.sha256(
        f"{mailbox_hash}:{gmail_message_id}".encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "observation_id": observation_id,
        "gmail_message_id": gmail_message_id,
        "message_id_header_hash": (
            hashlib.sha256(message_id_header.encode("utf-8")).hexdigest()
            if message_id_header
            else None
        ),
        "thread_id": thread_id,
        "history_id": str(history_id),
        "internal_date": internal_date,
        "notification_received_at": notification_received_at,
        "message_fetched_at": fetched_at,
        "observation_recorded_at": clock(),
        "from_allowlist_match": sender in set(allowed_senders),
        "subject_match": any(pattern in subject for pattern in subject_patterns),
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "dkim_or_dmarc_pass": _authentication_result(parsed),
        "reply_thread_match": _reply_relationship(parsed),
        "recovery_path": recovery_path,
        "duplicate": False,
    }
