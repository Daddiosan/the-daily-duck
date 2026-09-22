"""Durable cursor and relay-ledger storage for M3C.

Production uses two fixed Firestore collections. Every compare-and-set and
create-if-absent operation runs inside ``firestore.transactional``; local tests
install a controlled fake Firestore module and never contact Google Cloud.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from threading import Lock
from typing import Any, Callable, Mapping, Protocol

from .ledger import (
    ATTEMPT_ELIGIBLE_STATES,
    LEGAL_TRANSITIONS,
    LedgerStateConflict,
    RelayLedgerRecord,
    RelayLedgerState,
    utc_now_iso,
)


RELAY_CURSOR_COLLECTION = "relay_cursor"
RELAY_EVENTS_COLLECTION = "relay_events"
RELAY_WATCH_STATE_COLLECTION = "relay_watch_state"

CURSOR_PERSISTED_FIELDS = frozenset({"mailbox_hash", "history_id", "updated_at"})
EVENT_PERSISTED_FIELDS = frozenset(
    {
        "event_key",
        "stage",
        "workflow",
        "attempt_count",
        "state",
        "workflow_run_id",
        "created_at",
        "updated_at",
    }
)
WATCH_STATE_PERSISTED_FIELDS = frozenset(
    {"expiration", "history_id", "mailbox_hash", "updated_at"}
)
_MAILBOX_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")


class RelayStorageError(RuntimeError):
    """Persisted relay data is malformed or storage is unavailable."""


@dataclass(frozen=True)
class CursorRecord:
    mailbox_hash: str
    history_id: str
    updated_at: str


@dataclass(frozen=True)
class WatchStateRecord:
    expiration: int
    history_id: str
    mailbox_hash: str
    updated_at: str


class WatchStateStore(Protocol):
    def read_watch_state(self, mailbox_hash: str) -> WatchStateRecord | None: ...

    def store_watch_state_if_newer(
        self, mailbox_hash: str, history_id: str, expiration: int
    ) -> bool: ...


class CursorStore(Protocol):
    def read_cursor(self, mailbox_hash: str) -> CursorRecord | None: ...

    def compare_and_update_cursor(
        self,
        mailbox_hash: str,
        expected_history_id: str | None,
        new_history_id: str,
    ) -> bool: ...


class InMemoryCursorStore:
    """Thread-safe local/test cursor with the production CAS contract."""

    def __init__(self, *, clock: Callable[[], str] = utc_now_iso) -> None:
        self._records: dict[str, CursorRecord] = {}
        self._lock = Lock()
        self._clock = clock

    def read_cursor(self, mailbox_hash: str) -> CursorRecord | None:
        with self._lock:
            record = self._records.get(mailbox_hash)
            return replace(record) if record is not None else None

    def compare_and_update_cursor(
        self,
        mailbox_hash: str,
        expected_history_id: str | None,
        new_history_id: str,
    ) -> bool:
        _require_decimal_history_id(new_history_id)
        with self._lock:
            current = self._records.get(mailbox_hash)
            actual = current.history_id if current is not None else None
            if actual != expected_history_id:
                return False
            self._records[mailbox_hash] = CursorRecord(
                mailbox_hash=mailbox_hash,
                history_id=new_history_id,
                updated_at=self._clock(),
            )
            return True


class InMemoryWatchStateStore:
    """Thread-safe local/test store with monotonic successful-watch writes."""

    def __init__(self, *, clock: Callable[[], str] = utc_now_iso) -> None:
        self._records: dict[str, WatchStateRecord] = {}
        self._lock = Lock()
        self._clock = clock

    def read_watch_state(self, mailbox_hash: str) -> WatchStateRecord | None:
        _require_mailbox_hash(mailbox_hash)
        with self._lock:
            record = self._records.get(mailbox_hash)
            return replace(record) if record is not None else None

    def store_watch_state_if_newer(
        self, mailbox_hash: str, history_id: str, expiration: int
    ) -> bool:
        _require_mailbox_hash(mailbox_hash)
        _require_decimal_history_id(history_id)
        _require_expiration(expiration)
        with self._lock:
            current = self._records.get(mailbox_hash)
            if current is not None and not _watch_value_is_newer(
                expiration, history_id, current
            ):
                return False
            self._records[mailbox_hash] = WatchStateRecord(
                expiration=expiration,
                history_id=history_id,
                mailbox_hash=mailbox_hash,
                updated_at=self._clock(),
            )
            return True


def _require_decimal_history_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or not value.isdecimal()
    ):
        raise RelayStorageError("Persisted Gmail history id is malformed.")
    return value


def _require_mailbox_hash(value: object) -> str:
    if not isinstance(value, str) or _MAILBOX_HASH_PATTERN.fullmatch(value) is None:
        raise RelayStorageError("Persisted mailbox hash is malformed.")
    return value


def _require_expiration(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RelayStorageError("Persisted watch expiration is malformed.")
    return value


def _watch_state_from_data(
    mailbox_hash: str, data: Mapping[str, Any]
) -> WatchStateRecord:
    if set(data) != WATCH_STATE_PERSISTED_FIELDS:
        raise RelayStorageError("Persisted watch state schema is malformed.")
    if data.get("mailbox_hash") != mailbox_hash:
        raise RelayStorageError("Persisted watch state mailbox hash mismatches.")
    updated_at = data.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at:
        raise RelayStorageError("Persisted watch state timestamp is malformed.")
    return WatchStateRecord(
        expiration=_require_expiration(data.get("expiration")),
        history_id=_require_decimal_history_id(data.get("history_id")),
        mailbox_hash=_require_mailbox_hash(mailbox_hash),
        updated_at=updated_at,
    )


def _watch_value_is_newer(
    expiration: int, history_id: str, current: WatchStateRecord
) -> bool:
    return (expiration, int(history_id)) > (
        current.expiration,
        int(current.history_id),
    )


def _cursor_from_data(
    mailbox_hash: str, data: Mapping[str, Any]
) -> CursorRecord:
    if set(data) != CURSOR_PERSISTED_FIELDS:
        raise RelayStorageError("Persisted relay cursor schema is malformed.")
    if data.get("mailbox_hash") != mailbox_hash:
        raise RelayStorageError("Persisted relay cursor mailbox hash mismatches.")
    updated_at = data.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at:
        raise RelayStorageError("Persisted relay cursor timestamp is malformed.")
    return CursorRecord(
        mailbox_hash=mailbox_hash,
        history_id=_require_decimal_history_id(data.get("history_id")),
        updated_at=updated_at,
    )


def _event_to_data(record: RelayLedgerRecord) -> dict[str, Any]:
    return {
        "event_key": record.event_key,
        "stage": record.stage,
        "workflow": record.workflow,
        "attempt_count": record.attempt_count,
        "state": record.state.value,
        "workflow_run_id": record.workflow_run_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _event_from_data(event_key: str, data: Mapping[str, Any]) -> RelayLedgerRecord:
    if set(data) != EVENT_PERSISTED_FIELDS or data.get("event_key") != event_key:
        raise RelayStorageError("Persisted relay event schema is malformed.")
    stage = data.get("stage")
    workflow = data.get("workflow")
    attempt_count = data.get("attempt_count")
    run_id = data.get("workflow_run_id")
    created_at = data.get("created_at")
    updated_at = data.get("updated_at")
    if not isinstance(stage, str) or not stage:
        raise RelayStorageError("Persisted relay event stage is malformed.")
    if workflow is not None and not isinstance(workflow, str):
        raise RelayStorageError("Persisted relay event workflow is malformed.")
    if isinstance(attempt_count, bool) or not isinstance(attempt_count, int):
        raise RelayStorageError("Persisted relay attempt count is malformed.")
    if attempt_count < 1:
        raise RelayStorageError("Persisted relay attempt count is malformed.")
    if run_id is not None and not isinstance(run_id, str):
        raise RelayStorageError("Persisted relay run id is malformed.")
    if not isinstance(created_at, str) or not isinstance(updated_at, str):
        raise RelayStorageError("Persisted relay timestamps are malformed.")
    try:
        state = RelayLedgerState(data.get("state"))
    except (TypeError, ValueError) as exc:
        raise RelayStorageError("Persisted relay event state is malformed.") from exc
    return RelayLedgerRecord(
        event_key=event_key,
        stage=stage,
        workflow=workflow,
        attempt_count=attempt_count,
        state=state,
        workflow_run_id=run_id,
        created_at=created_at,
        updated_at=updated_at,
    )


class FirestoreRelayStorage:
    """Firestore-backed CursorStore and RelayLedger using true transactions."""

    def __init__(
        self,
        client: Any,
        *,
        clock: Callable[[], str] = utc_now_iso,
    ) -> None:
        self._client = client
        self._cursors = client.collection(RELAY_CURSOR_COLLECTION)
        self._events = client.collection(RELAY_EVENTS_COLLECTION)
        self._watch_states = client.collection(RELAY_WATCH_STATE_COLLECTION)
        self._clock = clock

    def read_cursor(self, mailbox_hash: str) -> CursorRecord | None:
        snapshot = self._cursors.document(mailbox_hash).get()
        if not snapshot.exists:
            return None
        return _cursor_from_data(mailbox_hash, dict(snapshot.to_dict() or {}))

    def compare_and_update_cursor(
        self,
        mailbox_hash: str,
        expected_history_id: str | None,
        new_history_id: str,
    ) -> bool:
        _require_decimal_history_id(new_history_id)
        try:
            from google.cloud import firestore
        except ImportError as exc:  # pragma: no cover - container dependency
            raise RelayStorageError("Firestore dependency is unavailable.") from exc
        reference = self._cursors.document(mailbox_hash)
        transaction = self._client.transaction()

        @firestore.transactional
        def update(txn: Any) -> bool:
            snapshot = reference.get(transaction=txn)
            if snapshot.exists:
                current = _cursor_from_data(
                    mailbox_hash, dict(snapshot.to_dict() or {})
                ).history_id
                if current != expected_history_id:
                    return False
                txn.update(
                    reference,
                    {
                        "history_id": new_history_id,
                        "updated_at": self._clock(),
                    },
                )
                return True
            if expected_history_id is not None:
                return False
            txn.create(
                reference,
                {
                    "mailbox_hash": mailbox_hash,
                    "history_id": new_history_id,
                    "updated_at": self._clock(),
                },
            )
            return True

        return bool(update(transaction))

    def read_watch_state(self, mailbox_hash: str) -> WatchStateRecord | None:
        _require_mailbox_hash(mailbox_hash)
        snapshot = self._watch_states.document(mailbox_hash).get()
        if not snapshot.exists:
            return None
        return _watch_state_from_data(mailbox_hash, dict(snapshot.to_dict() or {}))

    def store_watch_state_if_newer(
        self, mailbox_hash: str, history_id: str, expiration: int
    ) -> bool:
        _require_mailbox_hash(mailbox_hash)
        _require_decimal_history_id(history_id)
        _require_expiration(expiration)
        try:
            from google.cloud import firestore
        except ImportError as exc:  # pragma: no cover - container dependency
            raise RelayStorageError("Firestore dependency is unavailable.") from exc
        reference = self._watch_states.document(mailbox_hash)
        transaction = self._client.transaction()

        @firestore.transactional
        def update(txn: Any) -> bool:
            snapshot = reference.get(transaction=txn)
            if snapshot.exists:
                current = _watch_state_from_data(
                    mailbox_hash, dict(snapshot.to_dict() or {})
                )
                if not _watch_value_is_newer(expiration, history_id, current):
                    return False
                txn.set(
                    reference,
                    {
                        "expiration": expiration,
                        "history_id": history_id,
                        "mailbox_hash": mailbox_hash,
                        "updated_at": self._clock(),
                    },
                )
                return True
            txn.create(
                reference,
                {
                    "expiration": expiration,
                    "history_id": history_id,
                    "mailbox_hash": mailbox_hash,
                    "updated_at": self._clock(),
                },
            )
            return True

        return bool(update(transaction))

    def get(self, event_key: str) -> RelayLedgerRecord | None:
        snapshot = self._events.document(event_key).get()
        if not snapshot.exists:
            return None
        return _event_from_data(event_key, dict(snapshot.to_dict() or {}))

    def reserve_new(
        self, event_key: str, *, stage: str, workflow: str | None, now: str
    ) -> RelayLedgerRecord | None:
        try:
            from google.cloud import firestore
        except ImportError as exc:  # pragma: no cover - container dependency
            raise RelayStorageError("Firestore dependency is unavailable.") from exc
        reference = self._events.document(event_key)
        transaction = self._client.transaction()
        record = RelayLedgerRecord(
            event_key=event_key,
            stage=stage,
            workflow=workflow,
            attempt_count=1,
            state=RelayLedgerState.RECEIVED,
            workflow_run_id=None,
            created_at=now,
            updated_at=now,
        )

        @firestore.transactional
        def reserve(txn: Any) -> RelayLedgerRecord | None:
            if reference.get(transaction=txn).exists:
                return None
            txn.create(reference, _event_to_data(record))
            return record

        return reserve(transaction)

    def begin_attempt(self, event_key: str, *, now: str) -> RelayLedgerRecord | None:
        try:
            from google.cloud import firestore
        except ImportError as exc:  # pragma: no cover - container dependency
            raise RelayStorageError("Firestore dependency is unavailable.") from exc
        reference = self._events.document(event_key)
        transaction = self._client.transaction()

        @firestore.transactional
        def begin(txn: Any) -> RelayLedgerRecord | None:
            snapshot = reference.get(transaction=txn)
            if not snapshot.exists:
                return None
            record = _event_from_data(event_key, dict(snapshot.to_dict() or {}))
            if record.state not in ATTEMPT_ELIGIBLE_STATES:
                return None
            updated = replace(
                record,
                state=RelayLedgerState.DISPATCH_ATTEMPTING,
                attempt_count=(
                    record.attempt_count + 1
                    if record.state is RelayLedgerState.SAFE_TO_RETRY
                    else record.attempt_count
                ),
                updated_at=now,
            )
            txn.set(reference, _event_to_data(updated))
            return updated

        return begin(transaction)

    def set_state(
        self,
        event_key: str,
        *,
        expected_state: RelayLedgerState,
        next_state: RelayLedgerState,
        now: str,
        workflow_run_id: str | None = None,
    ) -> RelayLedgerRecord:
        if (expected_state, next_state) not in LEGAL_TRANSITIONS:
            raise LedgerStateConflict(
                f"Illegal transition {expected_state.value} -> {next_state.value}."
            )
        try:
            from google.cloud import firestore
        except ImportError as exc:  # pragma: no cover - container dependency
            raise RelayStorageError("Firestore dependency is unavailable.") from exc
        reference = self._events.document(event_key)
        transaction = self._client.transaction()

        @firestore.transactional
        def change(txn: Any) -> RelayLedgerRecord:
            snapshot = reference.get(transaction=txn)
            if not snapshot.exists:
                raise LedgerStateConflict("Relay event does not exist.")
            record = _event_from_data(event_key, dict(snapshot.to_dict() or {}))
            if record.state is not expected_state:
                raise LedgerStateConflict(
                    f"event_key expected_state={expected_state.value} "
                    f"but actual={record.state.value}."
                )
            updated = replace(
                record,
                state=next_state,
                updated_at=now,
                workflow_run_id=(
                    workflow_run_id
                    if workflow_run_id is not None
                    else record.workflow_run_id
                ),
            )
            txn.set(reference, _event_to_data(updated))
            return updated

        return change(transaction)


def build_firestore_storage(project: str) -> FirestoreRelayStorage:
    """Construct the production client; no read/write happens here."""

    if not project:
        raise RelayStorageError("Firestore project is required.")
    try:
        from google.cloud import firestore
    except ImportError as exc:  # pragma: no cover - container dependency
        raise RelayStorageError("Firestore dependency is unavailable.") from exc
    return FirestoreRelayStorage(firestore.Client(project=project))
