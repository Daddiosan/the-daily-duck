"""Storage for cloud/approval_dispatcher (A2): cursor, shadow observations,
transition ledger.

For local tests and the primary local implementation, scripts.a2_dispatch's
InMemoryMessageDedupeStore and InMemoryTransitionLedger are reused directly
(imported, not reimplemented) as A2's message-level and transition-level
stores: they already correctly implement the exact Protocols
scripts.a2_dispatch.process_gmail_event requires. Reimplementing them here
would be exactly the kind of needless duplication the Phase B.7 review
warned against. This module adds only what is genuinely A2-specific:
cursor persistence for A2's own independent Gmail history walk, a sanitized
shadow-record builder, and a Firestore-shaped implementation of the same
Protocols for the eventual production deployment.

The Firestore-backed class below never imports google.cloud.firestore at
module load time, only inside methods -- mirroring
cloud/approval_receiver/observation.py's own convention -- so this module
can always be imported even where that dependency is not installed. It is
never exercised against a real Firestore in this repository; see
tests/test_approval_dispatcher.py, which tests it against a fake Firestore
module installed into sys.modules, mirroring the proven technique in
tests/test_approval_receiver_firestore.py.

Phase C shadow mode never reaches DispatchOutcomeState's UNKNOWN_OUTCOME or
FAILED_FINAL branches in practice, because the shadow dispatch adapter
(scripts.a2_dispatch.FakeDispatchAdapter, used as-is -- see main.py) never
fails by default. The Firestore transition-ledger implementation below
still implements the full TransitionLedger Protocol correctly (so
process_gmail_event's control flow is unmodified), but persists only a
minimal reserved/confirmed concept rather than the full five-state
production model -- building that out now, before any real dispatch
exists, would be premature (see docs/phase3b2/A2_SHADOW_RUNBOOK.md).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Mapping, Protocol

try:
    from scripts.a2_dispatch import A2Outcome, DispatchOutcomeState
except ModuleNotFoundError:  # pragma: no cover - direct script invocation
    from a2_dispatch import A2Outcome, DispatchOutcomeState  # type: ignore


@dataclass(frozen=True)
class CursorState:
    """A2's own Gmail history-walk cursor. Independent of A1's cursor:
    each service tracks its own progress through the same Gmail mailbox's
    history, via its own Pub/Sub subscription on the shared topic."""

    processing_history_id: str | None = None


class CursorStore(Protocol):
    def read_cursor(self) -> CursorState: ...

    def compare_and_update_cursor(
        self, expected_history_id: str | None, new_history_id: str
    ) -> bool: ...


class InMemoryCursorStore:
    """Fake cursor store for local tests. Not durable; carries no cloud
    dependency."""

    def __init__(self, cursor: CursorState | None = None) -> None:
        self._cursor = cursor or CursorState()
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
            self._cursor = CursorState(processing_history_id=str(new_history_id))
            return True


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class InMemoryShadowObservationStore:
    """Implements scripts.a2_dispatch.MessageDedupeStore, plus a
    shadow-record enrichment write that Protocol does not define.

    process_gmail_event() itself only ever writes a minimal
    {"gmail_message_id": ...} payload via insert_observation_if_absent
    (see scripts/a2_dispatch.py, unmodified). A single object of this
    class serves both as the dedupe gate that call requires and as the
    persistence target for A2's own richer sanitized shadow schema, keyed
    by the same observation_id, via the separate write_shadow_record call
    main.py makes immediately afterward -- without needing to modify
    scripts/a2_dispatch.py to support enrichment.
    """

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._lock = Lock()

    def contains(self, observation_id: str) -> bool:
        with self._lock:
            return observation_id in self._records

    def insert_observation_if_absent(
        self, observation_id: str, observation: Mapping[str, object]
    ) -> bool:
        with self._lock:
            if observation_id in self._records:
                return False
            self._records[observation_id] = dict(observation)
            return True

    def write_shadow_record(
        self, observation_id: str, record: Mapping[str, Any]
    ) -> None:
        with self._lock:
            existing = self._records.get(observation_id, {})
            self._records[observation_id] = {**existing, **dict(record)}

    def get_shadow_record(self, observation_id: str) -> Mapping[str, Any] | None:
        with self._lock:
            record = self._records.get(observation_id)
            return dict(record) if record is not None else None


def build_shadow_record(
    *,
    observation_id: str,
    outcome: A2Outcome,
    timestamp: str,
) -> dict[str, Any]:
    """The exact, minimal sanitized fields persisted for one classified
    Gmail message.

    Never includes: full email body, quoted history, full subject, sender
    email address, or any credential. command.authorized_principal (the
    sender address, already allowlist-validated) exists on outcome.command
    but is deliberately never read here.
    """

    command = outcome.command
    return {
        "observation_id": observation_id,
        "stage": command.stage.value if command is not None else None,
        "issue_date": command.issue_date if command is not None else None,
        "normalized_command": command.command if command is not None else None,
        "idempotency_key": command.idempotency_key if command is not None else None,
        "transition_key": command.transition_key if command is not None else None,
        "classification": outcome.classification.value,
        "timestamp": timestamp,
        "source_type": "GMAIL_PUSH",
    }


class FirestoreDispatcherStorage:
    """Firestore-backed CursorStore + MessageDedupeStore + TransitionLedger
    for A2. Mirrors cloud/approval_receiver/observation.py's transactional
    check-and-set pattern.
    """

    def __init__(
        self,
        client: Any,
        *,
        cursor_collection: str,
        cursor_document: str,
        observation_collection: str,
        transition_collection: str,
    ) -> None:
        self._client = client
        self._cursor_ref = client.collection(cursor_collection).document(
            cursor_document
        )
        self._observations = client.collection(observation_collection)
        self._transitions = client.collection(transition_collection)

    # -- CursorStore --

    def read_cursor(self) -> CursorState:
        snapshot = self._cursor_ref.get()
        data = dict(snapshot.to_dict() or {}) if snapshot.exists else {}
        value = data.get("processing_history_id")
        return CursorState(
            processing_history_id=str(value) if value is not None else None
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
            current = data.get("processing_history_id")
            current = str(current) if current is not None else None
            if current != expected_history_id:
                return False
            txn.set(
                self._cursor_ref,
                {"processing_history_id": str(new_history_id)},
                merge=True,
            )
            return True

        return bool(update(transaction))

    # -- MessageDedupeStore (scripts.a2_dispatch.MessageDedupeStore) --

    def contains(self, observation_id: str) -> bool:
        return self._observations.document(observation_id).get().exists

    def insert_observation_if_absent(
        self, observation_id: str, observation: Mapping[str, object]
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

    def write_shadow_record(
        self, observation_id: str, record: Mapping[str, Any]
    ) -> None:
        """Enrichment write, not part of scripts.a2_dispatch.MessageDedupeStore.
        Called by main.py immediately after a fresh (non-duplicate)
        process_gmail_event() call, merging A2's sanitized shadow schema
        onto the same document insert_observation_if_absent already
        created."""

        self._observations.document(observation_id).set(dict(record), merge=True)

    def get_shadow_record(self, observation_id: str) -> Mapping[str, Any] | None:
        snapshot = self._observations.document(observation_id).get()
        return dict(snapshot.to_dict() or {}) if snapshot.exists else None

    # -- TransitionLedger (scripts.a2_dispatch.TransitionLedger) --
    # Minimal representation only: {"confirmed": bool}. See module
    # docstring for why the full five-state production model is not
    # persisted here.

    def reserve_if_absent(self, transition_key: str) -> bool:
        try:
            from google.cloud import firestore
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("Firestore dependency is unavailable.") from exc

        reference = self._transitions.document(transition_key)
        transaction = self._client.transaction()

        @firestore.transactional
        def reserve(txn: Any) -> bool:
            snapshot = reference.get(transaction=txn)
            if snapshot.exists:
                return False
            txn.create(reference, {"confirmed": False})
            return True

        return bool(reserve(transaction))

    def mark_attempted(self, transition_key: str) -> None:
        # Ephemeral, in-process bookkeeping only. A crash between
        # reserve_if_absent and mark_confirmed is safely recoverable by
        # reprocessing in shadow mode (worst case: a redundant shadow
        # record, never a real side effect), so no durable write happens
        # here.
        return None

    def mark_confirmed(self, transition_key: str) -> None:
        self._transitions.document(transition_key).set(
            {"confirmed": True}, merge=True
        )

    def mark_unknown_outcome(self, transition_key: str) -> None:
        # Not expected to be reached in shadow mode (the shadow dispatch
        # adapter never fails); implemented for Protocol completeness.
        self._transitions.document(transition_key).set(
            {"confirmed": False, "unknown_outcome": True}, merge=True
        )

    def release(self, transition_key: str) -> None:
        self._transitions.document(transition_key).delete()

    def state_of(self, transition_key: str) -> DispatchOutcomeState | None:
        snapshot = self._transitions.document(transition_key).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        if data.get("confirmed"):
            return DispatchOutcomeState.CONFIRMED
        if data.get("unknown_outcome"):
            return DispatchOutcomeState.UNKNOWN_OUTCOME
        return DispatchOutcomeState.PENDING
