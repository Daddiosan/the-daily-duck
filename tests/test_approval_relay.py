"""Behavioral test matrix for cloud/approval_relay (Thin Relay, Phase
M3A): routing, dedupe, business retry budget, DRY_RUN, and genuine
multi-threaded concurrency. Security/governance assertions live in
tests/test_approval_relay_contract.py, not here.
"""

from __future__ import annotations

import threading
import unittest
from typing import Any

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
    MalformedRelayEventError,
    MAX_BUSINESS_ATTEMPTS,
    RelayMode,
    RelayService,
    RelayStatus,
    create_app,
    decode_relay_notification,
)
from cloud.approval_relay.router import (
    RelayInboundEvent,
    RelayStage,
    RoutingConfig,
    classify_stage,
    event_key_for,
    workflow_for_stage,
)


GATE_A_PATTERN = "The Daily Duck — Choose Today's Story"
DESIGN_PATTERN = "The Daily Duck — Choose Image + Title"

ROUTING = RoutingConfig(
    gate_a_subject_pattern=GATE_A_PATTERN, design_subject_pattern=DESIGN_PATTERN
)


def make_event(
    *, subject: str, mailbox: str = "owner@example.com", message_id: str = "msg-1"
) -> RelayInboundEvent:
    return RelayInboundEvent(
        mailbox_identity=mailbox, gmail_message_id=message_id, subject=subject
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


class MalformedEventTests(unittest.TestCase):
    def test_missing_gmail_message_id_raises(self):
        with self.assertRaises(MalformedRelayEventError):
            decode_relay_notification(
                {"mailbox_identity": "owner@example.com", "subject": GATE_A_PATTERN}
            )

    def test_missing_subject_raises(self):
        with self.assertRaises(MalformedRelayEventError):
            decode_relay_notification(
                {"mailbox_identity": "owner@example.com", "gmail_message_id": "m1"}
            )

    def test_non_mapping_payload_raises(self):
        with self.assertRaises(MalformedRelayEventError):
            decode_relay_notification("not-a-mapping")  # type: ignore[arg-type]

    def test_valid_payload_decodes(self):
        event = decode_relay_notification(
            {
                "mailbox_identity": "owner@example.com",
                "gmail_message_id": "m1",
                "subject": GATE_A_PATTERN,
            }
        )
        self.assertEqual(event.gmail_message_id, "m1")


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
# Flask adapter (structural smoke coverage; full HTTP/OIDC/Cloud Run wiring
# is explicitly out of scope for this phase).
# ---------------------------------------------------------------------------


class FlaskAppTests(unittest.TestCase):
    def test_default_app_is_dry_run_and_routes_correctly(self):
        app = create_app()
        client = app.test_client()

        response = client.post(
            "/relay",
            json={
                "mailbox_identity": "owner@example.com",
                "gmail_message_id": "m1",
                "subject": GATE_A_PATTERN,
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["status"], RelayStatus.DRY_RUN_ROUTED.value)
        self.assertEqual(body["workflow"], GATE_A_WORKFLOW)

    def test_malformed_payload_acked_not_retried(self):
        app = create_app()
        client = app.test_client()
        response = client.post("/relay", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["status"], RelayStatus.MALFORMED_NO_RETRY.value
        )

    def test_health_endpoint(self):
        app = create_app()
        client = app.test_client()
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "OK")


if __name__ == "__main__":
    unittest.main()
