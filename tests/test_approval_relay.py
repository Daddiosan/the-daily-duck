"""Behavioral tests for M3C ingress, routing, and transactional storage."""

from __future__ import annotations

import base64
import json
import sys
import threading
import types
import unittest
from dataclasses import replace
from typing import Any

from cloud.approval_relay.auth import AuthenticationError, PushAuthenticator
from cloud.approval_relay.gmail_reader import (
    FakeGmailReader,
    GmailReaderError,
    HistoryBatch,
    MessageMetadata,
    RealGmailReader,
    decode_message_metadata,
)

from cloud.approval_relay.github_dispatch import (
    ALLOWED_WORKFLOWS,
    DESIGN_SELECTION_WORKFLOW,
    DISPATCH_REF,
    GATE_A_WORKFLOW,
    DispatchOutcome,
    FakeGitHubDispatcher,
    WorkflowNotAllowlistedError,
)
from cloud.approval_relay.ledger import (
    LEGAL_TRANSITIONS,
    InMemoryRelayLedger,
    LedgerStateConflict,
    RelayLedgerState,
)
from cloud.approval_relay.main import (
    MAX_BUSINESS_ATTEMPTS,
    ConfigurationError,
    EnvelopeError,
    RelayConfig,
    RelayIngressService,
    RelayMode,
    RelayService,
    RelayStatus,
    create_app,
    create_app_from_env,
    decode_pubsub_envelope,
)
from cloud.approval_relay.router import (
    RelayInboundEvent,
    RelayStage,
    RoutingConfig,
    classify_stage,
    event_key_for,
    mailbox_hash_for,
    notification_key_for,
    workflow_for_stage,
)
from cloud.approval_relay.storage import (
    CURSOR_PERSISTED_FIELDS,
    EVENT_PERSISTED_FIELDS,
    RELAY_CURSOR_COLLECTION,
    RELAY_EVENTS_COLLECTION,
    FirestoreRelayStorage,
    InMemoryCursorStore,
)


GATE_A_PATTERN = "The Daily Duck — Choose Today's Story"
DESIGN_PATTERN = "The Daily Duck — Choose Image + Title"

ROUTING = RoutingConfig(
    gate_a_subject_pattern=GATE_A_PATTERN, design_subject_pattern=DESIGN_PATTERN
)


def make_event(
    *,
    subject: str | None,
    mailbox: str = "owner@example.com",
    message_id: str = "msg-1",
    authorized: bool = True,
) -> RelayInboundEvent:
    return RelayInboundEvent(
        mailbox_identity=mailbox,
        gmail_message_id=message_id,
        subject=subject,
        from_allowlist_match=authorized,
    )


def make_service(
    *,
    dispatcher: FakeGitHubDispatcher | None = None,
    ledger: InMemoryRelayLedger | None = None,
    mode: RelayMode = RelayMode.LIVE,
) -> RelayService:
    counter = {"n": 0}

    def clock() -> str:
        counter["n"] += 1
        return f"2026-09-21T00:00:{counter['n']:02d}Z"

    return RelayService(
        ledger=ledger or InMemoryRelayLedger(),
        dispatcher=dispatcher or FakeGitHubDispatcher(),
        routing=ROUTING,
        mode=mode,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class RoutingTests(unittest.TestCase):
    def test_gate_a_candidate(self):
        self.assertEqual(
            classify_stage(f"Re: {GATE_A_PATTERN} — 2026-09-21", ROUTING),
            RelayStage.GATE_A,
        )

    def test_design_candidate(self):
        self.assertEqual(
            classify_stage(f"{DESIGN_PATTERN} — 2026-09-21 — Batch 1", ROUTING),
            RelayStage.DESIGN_SELECTION,
        )

    def test_unrelated(self):
        self.assertEqual(
            classify_stage("Your Amazon order has shipped", ROUTING),
            RelayStage.UNRELATED,
        )

    def test_ambiguous_when_both_patterns_present(self):
        subject = f"{GATE_A_PATTERN} {DESIGN_PATTERN}"
        self.assertEqual(classify_stage(subject, ROUTING), RelayStage.AMBIGUOUS)

    def test_workflow_for_stage_allowlist(self):
        self.assertEqual(workflow_for_stage(RelayStage.GATE_A), GATE_A_WORKFLOW)
        self.assertEqual(
            workflow_for_stage(RelayStage.DESIGN_SELECTION), DESIGN_SELECTION_WORKFLOW
        )
        self.assertIsNone(workflow_for_stage(RelayStage.UNRELATED))
        self.assertIsNone(workflow_for_stage(RelayStage.AMBIGUOUS))

    def test_sender_and_subject_are_both_required(self):
        dispatcher = FakeGitHubDispatcher()
        service = make_service(dispatcher=dispatcher, mode=RelayMode.DRY_RUN)
        authorized_gate = service.process_event(make_event(subject=GATE_A_PATTERN))
        authorized_design = service.process_event(
            make_event(subject=DESIGN_PATTERN, message_id="m2")
        )
        unauthorized_gate = service.process_event(
            make_event(subject=GATE_A_PATTERN, message_id="m3", authorized=False)
        )
        unauthorized_design = service.process_event(
            make_event(subject=DESIGN_PATTERN, message_id="m4", authorized=False)
        )
        unrelated = service.process_event(make_event(subject="hello", message_id="m5"))
        self.assertEqual(authorized_gate.status, RelayStatus.DRY_RUN_ROUTED)
        self.assertEqual(authorized_design.status, RelayStatus.DRY_RUN_ROUTED)
        self.assertEqual(unauthorized_gate.status, RelayStatus.UNAUTHORIZED_NO_DISPATCH)
        self.assertEqual(unauthorized_design.status, RelayStatus.UNAUTHORIZED_NO_DISPATCH)
        self.assertEqual(unrelated.status, RelayStatus.UNRELATED_NO_DISPATCH)
        self.assertEqual(dispatcher.calls, [])

    def test_missing_subject_and_malformed_sender_are_not_candidates(self):
        service = make_service(mode=RelayMode.DRY_RUN)
        self.assertEqual(
            service.process_event(make_event(subject=None)).status,
            RelayStatus.UNRELATED_NO_DISPATCH,
        )
        self.assertEqual(
            service.process_event(
                make_event(subject=GATE_A_PATTERN, authorized=False)
            ).status,
            RelayStatus.UNAUTHORIZED_NO_DISPATCH,
        )


class EventKeyTests(unittest.TestCase):
    def test_deterministic_for_same_inputs(self):
        a = event_key_for("owner@example.com", "msg-1")
        b = event_key_for("owner@example.com", "msg-1")
        self.assertEqual(a, b)

    def test_different_message_ids_do_not_collide(self):
        a = event_key_for("owner@example.com", "msg-1")
        b = event_key_for("owner@example.com", "msg-2")
        self.assertNotEqual(a, b)

    def test_different_mailboxes_do_not_collide(self):
        a = event_key_for("owner@example.com", "msg-1")
        b = event_key_for("other@example.com", "msg-1")
        self.assertNotEqual(a, b)

    def test_notification_and_message_keys_have_separate_namespaces(self):
        notification = notification_key_for("owner@example.com", "101")
        message = event_key_for("owner@example.com", "101")
        self.assertEqual(notification, notification_key_for("OWNER@example.com", "101"))
        self.assertNotEqual(notification, message)


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------


class DedupeTests(unittest.TestCase):
    def test_duplicate_sequential_event_dispatches_once(self):
        dispatcher = FakeGitHubDispatcher()
        service = make_service(dispatcher=dispatcher)
        event = make_event(subject=GATE_A_PATTERN)

        first = service.process_event(event)
        second = service.process_event(event)

        self.assertEqual(first.status, RelayStatus.DISPATCHED)
        self.assertEqual(second.status, RelayStatus.DUPLICATE_TERMINAL_NO_OP)
        self.assertEqual(len(dispatcher.calls), 1)
        self.assertEqual(second.workflow_run_id, first.workflow_run_id)

    def test_unrelated_event_never_touches_ledger(self):
        ledger = InMemoryRelayLedger()
        service = make_service(ledger=ledger)
        event = make_event(subject="Unrelated newsletter")

        result = service.process_event(event)

        self.assertEqual(result.status, RelayStatus.UNRELATED_NO_DISPATCH)
        self.assertIsNone(result.event_key)

    def test_ambiguous_event_recorded_but_never_dispatched(self):
        dispatcher = FakeGitHubDispatcher()
        ledger = InMemoryRelayLedger()
        service = make_service(dispatcher=dispatcher, ledger=ledger)
        event = make_event(subject=f"{GATE_A_PATTERN} {DESIGN_PATTERN}")

        result = service.process_event(event)
        again = service.process_event(event)

        self.assertEqual(result.status, RelayStatus.AMBIGUOUS_NO_DISPATCH)
        self.assertEqual(again.status, RelayStatus.AMBIGUOUS_NO_DISPATCH)
        self.assertEqual(len(dispatcher.calls), 0)
        record = ledger.get(result.event_key)
        self.assertEqual(record.state, RelayLedgerState.RECEIVED)
        self.assertIsNone(record.workflow)


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


class RetryTests(unittest.TestCase):
    def test_success_first_attempt(self):
        dispatcher = FakeGitHubDispatcher()
        service = make_service(dispatcher=dispatcher)
        result = service.process_event(make_event(subject=GATE_A_PATTERN))
        self.assertEqual(result.status, RelayStatus.DISPATCHED)
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(len(dispatcher.calls), 1)

    def test_clear_retryable_then_success(self):
        dispatcher = FakeGitHubDispatcher(
            scripted_outcomes={
                GATE_A_WORKFLOW: [DispatchOutcome.CLEAR_RETRYABLE_FAILURE]
            }
        )
        service = make_service(dispatcher=dispatcher)
        event = make_event(subject=GATE_A_PATTERN)

        first = service.process_event(event)
        second = service.process_event(event)

        self.assertEqual(first.status, RelayStatus.SAFE_TO_RETRY)
        self.assertEqual(first.attempt_count, 1)
        self.assertEqual(second.status, RelayStatus.DISPATCHED)
        self.assertEqual(second.attempt_count, 2)
        self.assertEqual(len(dispatcher.calls), 2)

    def test_three_retries_exhausted_attempt_four_is_final(self):
        self.assertEqual(MAX_BUSINESS_ATTEMPTS, 4)
        dispatcher = FakeGitHubDispatcher(
            default_outcome=DispatchOutcome.CLEAR_RETRYABLE_FAILURE
        )
        service = make_service(dispatcher=dispatcher)
        event = make_event(subject=GATE_A_PATTERN)

        results = [service.process_event(event) for _ in range(4)]

        self.assertEqual(
            [r.status for r in results],
            [
                RelayStatus.SAFE_TO_RETRY,
                RelayStatus.SAFE_TO_RETRY,
                RelayStatus.SAFE_TO_RETRY,
                RelayStatus.FAILED_FINAL,
            ],
        )
        self.assertEqual([r.attempt_count for r in results], [1, 2, 3, 4])
        self.assertEqual(len(dispatcher.calls), 4)

        fifth = service.process_event(event)
        self.assertEqual(fifth.status, RelayStatus.DUPLICATE_TERMINAL_NO_OP)
        self.assertEqual(len(dispatcher.calls), 4)

    def test_clear_final_failure_no_retry(self):
        dispatcher = FakeGitHubDispatcher(
            default_outcome=DispatchOutcome.CLEAR_FINAL_FAILURE
        )
        service = make_service(dispatcher=dispatcher)
        event = make_event(subject=GATE_A_PATTERN)

        first = service.process_event(event)
        second = service.process_event(event)

        self.assertEqual(first.status, RelayStatus.FAILED_FINAL)
        self.assertEqual(second.status, RelayStatus.DUPLICATE_TERMINAL_NO_OP)
        self.assertEqual(len(dispatcher.calls), 1)

    def test_unknown_outcome(self):
        dispatcher = FakeGitHubDispatcher(
            default_outcome=DispatchOutcome.UNKNOWN_OUTCOME
        )
        service = make_service(dispatcher=dispatcher)
        result = service.process_event(make_event(subject=GATE_A_PATTERN))
        self.assertEqual(result.status, RelayStatus.UNKNOWN_OUTCOME)

    def test_duplicate_after_unknown_outcome_never_redispatches(self):
        dispatcher = FakeGitHubDispatcher(
            default_outcome=DispatchOutcome.UNKNOWN_OUTCOME
        )
        service = make_service(dispatcher=dispatcher)
        event = make_event(subject=GATE_A_PATTERN)

        service.process_event(event)
        again = service.process_event(event)

        self.assertEqual(again.status, RelayStatus.DUPLICATE_TERMINAL_NO_OP)
        self.assertEqual(len(dispatcher.calls), 1)


# ---------------------------------------------------------------------------
# Post-dispatch durable-write failure (task spec Sec. 14: "If outcome
# cannot be durably recorded after an actual dispatch attempt: treat as a
# critical UNKNOWN_OUTCOME condition ... do not issue a second dispatch in
# the same request"). Simulated by making the ledger's set_state raise
# LedgerStateConflict on demand, exactly as a transactional Firestore write
# failure would surface to RelayService in a real deployment.
# ---------------------------------------------------------------------------


class PostDispatchLedgerWriteFailureTests(unittest.TestCase):
    def _wrap_set_state(self, ledger: InMemoryRelayLedger, *, fail_times: int):
        real_set_state = ledger.set_state
        state = {"remaining": fail_times}

        def flaky(*args: Any, **kwargs: Any):
            if state["remaining"] > 0:
                state["remaining"] -= 1
                raise LedgerStateConflict("simulated durable write failure")
            return real_set_state(*args, **kwargs)

        ledger.set_state = flaky  # type: ignore[method-assign]

    def test_recoverable_write_failure_falls_back_to_unknown_outcome(self):
        dispatcher = FakeGitHubDispatcher()
        ledger = InMemoryRelayLedger()
        service = make_service(dispatcher=dispatcher, ledger=ledger)
        event = make_event(subject=GATE_A_PATTERN)

        # Only the first set_state call (recording SUCCESS ->
        # DISPATCH_CONFIRMED) fails; the fallback attempt to mark
        # UNKNOWN_OUTCOME uses the real, working implementation.
        self._wrap_set_state(ledger, fail_times=1)

        result = service.process_event(event)

        self.assertEqual(result.status, RelayStatus.CRITICAL_UNKNOWN_OUTCOME_UNRECORDED)
        self.assertEqual(len(dispatcher.calls), 1)
        record = ledger.get(result.event_key)
        self.assertEqual(record.state, RelayLedgerState.UNKNOWN_OUTCOME)

        # A later redelivery of the same event must never redispatch.
        again = service.process_event(event)
        self.assertEqual(again.status, RelayStatus.DUPLICATE_TERMINAL_NO_OP)
        self.assertEqual(len(dispatcher.calls), 1)

    def test_persistent_write_failure_leaves_record_stuck_but_never_redispatches(self):
        dispatcher = FakeGitHubDispatcher()
        ledger = InMemoryRelayLedger()
        service = make_service(dispatcher=dispatcher, ledger=ledger)
        event = make_event(subject=GATE_A_PATTERN)

        # Every set_state call fails -- even the best-effort UNKNOWN_OUTCOME
        # fallback. The record must stay stuck in DISPATCH_ATTEMPTING,
        # which ledger.py's module docstring documents as equally safe
        # against automatic redispatch.
        self._wrap_set_state(ledger, fail_times=1000)

        result = service.process_event(event)

        self.assertEqual(result.status, RelayStatus.CRITICAL_UNKNOWN_OUTCOME_UNRECORDED)
        self.assertEqual(len(dispatcher.calls), 1)
        record = ledger.get(result.event_key)
        self.assertEqual(record.state, RelayLedgerState.DISPATCH_ATTEMPTING)

        again = service.process_event(event)
        self.assertEqual(again.status, RelayStatus.ALREADY_IN_PROGRESS)
        self.assertEqual(len(dispatcher.calls), 1)


# ---------------------------------------------------------------------------
# DRY_RUN
# ---------------------------------------------------------------------------


class DryRunTests(unittest.TestCase):
    def test_dry_run_correct_route(self):
        service = make_service(mode=RelayMode.DRY_RUN)
        result = service.process_event(make_event(subject=DESIGN_PATTERN))
        self.assertEqual(result.status, RelayStatus.DRY_RUN_ROUTED)
        self.assertEqual(result.stage, RelayStage.DESIGN_SELECTION)
        self.assertEqual(result.workflow, DESIGN_SELECTION_WORKFLOW)

    def test_dry_run_zero_dispatcher_calls(self):
        dispatcher = FakeGitHubDispatcher()
        service = make_service(dispatcher=dispatcher, mode=RelayMode.DRY_RUN)
        service.process_event(make_event(subject=GATE_A_PATTERN))
        self.assertEqual(len(dispatcher.calls), 0)

    def test_dry_run_never_produces_confirmed_state(self):
        ledger = InMemoryRelayLedger()
        service = make_service(ledger=ledger, mode=RelayMode.DRY_RUN)
        result = service.process_event(make_event(subject=GATE_A_PATTERN))
        record = ledger.get(result.event_key)
        self.assertEqual(record.state, RelayLedgerState.RECEIVED)
        self.assertNotEqual(result.status, RelayStatus.DISPATCHED)

    def test_dry_run_duplicate_still_no_dispatch(self):
        dispatcher = FakeGitHubDispatcher()
        service = make_service(dispatcher=dispatcher, mode=RelayMode.DRY_RUN)
        event = make_event(subject=GATE_A_PATTERN)
        service.process_event(event)
        service.process_event(event)
        self.assertEqual(len(dispatcher.calls), 0)


# ---------------------------------------------------------------------------
# Malformed events
# ---------------------------------------------------------------------------


def pubsub_envelope(
    *, email: str = "owner@example.com", history_id: object = "101"
) -> dict[str, object]:
    data = base64.b64encode(
        json.dumps({"emailAddress": email, "historyId": history_id}).encode()
    ).decode()
    return {"message": {"data": data, "messageId": "pubsub-1"}}


class PubSubEnvelopeTests(unittest.TestCase):
    def test_valid_envelope(self):
        value = decode_pubsub_envelope(pubsub_envelope())
        self.assertEqual(value.email_address, "owner@example.com")
        self.assertEqual(value.history_id, "101")

    def test_invalid_shapes_fail_closed(self):
        invalid = [
            None,
            {},
            {"message": {}},
            {"message": {"data": "%%%"}},
            {"message": {"data": base64.b64encode(b"not json").decode()}},
            {
                "message": {
                    "data": base64.b64encode(json.dumps({"historyId": "1"}).encode()).decode()
                }
            },
            {
                "message": {
                    "data": base64.b64encode(
                        json.dumps({"emailAddress": "owner@example.com"}).encode()
                    ).decode()
                }
            },
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(EnvelopeError):
                decode_pubsub_envelope(value)

    def test_history_id_requires_nonempty_decimal_string(self):
        for history_id in (
            12345,
            1.5,
            True,
            None,
            [],
            {},
            "",
            " ",
            "12x",
            "１２３",
        ):
            with self.subTest(history_id=history_id), self.assertRaises(EnvelopeError):
                decode_pubsub_envelope(pubsub_envelope(history_id=history_id))


# ---------------------------------------------------------------------------
# Ledger state machine
# ---------------------------------------------------------------------------


class LedgerStateMachineTests(unittest.TestCase):
    def test_legal_transitions_are_exactly_the_documented_set(self):
        expected = {
            (RelayLedgerState.RECEIVED, RelayLedgerState.DISPATCH_ATTEMPTING),
            (RelayLedgerState.SAFE_TO_RETRY, RelayLedgerState.DISPATCH_ATTEMPTING),
            (
                RelayLedgerState.DISPATCH_ATTEMPTING,
                RelayLedgerState.DISPATCH_CONFIRMED,
            ),
            (RelayLedgerState.DISPATCH_ATTEMPTING, RelayLedgerState.SAFE_TO_RETRY),
            (RelayLedgerState.DISPATCH_ATTEMPTING, RelayLedgerState.FAILED_FINAL),
            (RelayLedgerState.DISPATCH_ATTEMPTING, RelayLedgerState.UNKNOWN_OUTCOME),
        }
        self.assertEqual(LEGAL_TRANSITIONS, expected)

    def test_set_state_rejects_illegal_transition(self):
        ledger = InMemoryRelayLedger()
        ledger.reserve_new("k1", stage="GATE_A", workflow=GATE_A_WORKFLOW, now="t0")
        with self.assertRaises(LedgerStateConflict):
            ledger.set_state(
                "k1",
                expected_state=RelayLedgerState.RECEIVED,
                next_state=RelayLedgerState.DISPATCH_CONFIRMED,
                now="t1",
            )

    def test_set_state_rejects_mismatched_expected_state(self):
        ledger = InMemoryRelayLedger()
        ledger.reserve_new("k1", stage="GATE_A", workflow=GATE_A_WORKFLOW, now="t0")
        ledger.begin_attempt("k1", now="t1")
        with self.assertRaises(LedgerStateConflict):
            ledger.set_state(
                "k1",
                expected_state=RelayLedgerState.RECEIVED,
                next_state=RelayLedgerState.DISPATCH_ATTEMPTING,
                now="t2",
            )

    def test_begin_attempt_from_received_keeps_attempt_count_one(self):
        ledger = InMemoryRelayLedger()
        ledger.reserve_new("k1", stage="GATE_A", workflow=GATE_A_WORKFLOW, now="t0")
        record = ledger.begin_attempt("k1", now="t1")
        self.assertEqual(record.attempt_count, 1)

    def test_begin_attempt_from_safe_to_retry_increments(self):
        ledger = InMemoryRelayLedger()
        ledger.reserve_new("k1", stage="GATE_A", workflow=GATE_A_WORKFLOW, now="t0")
        ledger.begin_attempt("k1", now="t1")
        ledger.set_state(
            "k1",
            expected_state=RelayLedgerState.DISPATCH_ATTEMPTING,
            next_state=RelayLedgerState.SAFE_TO_RETRY,
            now="t2",
        )
        record = ledger.begin_attempt("k1", now="t3")
        self.assertEqual(record.attempt_count, 2)


# ---------------------------------------------------------------------------
# Security-relevant dispatcher allowlist behavior (functional; static
# checks live in tests/test_approval_relay_contract.py)
# ---------------------------------------------------------------------------


class DispatcherAllowlistTests(unittest.TestCase):
    def test_caller_cannot_choose_arbitrary_workflow(self):
        dispatcher = FakeGitHubDispatcher()
        with self.assertRaises(WorkflowNotAllowlistedError):
            dispatcher.dispatch(workflow="evil-workflow.yml", ref=DISPATCH_REF)

    def test_caller_cannot_choose_arbitrary_ref(self):
        dispatcher = FakeGitHubDispatcher()
        with self.assertRaises(WorkflowNotAllowlistedError):
            dispatcher.dispatch(workflow=GATE_A_WORKFLOW, ref="not-main")

    def test_allowlist_contains_exactly_the_two_workflows(self):
        self.assertEqual(
            ALLOWED_WORKFLOWS, frozenset({GATE_A_WORKFLOW, DESIGN_SELECTION_WORKFLOW})
        )


# ---------------------------------------------------------------------------
# Concurrency: real threading.Thread + Barrier/Event, no sleep-based races.
# ---------------------------------------------------------------------------


class RealConcurrencyTests(unittest.TestCase):
    def test_two_real_threads_same_event_key_at_most_one_dispatch_call(self):
        dispatcher = FakeGitHubDispatcher()
        service = make_service(dispatcher=dispatcher)
        event = make_event(subject=GATE_A_PATTERN, message_id="race-1")

        barrier = threading.Barrier(2)
        results: dict[int, Any] = {}
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=5)
                results[index] = service.process_event(event)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(dispatcher.calls), 1)
        statuses = sorted(r.status.value for r in results.values())
        self.assertIn(RelayStatus.DISPATCHED.value, statuses)
        other = [s for s in statuses if s != RelayStatus.DISPATCHED.value][0]
        self.assertIn(
            other,
            {
                RelayStatus.ALREADY_IN_PROGRESS.value,
                RelayStatus.DUPLICATE_TERMINAL_NO_OP.value,
            },
        )

    def test_ledger_begin_attempt_concurrent_only_one_winner(self):
        ledger = InMemoryRelayLedger()
        ledger.reserve_new(
            "race-key", stage="GATE_A", workflow=GATE_A_WORKFLOW, now="t0"
        )

        barrier = threading.Barrier(4)
        winners: list[Any] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait(timeout=5)
            record = ledger.begin_attempt("race-key", now="t1")
            if record is not None:
                with lock:
                    winners.append(record)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].attempt_count, 1)

    def test_duplicate_after_confirmed_under_concurrency_no_call(self):
        dispatcher = FakeGitHubDispatcher()
        service = make_service(dispatcher=dispatcher)
        event = make_event(subject=GATE_A_PATTERN, message_id="race-2")
        first = service.process_event(event)
        self.assertEqual(first.status, RelayStatus.DISPATCHED)

        barrier = threading.Barrier(3)
        results: list[Any] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait(timeout=5)
            result = service.process_event(event)
            with lock:
                results.append(result)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(dispatcher.calls), 1)
        self.assertTrue(
            all(r.status == RelayStatus.DUPLICATE_TERMINAL_NO_OP for r in results)
        )

    def test_separate_event_keys_do_not_block_each_other_incorrectly(self):
        dispatcher = FakeGitHubDispatcher()
        service = make_service(dispatcher=dispatcher)
        event_a = make_event(subject=GATE_A_PATTERN, message_id="key-a")
        event_b = make_event(subject=DESIGN_PATTERN, message_id="key-b")

        barrier = threading.Barrier(2)
        results: dict[str, Any] = {}

        def worker(name: str, event: RelayInboundEvent) -> None:
            barrier.wait(timeout=5)
            results[name] = service.process_event(event)

        threads = [
            threading.Thread(target=worker, args=("a", event_a)),
            threading.Thread(target=worker, args=("b", event_b)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(results["a"].status, RelayStatus.DISPATCHED)
        self.assertEqual(results["b"].status, RelayStatus.DISPATCHED)
        self.assertEqual(len(dispatcher.calls), 2)


# ---------------------------------------------------------------------------
# Gmail history / metadata
# ---------------------------------------------------------------------------


class _Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class _HistoryApi:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return _Request(self.pages.pop(0))


class _MessagesApi:
    def __init__(self, messages=None):
        self.messages = dict(messages or {})
        self.calls = []

    def get(self, **kwargs):
        self.calls.append(kwargs)
        return _Request(self.messages[kwargs["id"]])


class _UsersApi:
    def __init__(self, pages, messages=None):
        self.history_api = _HistoryApi(pages)
        self.messages_api = _MessagesApi(messages)

    def history(self):
        return self.history_api

    def messages(self):
        return self.messages_api


class _GmailApi:
    def __init__(self, pages, messages=None):
        self.users_api = _UsersApi(pages, messages)

    def users(self):
        return self.users_api


class GmailReaderTests(unittest.TestCase):
    def test_one_page_and_multi_page_dedupe(self):
        api = _GmailApi(
            [
                {
                    "history": [{"messagesAdded": [{"message": {"id": "m1"}}]}],
                    "nextPageToken": "p2",
                },
                {
                    "history": [
                        {
                            "messagesAdded": [
                                {"message": {"id": "m1"}},
                                {"message": {"id": "m2"}},
                            ]
                        }
                    ]
                },
            ]
        )
        batch = RealGmailReader(api).list_history("100")
        self.assertEqual(batch.message_ids, ("m1", "m2"))
        self.assertEqual(api.users_api.history_api.calls[1]["pageToken"], "p2")

    def test_later_page_failure_never_returns_partial_success(self):
        api = _GmailApi(
            [
                {"history": [], "nextPageToken": "p2"},
                RuntimeError("later page failed"),
            ]
        )
        with self.assertRaises(GmailReaderError):
            RealGmailReader(api).list_history("100")

    def test_repeated_page_token_fails(self):
        api = _GmailApi(
            [
                {"history": [], "nextPageToken": "same"},
                {"history": [], "nextPageToken": "same"},
            ]
        )
        with self.assertRaises(GmailReaderError):
            RealGmailReader(api).list_history("100")

    def test_empty_page_with_continuation_is_followed(self):
        api = _GmailApi(
            [
                {"history": [], "nextPageToken": "p2"},
                {"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}]},
            ]
        )
        self.assertEqual(
            RealGmailReader(api).list_history("100").message_ids, ("m1",)
        )

    def test_message_fetch_requests_metadata_only(self):
        api = _GmailApi(
            [],
            {
                "m1": {
                    "id": "m1",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": GATE_A_PATTERN},
                            {"name": "From", "value": "Duck <OWNER@Example.com>"},
                        ]
                    },
                }
            },
        )
        value = RealGmailReader(api).get_message_metadata("m1")
        self.assertEqual(value.sender, "owner@example.com")
        call = api.users_api.messages_api.calls[0]
        self.assertEqual(call["format"], "metadata")
        self.assertEqual(call["metadataHeaders"], ["Subject", "From"])

    def test_spoof_display_name_and_malformed_headers_do_not_authorize(self):
        spoof = decode_message_metadata(
            {
                "id": "m1",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": GATE_A_PATTERN},
                        {
                            "name": "From",
                            "value": '"owner@example.com" <attacker@example.com>',
                        },
                    ]
                },
            }
        )
        malformed = decode_message_metadata({"id": "m2", "payload": {"headers": []}})
        self.assertEqual(spoof.sender, "attacker@example.com")
        self.assertIsNone(malformed.sender)
        self.assertIsNone(malformed.subject)

    def test_sender_normalization_is_case_insensitive_and_trims_whitespace(self):
        value = decode_message_metadata(
            {
                "id": "m1",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": GATE_A_PATTERN},
                        {"name": "From", "value": "  OWNER@Example.com  "},
                    ]
                },
            }
        )
        self.assertEqual(value.sender, "owner@example.com")


# ---------------------------------------------------------------------------
# Firestore durable cursor / ledger
# ---------------------------------------------------------------------------


class FakeSnapshot:
    def __init__(self, data):
        self._data = dict(data) if data is not None else None
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeDocumentReference:
    def __init__(self, client, collection_name, document_id):
        self._client = client
        self._collection_name = collection_name
        self._document_id = document_id

    @property
    def _collection(self):
        return self._client._collections.setdefault(self._collection_name, {})

    def get(self, transaction=None):
        if transaction is not None:
            self._client.transaction_reads += 1
        return FakeSnapshot(self._collection.get(self._document_id))

    def create(self, data):
        if self._document_id in self._collection:
            raise RuntimeError("already exists")
        self._collection[self._document_id] = dict(data)

    def set(self, data, merge=False):
        if merge and self._document_id in self._collection:
            self._collection[self._document_id] = {
                **self._collection[self._document_id],
                **dict(data),
            }
        else:
            self._collection[self._document_id] = dict(data)

    def update(self, data):
        if self._document_id not in self._collection:
            raise RuntimeError("missing document")
        self._collection[self._document_id].update(dict(data))


class FakeCollection:
    def __init__(self, client, name):
        self._client = client
        self._name = name

    def document(self, document_id):
        return FakeDocumentReference(self._client, self._name, document_id)


class FakeTransaction:
    def __init__(self, client):
        self.client = client

    def create(self, reference, data):
        reference.create(data)

    def set(self, reference, data, merge=False):
        reference.set(data, merge=merge)

    def update(self, reference, data):
        reference.update(data)


class FakeFirestoreClient:
    def __init__(self):
        self._collections = {}
        self.lock = threading.RLock()
        self.transaction_count = 0
        self.transactional_calls = 0
        self.transaction_attempts = 0
        self.transaction_reads = 0
        self.conflicts_remaining = 0
        self.injected_conflicts = 0

    def collection(self, name):
        return FakeCollection(self, name)

    def transaction(self):
        self.transaction_count += 1
        return FakeTransaction(self)


def install_fake_firestore():
    saved = {
        name: sys.modules.get(name)
        for name in ("google", "google.cloud", "google.cloud.firestore")
    }

    def transactional(function):
        def wrapper(transaction, *args, **kwargs):
            with transaction.client.lock:
                transaction.client.transactional_calls += 1
                while True:
                    transaction.client.transaction_attempts += 1
                    if transaction.client.conflicts_remaining:
                        transaction.client.conflicts_remaining -= 1
                        transaction.client.injected_conflicts += 1
                        continue
                    return function(transaction, *args, **kwargs)

        return wrapper

    google_module = sys.modules.get("google") or types.ModuleType("google")
    had_cloud_attribute = hasattr(google_module, "cloud")
    original_cloud_attribute = getattr(google_module, "cloud", None)
    cloud_module = types.ModuleType("google.cloud")
    firestore_module = types.ModuleType("google.cloud.firestore")
    firestore_module.transactional = transactional
    cloud_module.firestore = firestore_module
    if not hasattr(google_module, "cloud"):
        google_module.cloud = cloud_module
    sys.modules["google"] = google_module
    sys.modules["google.cloud"] = cloud_module
    sys.modules["google.cloud.firestore"] = firestore_module
    return saved, had_cloud_attribute, original_cloud_attribute


def restore_fake_firestore(saved):
    modules, had_cloud_attribute, original_cloud_attribute = saved
    google_module = sys.modules.get("google")
    if google_module is not None:
        if had_cloud_attribute:
            google_module.cloud = original_cloud_attribute
        elif hasattr(google_module, "cloud"):
            delattr(google_module, "cloud")
    for name, module in modules.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class FirestoreRelayStorageTests(unittest.TestCase):
    def setUp(self):
        self.saved_modules = install_fake_firestore()
        self.client = FakeFirestoreClient()
        self.storage = FirestoreRelayStorage(self.client, clock=lambda: "cursor-time")

    def tearDown(self):
        restore_fake_firestore(self.saved_modules)

    def test_first_cursor_initialization_and_cas(self):
        mailbox_hash = mailbox_hash_for("owner@example.com")
        self.assertIsNone(self.storage.read_cursor(mailbox_hash))
        self.assertTrue(
            self.storage.compare_and_update_cursor(mailbox_hash, None, "100")
        )
        self.assertEqual(self.storage.read_cursor(mailbox_hash).history_id, "100")
        self.assertTrue(
            self.storage.compare_and_update_cursor(mailbox_hash, "100", "101")
        )
        self.assertFalse(
            self.storage.compare_and_update_cursor(mailbox_hash, "100", "102")
        )

    def test_sdk_transaction_retry_is_safe_for_cursor_initialization(self):
        mailbox_hash = mailbox_hash_for("owner@example.com")
        self.client.conflicts_remaining = 1
        self.assertTrue(
            self.storage.compare_and_update_cursor(mailbox_hash, None, "100")
        )
        self.assertEqual(self.client.injected_conflicts, 1)
        self.assertEqual(self.client.transaction_attempts, 2)
        self.assertEqual(self.storage.read_cursor(mailbox_hash).history_id, "100")

    def test_restart_reads_persisted_cursor_and_env_floor_never_overwrites(self):
        first_reader = FakeGmailReader(history=HistoryBatch(()))
        first = RelayIngressService(
            config=make_config(),
            gmail=first_reader,
            cursor_store=self.storage,
            relay=make_service(mode=RelayMode.DRY_RUN),
        )
        first.process_pubsub(pubsub_envelope(history_id="101"))

        reconstructed_storage = FirestoreRelayStorage(
            self.client, clock=lambda: "restart-time"
        )
        second_reader = FakeGmailReader(history=HistoryBatch(()))
        second = RelayIngressService(
            config=replace(make_config(), initial_history_id="999"),
            gmail=second_reader,
            cursor_store=reconstructed_storage,
            relay=make_service(mode=RelayMode.DRY_RUN),
        )
        second.process_pubsub(pubsub_envelope(history_id="102"))
        self.assertEqual(first_reader.list_history_calls, ["100"])
        self.assertEqual(second_reader.list_history_calls, ["101"])
        persisted = reconstructed_storage.read_cursor(
            mailbox_hash_for("owner@example.com")
        )
        self.assertEqual(persisted.history_id, "102")

    def test_two_storage_instances_reserve_same_event_only_once(self):
        other = FirestoreRelayStorage(self.client)
        barrier = threading.Barrier(2)
        results = []
        result_lock = threading.Lock()

        def reserve(storage):
            barrier.wait(timeout=5)
            value = storage.reserve_new(
                "event-1", stage="GATE_A", workflow=GATE_A_WORKFLOW, now="t0"
            )
            with result_lock:
                results.append(value is not None)

        threads = [
            threading.Thread(target=reserve, args=(self.storage,)),
            threading.Thread(target=reserve, args=(other,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sorted(results), [False, True])
        self.assertGreaterEqual(self.client.transactional_calls, 2)
        self.assertGreaterEqual(self.client.transaction_reads, 2)

    def test_two_relay_instances_allow_at_most_one_dispatch_attempt(self):
        dispatcher = FakeGitHubDispatcher()
        first = RelayService(
            ledger=self.storage,
            dispatcher=dispatcher,
            routing=ROUTING,
            mode=RelayMode.LIVE,
        )
        second = RelayService(
            ledger=FirestoreRelayStorage(self.client),
            dispatcher=dispatcher,
            routing=ROUTING,
            mode=RelayMode.LIVE,
        )
        barrier = threading.Barrier(2)
        results = []

        def process(service):
            barrier.wait(timeout=5)
            results.append(service.process_event(make_event(subject=GATE_A_PATTERN)))

        threads = [
            threading.Thread(target=process, args=(first,)),
            threading.Thread(target=process, args=(second,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(results), 2)
        self.assertEqual(len(dispatcher.calls), 1)
        self.assertIn(RelayStatus.DISPATCHED, {result.status for result in results})

    def test_attempt_budget_terminal_states_and_independent_keys(self):
        for key in ("retry", "unknown", "confirmed", "independent"):
            self.storage.reserve_new(
                key, stage="GATE_A", workflow=GATE_A_WORKFLOW, now="t0"
            )

        attempt = None
        for number in range(1, 5):
            attempt = self.storage.begin_attempt("retry", now=f"t{number}")
            self.assertEqual(attempt.attempt_count, number)
            self.storage.set_state(
                "retry",
                expected_state=RelayLedgerState.DISPATCH_ATTEMPTING,
                next_state=(
                    RelayLedgerState.SAFE_TO_RETRY
                    if number < 4
                    else RelayLedgerState.FAILED_FINAL
                ),
                now=f"s{number}",
            )
        self.assertIsNone(self.storage.begin_attempt("retry", now="t5"))

        self.storage.begin_attempt("unknown", now="t1")
        self.storage.set_state(
            "unknown",
            expected_state=RelayLedgerState.DISPATCH_ATTEMPTING,
            next_state=RelayLedgerState.UNKNOWN_OUTCOME,
            now="t2",
        )
        self.assertIsNone(self.storage.begin_attempt("unknown", now="t3"))

        self.storage.begin_attempt("confirmed", now="t1")
        self.storage.set_state(
            "confirmed",
            expected_state=RelayLedgerState.DISPATCH_ATTEMPTING,
            next_state=RelayLedgerState.DISPATCH_CONFIRMED,
            now="t2",
        )
        self.assertIsNone(self.storage.begin_attempt("confirmed", now="t3"))
        self.assertIsNotNone(self.storage.begin_attempt("independent", now="t1"))

    def test_persisted_schemas_are_minimal_and_collections_fixed(self):
        mailbox_hash = mailbox_hash_for("owner@example.com")
        self.storage.compare_and_update_cursor(mailbox_hash, None, "100")
        self.storage.reserve_new(
            "event-1", stage="GATE_A", workflow=GATE_A_WORKFLOW, now="t0"
        )
        cursor_data = self.client._collections[RELAY_CURSOR_COLLECTION][mailbox_hash]
        event_data = self.client._collections[RELAY_EVENTS_COLLECTION]["event-1"]
        self.assertEqual(set(cursor_data), CURSOR_PERSISTED_FIELDS)
        self.assertEqual(set(event_data), EVENT_PERSISTED_FIELDS)
        for forbidden in ("mailbox", "sender", "subject", "body", "token"):
            self.assertNotIn(forbidden, cursor_data)
            self.assertNotIn(forbidden, event_data)
        self.assertEqual(
            set(self.client._collections),
            {RELAY_CURSOR_COLLECTION, RELAY_EVENTS_COLLECTION},
        )


# ---------------------------------------------------------------------------
# Authenticated Flask ingress
# ---------------------------------------------------------------------------


def make_config() -> RelayConfig:
    return RelayConfig(
        mailbox_identity="owner@example.com",
        allowed_senders=frozenset({"owner@example.com"}),
        routing=ROUTING,
        oidc_expected_issuer="https://accounts.google.com",
        oidc_expected_audience="https://relay.example/relay",
        oidc_expected_principals=frozenset({"push@example.iam.gserviceaccount.com"}),
        initial_history_id="100",
        firestore_project="daily-duck-test",
    )


def claims(**overrides):
    value = {
        "iss": "https://accounts.google.com",
        "aud": "https://relay.example/relay",
        "email": "push@example.iam.gserviceaccount.com",
        "email_verified": True,
    }
    value.update(overrides)
    return value


def make_auth(verifier=None) -> PushAuthenticator:
    return PushAuthenticator(
        expected_issuer="https://accounts.google.com",
        expected_audience="https://relay.example/relay",
        expected_principals=frozenset({"push@example.iam.gserviceaccount.com"}),
        token_verifier=verifier or (lambda token, audience: claims()),
    )


class AuthenticationTests(unittest.TestCase):
    def test_missing_and_malformed_bearer_rejected(self):
        auth = make_auth()
        for header in (None, "", "Basic token", "Bearer", "Bearer a b"):
            with self.subTest(header=header), self.assertRaises(AuthenticationError):
                auth.verify(header)

    def test_invalid_token_wrong_issuer_audience_and_principal_rejected(self):
        cases = [
            lambda token, audience: (_ for _ in ()).throw(ValueError("bad")),
            lambda token, audience: claims(iss="https://evil.example"),
            lambda token, audience: claims(aud="wrong"),
            lambda token, audience: claims(email="other@example.com"),
        ]
        for verifier in cases:
            with self.assertRaises(AuthenticationError):
                make_auth(verifier).verify("Bearer token")

    def test_valid_fake_verifier_result_accepted(self):
        make_auth().verify("Bearer token")


class FlaskAppTests(unittest.TestCase):
    def _app(self, *, gmail=None, dispatcher=None):
        reader = gmail or FakeGmailReader(
            history=HistoryBatch(("m1",)),
            messages={
                "m1": MessageMetadata("m1", GATE_A_PATTERN, "owner@example.com")
            },
        )
        return create_app(
            config=make_config(),
            gmail=reader,
            authenticator=make_auth(),
            dispatcher=dispatcher,
        )

    def test_missing_dependencies_fail_closed(self):
        with self.assertRaises(ConfigurationError):
            create_app()
        with self.assertRaises(ConfigurationError):
            create_app_from_env(
                env={},
                gmail_reader_factory=lambda: FakeGmailReader(),
                token_verifier_factory=lambda: (lambda token, audience: claims()),
            )

    def test_production_constructor_is_dry_run_and_does_no_gmail_call(self):
        env = {
            "RELAY_MAILBOX_IDENTITY": "owner@example.com",
            "RELAY_ALLOWED_SENDERS": "owner@example.com",
            "RELAY_GATE_A_SUBJECT_PATTERN": GATE_A_PATTERN,
            "RELAY_DESIGN_SUBJECT_PATTERN": DESIGN_PATTERN,
            "RELAY_OIDC_EXPECTED_ISSUER": "https://accounts.google.com",
            "RELAY_OIDC_EXPECTED_AUDIENCE": "https://relay.example/relay",
            "RELAY_OIDC_EXPECTED_PRINCIPALS": "push@example.iam.gserviceaccount.com",
            "RELAY_GMAIL_INITIAL_HISTORY_ID": "100",
            "RELAY_GMAIL_OAUTH_CLIENT_JSON": "{}",
            "RELAY_GMAIL_OAUTH_REFRESH_TOKEN": "secret-not-used",
            "RELAY_FIRESTORE_PROJECT": "daily-duck-test",
        }
        calls = []

        def gmail_factory():
            calls.append("gmail-built")
            return FakeGmailReader()

        fake_firestore_client = FakeFirestoreClient()
        app = create_app_from_env(
            env=env,
            gmail_reader_factory=gmail_factory,
            firestore_client_factory=lambda: fake_firestore_client,
            token_verifier_factory=lambda: (lambda token, audience: claims()),
        )
        self.assertEqual(calls, [])
        components = app.extensions["relay_components"]
        self.assertEqual(components["relay"].mode, RelayMode.DRY_RUN)
        self.assertIsInstance(components["dispatcher"], FakeGitHubDispatcher)
        self.assertIsInstance(components["cursor_store"], FirestoreRelayStorage)
        self.assertIsInstance(components["ledger"], FirestoreRelayStorage)
        self.assertIs(components["cursor_store"], components["ledger"])

        missing_firestore = dict(env)
        del missing_firestore["RELAY_FIRESTORE_PROJECT"]
        with self.assertRaises(ConfigurationError):
            create_app_from_env(
                env=missing_firestore,
                gmail_reader_factory=gmail_factory,
                firestore_client_factory=lambda: fake_firestore_client,
                token_verifier_factory=lambda: (lambda token, audience: claims()),
            )

    def test_production_constructor_rejects_live_mode(self):
        env = {
            "RELAY_MAILBOX_IDENTITY": "owner@example.com",
            "RELAY_ALLOWED_SENDERS": "owner@example.com",
            "RELAY_GATE_A_SUBJECT_PATTERN": GATE_A_PATTERN,
            "RELAY_DESIGN_SUBJECT_PATTERN": DESIGN_PATTERN,
            "RELAY_OIDC_EXPECTED_ISSUER": "https://accounts.google.com",
            "RELAY_OIDC_EXPECTED_AUDIENCE": "https://relay.example/relay",
            "RELAY_OIDC_EXPECTED_PRINCIPALS": "push@example.iam.gserviceaccount.com",
            "RELAY_GMAIL_INITIAL_HISTORY_ID": "100",
            "RELAY_GMAIL_OAUTH_CLIENT_JSON": "{}",
            "RELAY_GMAIL_OAUTH_REFRESH_TOKEN": "secret-not-used",
            "RELAY_FIRESTORE_PROJECT": "daily-duck-test",
            "RELAY_MODE": "LIVE",
        }
        with self.assertRaises(ConfigurationError):
            create_app_from_env(
                env=env,
                gmail_reader_factory=lambda: FakeGmailReader(),
                firestore_client_factory=lambda: FakeFirestoreClient(),
                token_verifier_factory=lambda: (lambda token, audience: claims()),
            )

    def test_auth_runs_before_event_processing(self):
        reader = FakeGmailReader(history=HistoryBatch(("m1",)), messages={})
        response = self._app(gmail=reader).test_client().post("/relay", json={})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(reader.list_history_calls, [])

    def test_valid_real_shaped_push_routes_dry_run_without_dispatch(self):
        dispatcher = FakeGitHubDispatcher()
        app = self._app(dispatcher=dispatcher)
        response = app.test_client().post(
            "/relay",
            json=pubsub_envelope(),
            headers={"Authorization": "Bearer token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["results"], ["DRY_RUN_ROUTED"])
        self.assertEqual(dispatcher.calls, [])
        components = app.extensions["relay_components"]
        self.assertIsInstance(components["dispatcher"], FakeGitHubDispatcher)
        self.assertEqual(components["relay"].mode, RelayMode.DRY_RUN)

    def test_real_ingress_sender_subject_matrix_is_fail_closed_and_sanitized(self):
        messages = {
            "m1": MessageMetadata("m1", GATE_A_PATTERN, "owner@example.com"),
            "m2": MessageMetadata("m2", DESIGN_PATTERN, "owner@example.com"),
            "m3": MessageMetadata("m3", GATE_A_PATTERN, "attacker@example.com"),
            "m4": MessageMetadata("m4", DESIGN_PATTERN, "attacker@example.com"),
            "m5": MessageMetadata("m5", "unrelated", "owner@example.com"),
            "m6": MessageMetadata(
                "m6", f"{GATE_A_PATTERN} {DESIGN_PATTERN}", "owner@example.com"
            ),
            "m7": MessageMetadata("m7", GATE_A_PATTERN, None),
            "m8": MessageMetadata("m8", None, "owner@example.com"),
        }
        reader = FakeGmailReader(
            history=HistoryBatch(tuple(messages)), messages=messages
        )
        dispatcher = FakeGitHubDispatcher()
        app = self._app(gmail=reader, dispatcher=dispatcher)
        response = app.test_client().post(
            "/relay",
            json=pubsub_envelope(),
            headers={"Authorization": "Bearer token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["results"],
            [
                "DRY_RUN_ROUTED",
                "DRY_RUN_ROUTED",
                "UNAUTHORIZED_NO_DISPATCH",
                "UNAUTHORIZED_NO_DISPATCH",
                "UNRELATED_NO_DISPATCH",
                "AMBIGUOUS_NO_DISPATCH",
                "UNAUTHORIZED_NO_DISPATCH",
                "UNRELATED_NO_DISPATCH",
            ],
        )
        self.assertEqual(dispatcher.calls, [])
        ledger = app.extensions["relay_components"]["ledger"]
        self.assertIsNotNone(ledger.get(event_key_for("owner@example.com", "m1")))
        self.assertIsNone(ledger.get(event_key_for("owner@example.com", "m3")))

    def test_wrong_mailbox_rejected(self):
        response = self._app().test_client().post(
            "/relay",
            json=pubsub_envelope(email="wrong@example.com"),
            headers={"Authorization": "Bearer token"},
        )
        self.assertEqual(response.status_code, 400)

    def test_non_string_history_id_is_rejected_before_gmail_access(self):
        reader = FakeGmailReader(history=HistoryBatch(()))
        response = self._app(gmail=reader).test_client().post(
            "/relay",
            json=pubsub_envelope(history_id=12345),
            headers={"Authorization": "Bearer token"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(reader.list_history_calls, [])

    def test_duplicate_notification_is_acked_without_second_history_walk(self):
        reader = FakeGmailReader(history=HistoryBatch(()))
        client = self._app(gmail=reader).test_client()
        for _ in range(2):
            response = client.post(
                "/relay",
                json=pubsub_envelope(),
                headers={"Authorization": "Bearer token"},
            )
            self.assertEqual(response.status_code, 200)
        self.assertEqual(reader.list_history_calls, ["100"])

    def test_failed_message_fetch_does_not_advance_cursor(self):
        reader = FakeGmailReader(
            history=HistoryBatch(("m1",)), messages={"m1": GmailReaderError("fail")}
        )
        cursor_store = InMemoryCursorStore(clock=lambda: "t0")
        app = create_app(
            config=make_config(),
            gmail=reader,
            authenticator=make_auth(),
            cursor_store=cursor_store,
        )
        response = app.test_client().post(
            "/relay",
            json=pubsub_envelope(),
            headers={"Authorization": "Bearer token"},
        )
        self.assertEqual(response.status_code, 500)
        cursor = cursor_store.read_cursor(mailbox_hash_for("owner@example.com"))
        self.assertIsNotNone(cursor)
        self.assertEqual(cursor.history_id, "100")

    def test_cursor_cas_failure_is_retryable_not_success(self):
        class FailingFinalCursorStore(InMemoryCursorStore):
            def __init__(self):
                super().__init__(clock=lambda: "t0")
                self.calls = 0

            def compare_and_update_cursor(
                self, mailbox_hash, expected_history_id, new_history_id
            ):
                self.calls += 1
                if self.calls == 1:
                    return super().compare_and_update_cursor(
                        mailbox_hash, expected_history_id, new_history_id
                    )
                return False

        cursor_store = FailingFinalCursorStore()
        app = create_app(
            config=make_config(),
            gmail=FakeGmailReader(history=HistoryBatch(())),
            authenticator=make_auth(),
            cursor_store=cursor_store,
        )
        response = app.test_client().post(
            "/relay",
            json=pubsub_envelope(),
            headers={"Authorization": "Bearer token"},
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["status"], "RETRY")
        cursor = cursor_store.read_cursor(mailbox_hash_for("owner@example.com"))
        self.assertEqual(cursor.history_id, "100")

    def test_health_endpoint(self):
        response = self._app().test_client().get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "OK")


if __name__ == "__main__":
    unittest.main()
