"""Phase 3B-2 A2 (Phase A, local implementation) tests.

PHASE_A_NOTES documents one non-obvious design decision so a future reader
does not "fix" it back to something that fails scripts/approval_domain.py's
own authorization contract:

scripts/a2_dispatch.py calls build_approval_command() with
source_type=ApprovalSource.GMAIL_POLL, not ApprovalSource.EVENT, even though
A2 is the event-driven path. This is intentional: approval_domain's
_validate_principal_source only allows ApprovalSource.EVENT to pair with a
TrustedPrincipalSource.GITHUB_WORKFLOW_CONTEXT principal (see
tests/test_approval_domain.py's own gate()/design() helpers, which use
trusted_principal_from_github_context only for ApprovalSource.EVENT). A2
classifies a Gmail message before any GitHub Actions execution context
exists, so its trust origin is GMAIL_MESSAGE_METADATA, which is exactly what
ApprovalSource.GMAIL_POLL is authorized to pair with -- regardless of
whether the Gmail message was found by periodic IMAP search or a Gmail push
notification. ApprovalSource.EVENT is reserved for a later, second
authorization check that could run inside the *dispatched* GitHub Actions
workflow itself (checking github.actor against an allowlist, mirroring
scripts/approval_shadow.py), which is out of scope for Phase A.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from scripts.a2_dispatch import (
    A2Decision,
    DispatchAttemptResult,
    DispatchErrorClass,
    EventClassification,
    FakeDispatchAdapter,
    FetchedGmailMessage,
    GitHubAppDispatchAdapter,
    InMemoryMessageDedupeStore,
    InMemoryTransitionLedger,
    build_dispatch_payload,
    classify_dispatch_failure,
    process_gmail_event,
    _observation_id,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_SENDERS = frozenset({"owner@example.com"})
GATE_A_PATTERN = "The Daily Duck — Choose Today's Story"
DESIGN_PATTERN = "The Daily Duck — Choose Image + Title"
ISSUE = "2026-09-21"

GATE_A_SNAPSHOT = {
    "active_issue_date": ISSUE,
    "current_state": "WAITING_STORY_SELECTION",
    "current_command": None,
    "active_design_batch_id": None,
}

DESIGN_SNAPSHOT = {
    "active_issue_date": ISSUE,
    "current_state": "WAITING_FINAL_SELECTION",
    "current_command": None,
    "active_design_batch_id": 4,
}


def gate_a_message(*, message_id="msg-1", body="3", issue=ISSUE, sender="owner@example.com"):
    return FetchedGmailMessage(
        gmail_message_id=message_id,
        sender=sender,
        subject=f"{GATE_A_PATTERN} — {issue}",
        body=body,
    )


def design_message(*, message_id="msg-2", body="1 3", issue=ISSUE, sender="owner@example.com"):
    return FetchedGmailMessage(
        gmail_message_id=message_id,
        sender=sender,
        subject=f"{DESIGN_PATTERN} — {issue} — Batch 4",
        body=body,
    )


def fresh_stores():
    return InMemoryMessageDedupeStore(), InMemoryTransitionLedger()


def call(message, snapshot, *, dedupe=None, ledger=None, adapter=None):
    dedupe = dedupe or InMemoryMessageDedupeStore()
    ledger = ledger or InMemoryTransitionLedger()
    adapter = adapter or FakeDispatchAdapter()
    outcome = process_gmail_event(
        message,
        mailbox_identity="duck@example.com",
        allowed_senders=ALLOWED_SENDERS,
        gate_a_subject_pattern=GATE_A_PATTERN,
        design_subject_pattern=DESIGN_PATTERN,
        production_snapshot=snapshot,
        message_dedupe_store=dedupe,
        transition_ledger=ledger,
        dispatch_adapter=adapter,
    )
    return outcome, dedupe, ledger, adapter


class GateAAndDesignReplyTests(unittest.TestCase):
    def test_01_gate_a_valid_reply_dispatches_approval_check_phase2(self):
        outcome, _, _, adapter = call(gate_a_message(), GATE_A_SNAPSHOT)
        self.assertEqual(outcome.classification, EventClassification.GATE_A_REPLY)
        self.assertEqual(outcome.decision, A2Decision.DISPATCHED)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(adapter.calls[0]["workflow_file"], "approval-check-phase2.yml")
        self.assertEqual(adapter.calls[0]["ref"], "main")

    def test_02_design_valid_reply_dispatches_design_selection_check(self):
        outcome, _, _, adapter = call(design_message(), DESIGN_SNAPSHOT)
        self.assertEqual(outcome.classification, EventClassification.DESIGN_REPLY)
        self.assertEqual(outcome.decision, A2Decision.DISPATCHED)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(adapter.calls[0]["workflow_file"], "design-selection-check.yml")


class ClassificationEdgeCaseTests(unittest.TestCase):
    def test_03_unrelated_email_is_not_dispatched(self):
        message = FetchedGmailMessage(
            gmail_message_id="msg-x",
            sender="owner@example.com",
            subject="Your weekly newsletter",
            body="unsubscribe",
        )
        outcome, _, _, adapter = call(message, GATE_A_SNAPSHOT)
        self.assertEqual(outcome.classification, EventClassification.UNRELATED)
        self.assertEqual(outcome.decision, A2Decision.SKIPPED_UNRELATED)
        self.assertEqual(adapter.calls, [])

    def test_04_duplicate_gmail_event_is_not_reprocessed(self):
        dedupe, ledger = fresh_stores()
        message = gate_a_message()
        first, _, _, adapter = call(message, GATE_A_SNAPSHOT, dedupe=dedupe, ledger=ledger)
        second, _, _, _ = call(message, GATE_A_SNAPSHOT, dedupe=dedupe, ledger=ledger, adapter=adapter)
        self.assertEqual(first.decision, A2Decision.DISPATCHED)
        self.assertEqual(second.decision, A2Decision.SKIPPED_DUPLICATE_EVENT)
        self.assertEqual(second.classification, EventClassification.DUPLICATE_EVENT)
        self.assertEqual(len(adapter.calls), 1)

    def test_05_duplicate_transition_from_two_different_messages_dispatches_once(self):
        dedupe, ledger = fresh_stores()
        adapter = FakeDispatchAdapter()
        first, _, _, _ = call(
            gate_a_message(message_id="msg-a"),
            GATE_A_SNAPSHOT,
            dedupe=dedupe,
            ledger=ledger,
            adapter=adapter,
        )
        second, _, _, _ = call(
            gate_a_message(message_id="msg-b"),
            GATE_A_SNAPSHOT,
            dedupe=dedupe,
            ledger=ledger,
            adapter=adapter,
        )
        self.assertEqual(first.decision, A2Decision.DISPATCHED)
        self.assertEqual(second.decision, A2Decision.SKIPPED_DUPLICATE_TRANSITION)
        self.assertEqual(len(adapter.calls), 1)

    def test_06_stale_reply_for_old_issue_date_is_not_dispatched(self):
        message = gate_a_message(issue="2026-09-20")
        outcome, _, _, adapter = call(message, GATE_A_SNAPSHOT)
        self.assertEqual(outcome.classification, EventClassification.STALE_REPLY)
        self.assertEqual(outcome.decision, A2Decision.SKIPPED_STALE)
        self.assertEqual(adapter.calls, [])

    def test_07_invalid_reply_body_is_not_dispatched(self):
        message = gate_a_message(body="3 OK")
        outcome, _, _, adapter = call(message, GATE_A_SNAPSHOT)
        self.assertEqual(outcome.classification, EventClassification.INVALID_REPLY)
        self.assertEqual(outcome.decision, A2Decision.SKIPPED_INVALID)
        self.assertEqual(adapter.calls, [])

    def test_07b_unauthorized_sender_is_invalid_not_dispatched(self):
        message = gate_a_message(sender="attacker@example.com")
        outcome, _, _, adapter = call(message, GATE_A_SNAPSHOT)
        self.assertEqual(outcome.decision, A2Decision.SKIPPED_INVALID)
        self.assertEqual(adapter.calls, [])


class FakeDispatchTests(unittest.TestCase):
    def test_08_fake_dispatch_success_records_call_and_no_network_module_used(self):
        outcome, _, _, adapter = call(gate_a_message(), GATE_A_SNAPSHOT)
        self.assertEqual(outcome.decision, A2Decision.DISPATCHED)
        self.assertEqual(len(adapter.calls), 1)
        import scripts.a2_dispatch as mod

        for name in ("requests", "urllib", "http.client", "socket"):
            self.assertNotIn(name, mod.__dict__)


class DispatchFailureClassificationTests(unittest.TestCase):
    def test_09_retryable_http_statuses_are_classified_retryable(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                result = DispatchAttemptResult(success=False, http_status=status)
                self.assertEqual(classify_dispatch_failure(result), DispatchErrorClass.RETRYABLE)
        for kind in ("TIMEOUT", "NETWORK_ERROR"):
            with self.subTest(kind=kind):
                result = DispatchAttemptResult(success=False, error_kind=kind)
                self.assertEqual(classify_dispatch_failure(result), DispatchErrorClass.RETRYABLE)

    def test_09b_retryable_failure_end_to_end_releases_ledger_for_manual_retry(self):
        dedupe, ledger = fresh_stores()
        adapter = FakeDispatchAdapter(
            results=[DispatchAttemptResult(success=False, http_status=503)]
        )
        outcome, _, _, _ = call(
            gate_a_message(message_id="msg-retry"),
            GATE_A_SNAPSHOT,
            dedupe=dedupe,
            ledger=ledger,
            adapter=adapter,
        )
        self.assertEqual(outcome.decision, A2Decision.DISPATCH_FAILED_RETRYABLE)
        # The message itself is NOT marked done, so a Pub/Sub redelivery of
        # the identical message can reprocess it (this is what "retry
        # responsibility is Pub/Sub's" means in practice).
        self.assertFalse(
            dedupe.contains(_observation_id("duck@example.com", "msg-retry"))
        )
        # The transition reservation was released, so a fresh attempt for
        # the same command can proceed instead of being told "duplicate".
        second_adapter = FakeDispatchAdapter()
        second_outcome, _, _, _ = call(
            gate_a_message(message_id="msg-retry"),
            GATE_A_SNAPSHOT,
            dedupe=dedupe,
            ledger=ledger,
            adapter=second_adapter,
        )
        self.assertEqual(second_outcome.decision, A2Decision.DISPATCHED)
        self.assertEqual(len(second_adapter.calls), 1)

    def test_10_non_retryable_statuses_and_kinds_are_classified_non_retryable(self):
        for status in (400, 401, 403):
            with self.subTest(status=status):
                result = DispatchAttemptResult(success=False, http_status=status)
                self.assertEqual(
                    classify_dispatch_failure(result), DispatchErrorClass.NON_RETRYABLE
                )
        for kind in (
            "INVALID_SCHEMA",
            "MISSING_SECRET",
            "CONFIGURATION_ERROR",
            "AUTHENTICATION_ERROR",
        ):
            with self.subTest(kind=kind):
                result = DispatchAttemptResult(success=False, error_kind=kind)
                self.assertEqual(
                    classify_dispatch_failure(result), DispatchErrorClass.NON_RETRYABLE
                )

    def test_10b_unrecognized_failure_fails_closed_to_non_retryable(self):
        result = DispatchAttemptResult(success=False)
        self.assertEqual(classify_dispatch_failure(result), DispatchErrorClass.NON_RETRYABLE)

    def test_10c_non_retryable_failure_end_to_end(self):
        adapter = FakeDispatchAdapter(
            results=[DispatchAttemptResult(success=False, http_status=403)]
        )
        outcome, _, _, _ = call(gate_a_message(), GATE_A_SNAPSHOT, adapter=adapter)
        self.assertEqual(outcome.decision, A2Decision.DISPATCH_FAILED_NON_RETRYABLE)


class PayloadSafetyTests(unittest.TestCase):
    def test_11_payload_never_contains_full_body_or_subject_text(self):
        outcome, _, _, _ = call(gate_a_message(body="3"), GATE_A_SNAPSHOT)
        self.assertIsNotNone(outcome.payload)
        values = " ".join(str(v) for v in outcome.payload.values())
        self.assertNotIn(GATE_A_PATTERN, values)
        self.assertEqual(
            set(outcome.payload.keys()),
            {
                "stage",
                "issue_date",
                "command",
                "design_batch_id",
                "idempotency_key",
                "transition_key",
                "source_event_id",
                "authorized_principal",
            },
        )

    def test_12_payload_and_adapter_never_carry_credentials(self):
        outcome, _, _, _ = call(gate_a_message(), GATE_A_SNAPSHOT)
        values = " ".join(str(v) for v in outcome.payload.values())
        for forbidden in ("BEGIN PRIVATE KEY", "ghp_", "token", "secret", "password"):
            self.assertNotIn(forbidden.lower(), values.lower())

        adapter = GitHubAppDispatchAdapter(
            app_id="123456",
            private_key_pem="-----BEGIN PRIVATE KEY-----FAKE-DO-NOT-USE-----END PRIVATE KEY-----",
            installation_id="987654",
            repository="Daddiosan/the-daily-duck",
        )
        with self.assertRaises(NotImplementedError):
            adapter.dispatch_workflow(
                workflow_file="approval-check-phase2.yml",
                ref="main",
                inputs=build_dispatch_payload(
                    call(gate_a_message(message_id="msg-repr"), GATE_A_SNAPSHOT)[0].command
                ),
            )
        self.assertNotIn("BEGIN PRIVATE KEY", repr(adapter))
        self.assertNotIn("FAKE-DO-NOT-USE", repr(adapter))


class LegacyPollingUntouchedTests(unittest.TestCase):
    def test_13_legacy_polling_cron_schedules_are_unchanged(self):
        gate_a_yml = (
            REPO_ROOT / ".github" / "workflows" / "approval-check-phase2.yml"
        ).read_text(encoding="utf-8")
        design_yml = (
            REPO_ROOT / ".github" / "workflows" / "design-selection-check.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('cron: "11,26,41,56 * * * *"', gate_a_yml)
        self.assertIn('cron: "9,24,39,54 * * * *"', design_yml)

    def test_14_a2_module_never_imports_imaplib_or_smtplib(self):
        source = (
            REPO_ROOT / "scripts" / "a2_dispatch.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("imaplib", source)
        self.assertNotIn("smtplib", source)
        self.assertIsNone(re.search(r"^\s*import\s+requests", source, re.MULTILINE))

    def test_15_same_event_cannot_cause_two_dispatches(self):
        dedupe, ledger = fresh_stores()
        adapter = FakeDispatchAdapter()
        message = gate_a_message(message_id="msg-once")
        for _ in range(3):
            call(message, GATE_A_SNAPSHOT, dedupe=dedupe, ledger=ledger, adapter=adapter)
        self.assertEqual(len(adapter.calls), 1)


if __name__ == "__main__":
    unittest.main()
