"""Event/transition ledger contract and in-memory test implementation.

Deliberately NOT scripts.a2_dispatch.InMemoryMessageDedupeStore /
InMemoryTransitionLedger, and not proof by way of them: those two classes'
own docstrings already disclose they hold no lock at all and are "not
durable" / unsafe "from separate processes" (scripts/a2_dispatch.py's
InMemoryTransitionLedger docstring, PRODUCTION_CAS_REQUIREMENT). Reusing
them here would prove nothing about this relay's own concurrency safety
requirement (task spec Sec. 8/12) and would silently inherit a known-unsafe
implementation as if it were validated. This module defines its own
RelayLedger Protocol with explicit atomic CAS semantics, and its own
InMemoryRelayLedger, genuinely thread-safe via a real threading.Lock (the
same technique cloud/approval_receiver/observation.py's
InMemoryObservationStore and cloud/approval_dispatcher/storage.py's
InMemoryCursorStore already use and have already been proven correct under
real concurrent threads by tests/test_approval_dispatcher.py's
RealConcurrentCursorCasTests).

The production Firestore implementation lives in storage.py. Every mutation
there is one transaction against the deterministic event-key document.

Persisted fields are exactly the minimum the task spec lists: event_key,
stage, workflow, attempt_count, state, workflow_run_id, created_at,
updated_at. Never the Gmail message subject, body, or sender address.

State machine
-------------

RECEIVED            -- event reserved; no dispatch attempt made yet.
DISPATCH_ATTEMPTING -- exactly one dispatch attempt is in flight for this
                       event_key right now (or crashed mid-flight -- see
                       below). Acts as a mutex: begin_attempt() can only
                       ever succeed starting from RECEIVED or
                       SAFE_TO_RETRY, so at most one caller at a time (and
                       across any number of redeliveries) can hold this
                       state for a given event_key.
DISPATCH_CONFIRMED  -- terminal. GitHub confirmed the dispatch.
SAFE_TO_RETRY       -- a clear, retryable failure was recorded and the
                       business attempt budget is not exhausted; exactly
                       one more begin_attempt() may proceed.
UNKNOWN_OUTCOME     -- terminal (until a future, separately-approved
                       reconciliation path exists). The dispatch may or
                       may not have actually reached GitHub; never
                       automatically redispatched.
FAILED_FINAL        -- terminal. Either a clear non-retryable failure, or
                       a clear retryable failure that already reached the
                       business attempt limit.

LEGAL_TRANSITIONS below is the exhaustive, explicit transition table (task
spec Sec. 8). set_state() refuses (raises LedgerStateConflict) any
transition not in this table, and also refuses if the record's *actual*
current state does not match the caller's expected_state -- so a caller
can never accidentally apply an outcome to the wrong record generation.

A crash between a real dispatch attempt and its outcome being durably
recorded (main.py's RelayService handles this -- see its
CRITICAL_UNKNOWN_OUTCOME_UNRECORDED path) leaves a record stuck in
DISPATCH_ATTEMPTING. This is deliberately safe by construction: only
RECEIVED and SAFE_TO_RETRY are begin_attempt()-eligible, so a record stuck
in DISPATCH_ATTEMPTING can never be picked up for another automatic
attempt either -- it carries exactly the same "reserved, needs manual
reconciliation" guarantee as an explicit UNKNOWN_OUTCOME record, without
this module needing to invent a seventh state for it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from threading import Lock
from typing import Protocol


class RelayLedgerState(str, Enum):
    RECEIVED = "RECEIVED"
    DISPATCH_ATTEMPTING = "DISPATCH_ATTEMPTING"
    DISPATCH_CONFIRMED = "DISPATCH_CONFIRMED"
    SAFE_TO_RETRY = "SAFE_TO_RETRY"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    FAILED_FINAL = "FAILED_FINAL"


# States from which begin_attempt() may proceed.
ATTEMPT_ELIGIBLE_STATES = frozenset(
    {RelayLedgerState.RECEIVED, RelayLedgerState.SAFE_TO_RETRY}
)

# States from which no further automatic action is ever taken.
TERMINAL_STATES = frozenset(
    {
        RelayLedgerState.DISPATCH_CONFIRMED,
        RelayLedgerState.FAILED_FINAL,
        RelayLedgerState.UNKNOWN_OUTCOME,
    }
)

# The exhaustive, explicit legal state-transition table. Anything not
# listed here is illegal. begin_attempt()'s own two transitions are
# enforced directly by its ATTEMPT_ELIGIBLE_STATES check, not by this
# table (set_state() is the entry point this table gates).
LEGAL_TRANSITIONS: frozenset[tuple[RelayLedgerState, RelayLedgerState]] = frozenset(
    {
        (RelayLedgerState.RECEIVED, RelayLedgerState.DISPATCH_ATTEMPTING),
        (RelayLedgerState.SAFE_TO_RETRY, RelayLedgerState.DISPATCH_ATTEMPTING),
        (RelayLedgerState.DISPATCH_ATTEMPTING, RelayLedgerState.DISPATCH_CONFIRMED),
        (RelayLedgerState.DISPATCH_ATTEMPTING, RelayLedgerState.SAFE_TO_RETRY),
        (RelayLedgerState.DISPATCH_ATTEMPTING, RelayLedgerState.FAILED_FINAL),
        (RelayLedgerState.DISPATCH_ATTEMPTING, RelayLedgerState.UNKNOWN_OUTCOME),
    }
)


class LedgerStateConflict(RuntimeError):
    """Raised by set_state() when its expected_state does not match the
    record's actual current state, or when the (expected_state,
    next_state) pair is not in LEGAL_TRANSITIONS. Always indicates a
    caller bug or a genuine concurrent race the caller must handle as a
    no-op, never a condition to blindly retry by calling set_state again
    with the same arguments."""


@dataclass(frozen=True)
class RelayLedgerRecord:
    event_key: str
    stage: str
    workflow: str | None
    attempt_count: int
    state: RelayLedgerState
    workflow_run_id: str | None
    created_at: str
    updated_at: str


class RelayLedger(Protocol):
    """Atomic per-event_key CAS store. Every method must be implementable
    as a single transaction against one document keyed by event_key -- see
    module docstring."""

    def get(self, event_key: str) -> RelayLedgerRecord | None: ...

    def reserve_new(
        self, event_key: str, *, stage: str, workflow: str | None, now: str
    ) -> RelayLedgerRecord | None:
        """Atomic create-if-absent. On success, creates a record with
        state=RECEIVED, attempt_count=1, workflow_run_id=None, and returns
        it. If a record for event_key already exists, performs no write
        and returns None -- the caller must call get() to inspect the
        existing record; this method never overwrites one."""

    def begin_attempt(
        self, event_key: str, *, now: str
    ) -> RelayLedgerRecord | None:
        """Atomic CAS: succeeds only if the record's current state is in
        ATTEMPT_ELIGIBLE_STATES (RECEIVED or SAFE_TO_RETRY). On success:
        state -> DISPATCH_ATTEMPTING; attempt_count is incremented by 1
        only when the prior state was SAFE_TO_RETRY (RECEIVED already
        carries attempt_count=1 from reserve_new, representing the first
        attempt). Returns the updated record, or None if no record exists
        or its state was not attempt-eligible (a concurrent duplicate
        already claimed it, or it is already terminal)."""

    def set_state(
        self,
        event_key: str,
        *,
        expected_state: RelayLedgerState,
        next_state: RelayLedgerState,
        now: str,
        workflow_run_id: str | None = None,
    ) -> RelayLedgerRecord:
        """Atomic CAS: writes next_state only if the record's actual
        current state equals expected_state AND (expected_state,
        next_state) is in LEGAL_TRANSITIONS; otherwise raises
        LedgerStateConflict without writing. workflow_run_id, if given,
        replaces the stored value; if omitted, the existing stored value
        (if any) is preserved."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class InMemoryRelayLedger:
    """Thread-safe local/test implementation of RelayLedger.

    A single threading.Lock guards all reads and writes to the underlying
    dict. Critical sections are tiny (one dict lookup plus one dict
    write), so this does not serialize unrelated work in any way that
    matters -- it only ever blocks two operations that are already
    racing on the exact same event_key, which is precisely the
    correctness property being proved. Two different event_keys never
    observe or influence each other's state through this lock beyond a
    brief, bounded wait for the lock itself.
    """

    def __init__(self) -> None:
        self._records: dict[str, RelayLedgerRecord] = {}
        self._lock = Lock()

    def get(self, event_key: str) -> RelayLedgerRecord | None:
        with self._lock:
            record = self._records.get(event_key)
            return replace(record) if record is not None else None

    def reserve_new(
        self, event_key: str, *, stage: str, workflow: str | None, now: str
    ) -> RelayLedgerRecord | None:
        with self._lock:
            if event_key in self._records:
                return None
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
            self._records[event_key] = record
            return replace(record)

    def begin_attempt(self, event_key: str, *, now: str) -> RelayLedgerRecord | None:
        with self._lock:
            record = self._records.get(event_key)
            if record is None or record.state not in ATTEMPT_ELIGIBLE_STATES:
                return None
            next_attempt_count = (
                record.attempt_count + 1
                if record.state is RelayLedgerState.SAFE_TO_RETRY
                else record.attempt_count
            )
            updated = replace(
                record,
                state=RelayLedgerState.DISPATCH_ATTEMPTING,
                attempt_count=next_attempt_count,
                updated_at=now,
            )
            self._records[event_key] = updated
            return replace(updated)

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
        with self._lock:
            record = self._records.get(event_key)
            if record is None or record.state is not expected_state:
                actual = record.state.value if record is not None else None
                raise LedgerStateConflict(
                    f"event_key expected_state={expected_state.value} "
                    f"but actual={actual}."
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
            self._records[event_key] = updated
            return replace(updated)
