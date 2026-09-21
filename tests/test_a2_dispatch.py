"""Phase 3B-2 A2 (Phase A + Phase B) tests.

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
notification. ApprovalSourceContractTests below proves this against
approval_domain.py directly (not just by assertion in this docstring):
EVENT+GMAIL_MESSAGE_METADATA is rejected by
_validate_principal_source's TRUSTED_PRINCIPAL_SOURCE_MISMATCH, so A2 could
not use EVENT even if it wanted to, without approval_domain.py itself being
changed (out of scope here; see Phase B report's APPROVAL_SOURCE_VERDICT).
ApprovalSource.EVENT's only current consumer, scripts/approval_shadow.py, is
a *different* kind of event: a human-triggered workflow_dispatch through
GitHub's own UI, whose trust origin genuinely is a GitHub actor. A2's Gmail
push trigger is not that.

PHASE_B_NOTES documents what the Phase B adversarial review changed in
scripts/a2_dispatch.py and why, so a future reader does not mistake these for
arbitrary refactors:

1. AMBIGUOUS DISPATCH OUTCOME (the critical finding). Phase A classified a
   bare "TIMEOUT" or "NETWORK_ERROR" as RETRYABLE and released the
   transition ledger reservation on any dispatch failure, RETRYABLE
   included. AmbiguousDispatchOutcomeTests proves that this combination lets
   dispatch #1 be accepted by GitHub, its response get lost, the ledger get
   released, and a redelivery of the same or an equivalent message request
   dispatch #2 -- a real, GitHub-visible duplicate workflow_dispatch call,
   not merely a duplicate business side effect (which downstream state
   guards would likely still catch, but this review was asked not to lean on
   that). Phase B fixes this at the root: only error kinds that PROVE the
   request never left the process (CONNECT_TIMEOUT, DNS_FAILURE,
   CONNECTION_REFUSED) are RETRYABLE now. A bare/legacy TIMEOUT,
   NETWORK_ERROR, an explicit RESPONSE_TIMEOUT, an adapter exception, and
   any completely unrecognized failure are all AMBIGUOUS, which leaves the
   transition_key reservation in place (DispatchOutcomeState.UNKNOWN_OUTCOME)
   instead of releasing it, so no automatic path -- not a Pub/Sub
   redelivery, not a second unrelated Gmail message carrying the same
   command -- can request a second dispatch. Resolving an UNKNOWN_OUTCOME
   requires a separate, explicitly-named, not-yet-built reconciliation path
   (InMemoryTransitionLedger.resolve_unknown_outcome), never automatic
   retry.

2. DESIGN STALE-BATCH SUBJECT CHECK. Synthetic testing surfaced that A2's
   own subject-staleness check ignored the design batch number, and that
   build_approval_command's STALE_DESIGN_BATCH check could never fire from
   A2 because A2 always reads BOTH the "declared" and "expected" batch id
   from the same production_snapshot field (a Gmail reply's wire text -
   "1 3" or "NEXT 3" - never states which batch it answers; only the
   subject does, as "... - Batch N", per
   scripts/send_design_approval_email.py's build_email()). Without checking
   the batch number in the subject, a reply to an old batch's email would
   silently be evaluated against the CURRENT batch's concepts. Phase B adds
   the batch number to the expected-subject check for DESIGN_SELECTION.

3. should_acknowledge_to_pubsub() makes the ack/nack-per-decision retry
   boundary an explicit, tested function instead of only documentation, so
   "NON_RETRYABLE and DISPATCH_OUTCOME_UNKNOWN can never cause Pub/Sub to
   redeliver" is something CI can actually verify field-by-field as new
   A2Decision values are added later.

This module never contacts a real GitHub, Gmail, or Pub/Sub endpoint. All
scenarios are synthesized locally via FakeDispatchAdapter and the in-memory
stores.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from scripts.a2_dispatch import (
    A2Decision,
    DispatchAttemptResult,
    DispatchErrorClass,
    DispatchOutcomeState,
    EventClassification,
    FakeDispatchAdapter,
    FetchedGmailMessage,
    GitHubAppDispatchAdapter,
    InMemoryMessageDedupeStore,
    InMemoryTransitionLedger,
    build_dispatch_payload,
    classify_dispatch_failure,
    process_gmail_event,
    should_acknowledge_to_pubsub,
    _observation_id,
)
from scripts.approval_domain import (
    ApprovalSource,
    ApprovalStage,
    ApprovalValidationError,
    build_approval_command,
    trusted_principal_from_gmail_metadata,
    trusted_principal_from_github_context,
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


def design_message(
    *,
    message_id="msg-2",
    body="1 3",
    issue=ISSUE,
    batch=4,
    sender="owner@example.com",
):
    return FetchedGmailMessage(
        gmail_message_id=message_id,
        sender=sender,
        subject=f"{DESIGN_PATTERN} — {issue} — Batch {batch}",
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


# ---------------------------------------------------------------------------
# Section 2: ApprovalSource contract review
# ---------------------------------------------------------------------------


class ApprovalSourceContractTests(unittest.TestCase):
    """Proves the ApprovalSource.GMAIL_POLL choice against
    scripts/approval_domain.py's own authorization contract, rather than
    only asserting it in prose."""

    def test_event_source_rejects_gmail_metadata_principal(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            build_approval_command(
                stage=ApprovalStage.GATE_A,
                issue_date=ISSUE,
                command="3",
                source_type=ApprovalSource.EVENT,
                trusted_principal=trusted_principal_from_gmail_metadata(
                    "owner@example.com"
                ),
                allowed_principals=ALLOWED_SENDERS,
                message_id="msg-1",
            )
        self.assertEqual(
            caught.exception.reason, "TRUSTED_PRINCIPAL_SOURCE_MISMATCH"
        )

    def test_gmail_poll_source_accepts_gmail_metadata_principal(self):
        command = build_approval_command(
            stage=ApprovalStage.GATE_A,
            issue_date=ISSUE,
            command="3",
            source_type=ApprovalSource.GMAIL_POLL,
            trusted_principal=trusted_principal_from_gmail_metadata(
                "owner@example.com"
            ),
            allowed_principals=ALLOWED_SENDERS,
            message_id="msg-1",
        )
        self.assertEqual(command.command, "SELECT_STORY:3")

    def test_gmail_poll_source_rejects_github_actor_principal(self):
        # The inverse pairing is also rejected: GMAIL_POLL requires
        # GMAIL_MESSAGE_METADATA, not GITHUB_WORKFLOW_CONTEXT. This shows
        # the restriction is a real two-way contract, not a one-off
        # special case for EVENT.
        # Use an identity that IS on the allowlist so authorize_principal's
        # earlier allowlist check passes and the assertion actually
        # exercises _validate_principal_source's source-type mismatch,
        # rather than being masked by an unrelated UNAUTHORIZED_TRUSTED_
        # PRINCIPAL from a name that was never allowed in the first place.
        with self.assertRaises(ApprovalValidationError) as caught:
            build_approval_command(
                stage=ApprovalStage.GATE_A,
                issue_date=ISSUE,
                command="3",
                source_type=ApprovalSource.GMAIL_POLL,
                trusted_principal=trusted_principal_from_github_context(
                    "owner@example.com"
                ),
                allowed_principals=ALLOWED_SENDERS,
            )
        self.assertEqual(
            caught.exception.reason, "TRUSTED_PRINCIPAL_SOURCE_MISMATCH"
        )

    def test_a2_module_actually_uses_gmail_poll_end_to_end(self):
        outcome, _, _, _ = call(gate_a_message(), GATE_A_SNAPSHOT)
        self.assertEqual(outcome.command.source_type, ApprovalSource.GMAIL_POLL)


# ---------------------------------------------------------------------------
# Section 3: Synthetic event matrix
# ---------------------------------------------------------------------------


class GateAAndDesignReplyTests(unittest.TestCase):
    def test_01_gate_a_valid_reply_dispatches_approval_check_phase2(self):
        outcome, _, _, adapter = call(gate_a_message(), GATE_A_SNAPSHOT)
        self.assertEqual(outcome.classification, EventClassification.GATE_A_REPLY)
        self.assertEqual(outcome.decision, A2Decision.DISPATCHED)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(adapter.calls[0]["workflow_file"], "approval-check-phase2.yml")
        self.assertEqual(adapter.calls[0]["ref"], "main")

    def test_01b_gate_a_valid_boundary_values_1_and_5(self):
        for value in ("1", "5"):
            with self.subTest(value=value):
                outcome, _, _, adapter = call(
                    gate_a_message(message_id=f"msg-{value}", body=value),
                    GATE_A_SNAPSHOT,
                )
                self.assertEqual(outcome.decision, A2Decision.DISPATCHED)
                self.assertEqual(len(adapter.calls), 1)

    def test_02_design_valid_reply_dispatches_design_selection_check(self):
        outcome, _, _, adapter = call(design_message(), DESIGN_SNAPSHOT)
        self.assertEqual(outcome.classification, EventClassification.DESIGN_REPLY)
        self.assertEqual(outcome.decision, A2Decision.DISPATCHED)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(adapter.calls[0]["workflow_file"], "design-selection-check.yml")

    def test_02b_design_next_3_also_dispatches(self):
        outcome, _, _, adapter = call(
            design_message(message_id="msg-next3", body="NEXT 3"), DESIGN_SNAPSHOT
        )
        self.assertEqual(outcome.decision, A2Decision.DISPATCHED)
        self.assertEqual(outcome.command.command, "NEXT_3")
        self.assertEqual(adapter.calls[0]["workflow_file"], "design-selection-check.yml")

    def test_02c_design_full_width_digits_and_space_are_normalized(self):
        # extract_design_command_from_gmail NFKC-normalizes (unlike Gate A);
        # "１　３" (full-width digits + full-width space) must equal "1 3".
        outcome, _, _, adapter = call(
            design_message(message_id="msg-fw", body="１　３"), DESIGN_SNAPSHOT
        )
        self.assertEqual(outcome.decision, A2Decision.DISPATCHED)
        self.assertEqual(outcome.command.command, "SELECT_DESIGN:1:3")


class GateAClassificationMatrixTests(unittest.TestCase):
    def test_invalid_command_0_is_rejected(self):
        outcome, _, _, adapter = call(gate_a_message(body="0"), GATE_A_SNAPSHOT)
        self.assertEqual(outcome.decision, A2Decision.SKIPPED_INVALID)
        self.assertEqual(adapter.calls, [])

    def test_invalid_command_6_is_rejected(self):
        outcome, _, _, adapter = call(gate_a_message(body="6"), GATE_A_SNAPSHOT)
        self.assertEqual(outcome.decision, A2Decision.SKIPPED_INVALID)
        self.assertEqual(adapter.calls, [])

    def test_full_width_digit_is_rejected_not_normalized(self):
        # normalize_gate_command deliberately does NOT NFKC-normalize
        # (approval_domain.py's own comment: doing so "would silently
        # broaden the accepted Gate A syntax"). A2 must inherit that
        # strictness via extract_gate_a_command_from_gmail, not work around
        # it.
        outcome, _, _, adapter = call(gate_a_message(body="３"), GATE_A_SNAPSHOT)
        self.assertEqual(outcome.decision, A2Decision.SKIPPED_INVALID)
        self.assertEqual(adapter.calls, [])

    def test_stale_issue_is_not_dispatched(self):
        outcome, _, _, adapter = call(
            gate_a_message(issue="2026-09-20"), GATE_A_SNAPSHOT
        )
        self.assertEqual(outcome.classification, EventClassification.STALE_REPLY)
        self.assertEqual(outcome.decision, A2Decision.SKIPPED_STALE)
        self.assertEqual(adapter.calls, [])

    def test_unauthorized_sender_is_invalid_not_dispatched(self):
        outcome, _, _, adapter = call(
            gate_a_message(sender="attacker@example.com"), GATE_A_SNAPSHOT
        )
        self.assertEqual(outcome.decision, A2Decision.SKIPPED_INVALID)
        self.assertEqual(adapter.calls, [])

    def test_wrong_subject_is_unrelated(self):
        message = FetchedGmailMessage(
            gmail_message_id="msg-wrong-subject",
            sender="owner@example.com",
            subject="Re: dinner plans",
            body="3",
        )
        outcome, _, _, adapter = call(message, GATE_A_SNAPSHOT)
        self.assertEqual(outcome.classification, EventClassification.UNRELATED)
        self.assertEqual(adapter.calls, [])

    def test_duplicate_gmail_message_is_not_reprocessed(self):
        dedupe, ledger = fresh_stores()
        message = gate_a_message()
        first, _, _, adapter = call(message, GATE_A_SNAPSHOT, dedupe=dedupe, ledger=ledger)
        second, _, _, _ = call(
            message, GATE_A_SNAPSHOT, dedupe=dedupe, ledger=ledger, adapter=adapter
        )
        self.assertEqual(first.decision, A2Decision.DISPATCHED)
        self.assertEqual(second.decision, A2Decision.SKIPPED_DUPLICATE_EVENT)
        self.assertEqual(second.classification, EventClassification.DUPLICATE_EVENT)
        self.assertEqual(len(adapter.calls), 1)

    def test_same_transition_from_different_message_ids_dispatches_once(self):
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


class DesignClassificationMatrixTests(unittest.TestCase):
    def test_malformed_selection_is_rejected(self):
        # "4 1": concept 4 is out of the 1-3 range, matches neither the
        # FINAL nor the NEXT_3 wire grammar.
        outcome, _, _, adapter = call(
            design_message(body="4 1"), DESIGN_SNAPSHOT
        )
        self.assertEqual(outcome.decision, A2Decision.SKIPPED_INVALID)
        self.assertEqual(adapter.calls, [])

    def test_stale_batch_reply_is_not_dispatched(self):
        # Subject references Batch 3; production_snapshot says the active
        # batch is 4 (a NEXT 3 already rotated it). Phase B fix: this must
        # be caught at the subject layer, since build_approval_command's own
        # STALE_DESIGN_BATCH check cannot see the message's actual batch.
        message = design_message(batch=3)
        outcome, _, _, adapter = call(message, DESIGN_SNAPSHOT)
        self.assertEqual(outcome.classification, EventClassification.STALE_REPLY)
        self.assertEqual(outcome.decision, A2Decision.SKIPPED_STALE)
        self.assertEqual(adapter.calls, [])

    def test_unauthorized_sender_is_invalid_not_dispatched(self):
        outcome, _, _, adapter = call(
            design_message(sender="attacker@example.com"), DESIGN_SNAPSHOT
        )
        self.assertEqual(outcome.decision, A2Decision.SKIPPED_INVALID)
        self.assertEqual(adapter.calls, [])

    def test_wrong_subject_is_unrelated(self):
        message = FetchedGmailMessage(
            gmail_message_id="msg-design-wrong-subject",
            sender="owner@example.com",
            subject="Re: dinner plans",
            body="1 3",
        )
        outcome, _, _, adapter = call(message, DESIGN_SNAPSHOT)
        self.assertEqual(outcome.classification, EventClassification.UNRELATED)
        self.assertEqual(adapter.calls, [])

    def test_duplicate_gmail_message_is_not_reprocessed(self):
        dedupe, ledger = fresh_stores()
        message = design_message()
        first, _, _, adapter = call(
            message, DESIGN_SNAPSHOT, dedupe=dedupe, ledger=ledger
        )
        second, _, _, _ = call(
            message, DESIGN_SNAPSHOT, dedupe=dedupe, ledger=ledger, adapter=adapter
        )
        self.assertEqual(first.decision, A2Decision.DISPATCHED)
        self.assertEqual(second.decision, A2Decision.SKIPPED_DUPLICATE_EVENT)
        self.assertEqual(len(adapter.calls), 1)

    def test_same_transition_from_different_message_ids_dispatches_once(self):
        dedupe, ledger = fresh_stores()
        adapter = FakeDispatchAdapter()
        first, _, _, _ = call(
            design_message(message_id="msg-design-a"),
            DESIGN_SNAPSHOT,
            dedupe=dedupe,
            ledger=ledger,
            adapter=adapter,
        )
        second, _, _, _ = call(
            design_message(message_id="msg-design-b"),
            DESIGN_SNAPSHOT,
            dedupe=dedupe,
            ledger=ledger,
            adapter=adapter,
        )
        self.assertEqual(first.decision, A2Decision.DISPATCHED)
        self.assertEqual(second.decision, A2Decision.SKIPPED_DUPLICATE_TRANSITION)
        self.assertEqual(len(adapter.calls), 1)


class UnrelatedEmailMatrixTests(unittest.TestCase):
    def test_allowed_sender_unrelated_subject(self):
        message = FetchedGmailMessage(
            gmail_message_id="msg-u1",
            sender="owner@example.com",
            subject="Your weekly newsletter",
            body="unsubscribe",
        )
        outcome, _, _, adapter = call(message, GATE_A_SNAPSHOT)
        self.assertEqual(outcome.classification, EventClassification.UNRELATED)
        self.assertEqual(adapter.calls, [])

    def test_unrelated_sender_with_matching_looking_subject_is_invalid(self):
        # Subject exactly matches Gate A's expected subject, body is a
        # well-formed "3", but the sender is not on the allowlist. This is
        # deliberately classified INVALID (an untrusted approval attempt),
        # not UNRELATED (random noise): the same fail-closed
        # authorize_principal() path used by test_unauthorized_sender_*
        # above, just phrased at the "everything else looked legitimate"
        # end of the spectrum.
        message = gate_a_message(sender="attacker@example.com")
        outcome, _, _, adapter = call(message, GATE_A_SNAPSHOT)
        self.assertEqual(outcome.classification, EventClassification.INVALID_REPLY)
        self.assertEqual(adapter.calls, [])

    def test_neither_sender_nor_subject_match(self):
        message = FetchedGmailMessage(
            gmail_message_id="msg-u3",
            sender="nobody@example.com",
            subject="Totally unrelated",
            body="whatever",
        )
        outcome, _, _, adapter = call(message, GATE_A_SNAPSHOT)
        self.assertEqual(outcome.classification, EventClassification.UNRELATED)
        self.assertEqual(adapter.calls, [])


# ---------------------------------------------------------------------------
# Section 4: Ambiguous dispatch failure (critical)
# ---------------------------------------------------------------------------


class AmbiguousDispatchOutcomeTests(unittest.TestCase):
    def test_lost_response_after_accepted_dispatch_does_not_permit_redispatch(self):
        """The exact scenario Phase B was asked to test: GitHub accepts the
        workflow_dispatch call, but A2 never receives the response (the
        connection times out after the request was already sent)."""

        dedupe, ledger = fresh_stores()
        adapter = FakeDispatchAdapter(
            results=[
                DispatchAttemptResult(
                    success=False,
                    error_kind="RESPONSE_TIMEOUT",
                    detail="response lost after request was transmitted",
                )
            ]
        )
        outcome, _, _, _ = call(
            gate_a_message(message_id="msg-ambiguous"),
            GATE_A_SNAPSHOT,
            dedupe=dedupe,
            ledger=ledger,
            adapter=adapter,
        )
        self.assertEqual(outcome.decision, A2Decision.DISPATCH_OUTCOME_UNKNOWN)
        self.assertEqual(outcome.dispatch_state, DispatchOutcomeState.UNKNOWN_OUTCOME)
        self.assertEqual(len(adapter.calls), 1)

        # A Pub/Sub redelivery of the identical message must not attempt a
        # second dispatch: the message is marked handled...
        second_adapter = FakeDispatchAdapter()
        second_outcome, _, _, _ = call(
            gate_a_message(message_id="msg-ambiguous"),
            GATE_A_SNAPSHOT,
            dedupe=dedupe,
            ledger=ledger,
            adapter=second_adapter,
        )
        self.assertEqual(second_outcome.decision, A2Decision.SKIPPED_DUPLICATE_EVENT)
        self.assertEqual(second_adapter.calls, [])

        # ... and even a DIFFERENT Gmail message carrying the identical
        # business command (the scenario downstream state guards should
        # never have to be relied on for) is blocked at the ledger, not
        # just at message-level dedupe.
        third_adapter = FakeDispatchAdapter()
        third_outcome, _, _, _ = call(
            gate_a_message(message_id="msg-ambiguous-resend"),
            GATE_A_SNAPSHOT,
            dedupe=dedupe,
            ledger=ledger,
            adapter=third_adapter,
        )
        self.assertEqual(
            third_outcome.decision, A2Decision.SKIPPED_DUPLICATE_TRANSITION
        )
        self.assertEqual(third_adapter.calls, [])

    def test_phase_a_behavior_would_have_allowed_the_duplicate(self):
        """Documents the actual Phase A bug this review found, so a future
        change cannot silently regress back to it. Simulates Phase A's
        release-on-any-failure policy directly against the ledger (not by
        reintroducing the old code), and shows a second dispatch becomes
        possible under that policy."""

        ledger = InMemoryTransitionLedger()
        transition_key = "would-be-duplicate"
        self.assertTrue(ledger.reserve_if_absent(transition_key))
        ledger.mark_attempted(transition_key)
        # Phase A: any failure, including a lost-response timeout, called
        # release() unconditionally.
        ledger.release(transition_key)
        # Under that policy, nothing blocks a second reservation for the
        # same transition -- which is precisely how dispatch #2 could fire.
        self.assertTrue(ledger.reserve_if_absent(transition_key))

    def test_uncaught_adapter_exception_is_treated_as_unknown_not_retryable(self):
        class RaisingAdapter:
            def __init__(self):
                self.calls = []

            def dispatch_workflow(self, *, workflow_file, ref, inputs):
                self.calls.append(1)
                raise TimeoutError("connection reset by peer")

        adapter = RaisingAdapter()
        outcome, _, ledger, _ = call(
            gate_a_message(message_id="msg-raise"), GATE_A_SNAPSHOT, adapter=adapter
        )
        self.assertEqual(outcome.decision, A2Decision.DISPATCH_OUTCOME_UNKNOWN)
        self.assertEqual(outcome.dispatch_result.error_kind, "EXCEPTION")
        self.assertEqual(len(adapter.calls), 1)
        # The reservation is still blocking, exactly as for a returned
        # ambiguous DispatchAttemptResult.
        self.assertFalse(ledger.reserve_if_absent(outcome.command.transition_key))

    def test_unknown_outcome_requires_explicit_reconciliation_not_auto_release(self):
        ledger = InMemoryTransitionLedger()
        key = "needs-reconciliation"
        ledger.reserve_if_absent(key)
        ledger.mark_attempted(key)
        ledger.mark_unknown_outcome(key)
        # No implicit path back to reservable.
        self.assertFalse(ledger.reserve_if_absent(key))
        with self.assertRaises(ValueError):
            # Cannot resolve a key that was never in UNKNOWN_OUTCOME.
            ledger.resolve_unknown_outcome("never-reserved", confirmed=False)
        # A reconciliation job confirming GitHub did NOT create the run may
        # release it for a fresh, deliberate retry.
        ledger.resolve_unknown_outcome(key, confirmed=False)
        self.assertTrue(ledger.reserve_if_absent(key))

    def test_unknown_outcome_confirmed_present_blocks_forever(self):
        ledger = InMemoryTransitionLedger()
        key = "confirmed-by-reconciliation"
        ledger.reserve_if_absent(key)
        ledger.mark_attempted(key)
        ledger.mark_unknown_outcome(key)
        ledger.resolve_unknown_outcome(key, confirmed=True)
        self.assertEqual(ledger.state_of(key), DispatchOutcomeState.CONFIRMED)
        self.assertFalse(ledger.reserve_if_absent(key))


# ---------------------------------------------------------------------------
# Section 5: Retry matrix
# ---------------------------------------------------------------------------


class RetryMatrixTests(unittest.TestCase):
    def test_retryable_kinds_prove_the_request_never_left(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                result = DispatchAttemptResult(success=False, http_status=status)
                self.assertEqual(
                    classify_dispatch_failure(result), DispatchErrorClass.RETRYABLE
                )
        for kind in ("CONNECT_TIMEOUT", "DNS_FAILURE", "CONNECTION_REFUSED"):
            with self.subTest(kind=kind):
                result = DispatchAttemptResult(success=False, error_kind=kind)
                self.assertEqual(
                    classify_dispatch_failure(result), DispatchErrorClass.RETRYABLE
                )

    def test_ambiguous_kinds_are_not_retryable(self):
        for kind in ("TIMEOUT", "RESPONSE_TIMEOUT", "NETWORK_ERROR", "EXCEPTION"):
            with self.subTest(kind=kind):
                result = DispatchAttemptResult(success=False, error_kind=kind)
                self.assertEqual(
                    classify_dispatch_failure(result), DispatchErrorClass.AMBIGUOUS
                )

    def test_non_retryable_statuses_and_kinds(self):
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

    def test_unrecognized_failure_fails_safe_to_ambiguous_not_non_retryable(self):
        # Phase B correction: Phase A defaulted this to NON_RETRYABLE, which
        # incorrectly implied "confirmed not sent." An unrecognized failure
        # proves nothing either way, so it must be AMBIGUOUS.
        result = DispatchAttemptResult(success=False)
        self.assertEqual(classify_dispatch_failure(result), DispatchErrorClass.AMBIGUOUS)

    def test_non_retryable_end_to_end_releases_ledger_and_acks(self):
        adapter = FakeDispatchAdapter(
            results=[DispatchAttemptResult(success=False, http_status=403)]
        )
        outcome, _, ledger, _ = call(gate_a_message(), GATE_A_SNAPSHOT, adapter=adapter)
        self.assertEqual(outcome.decision, A2Decision.DISPATCH_FAILED_NON_RETRYABLE)
        self.assertEqual(outcome.dispatch_state, DispatchOutcomeState.FAILED_FINAL)
        self.assertTrue(should_acknowledge_to_pubsub(outcome.decision))
        # Released: a human who fixes the underlying cause may retry.
        self.assertTrue(ledger.reserve_if_absent(outcome.command.transition_key))

    def test_retryable_end_to_end_releases_ledger_and_nacks(self):
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
        self.assertFalse(should_acknowledge_to_pubsub(outcome.decision))
        self.assertFalse(
            dedupe.contains(_observation_id("duck@example.com", "msg-retry"))
        )
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

    def test_ambiguous_outcome_acks_and_does_not_nack(self):
        outcome, _, _, _ = call(
            gate_a_message(),
            GATE_A_SNAPSHOT,
            adapter=FakeDispatchAdapter(
                results=[DispatchAttemptResult(success=False, error_kind="TIMEOUT")]
            ),
        )
        self.assertEqual(outcome.decision, A2Decision.DISPATCH_OUTCOME_UNKNOWN)
        self.assertTrue(should_acknowledge_to_pubsub(outcome.decision))

    def test_should_acknowledge_covers_every_decision_value(self):
        # Every decision must have an explicit, correct ack/nack answer;
        # only the confirmed-not-sent retryable case may nack.
        for decision in A2Decision:
            with self.subTest(decision=decision):
                expected_nack = decision is A2Decision.DISPATCH_FAILED_RETRYABLE
                self.assertEqual(
                    should_acknowledge_to_pubsub(decision), not expected_nack
                )

    def test_no_sleep_or_loop_in_module_source(self):
        source = (REPO_ROOT / "scripts" / "a2_dispatch.py").read_text(encoding="utf-8")
        self.assertNotIn("time.sleep", source)
        self.assertNotIn("import time", source)

    def test_single_failure_produces_exactly_one_dispatch_attempt(self):
        # Guards against accidental retry multiplication: a RETRYABLE
        # failure must not itself cause more than the one call this
        # process made; any further attempt must come from a distinct,
        # explicit reprocessing call (proven above), never from a loop
        # inside process_gmail_event.
        adapter = FakeDispatchAdapter(
            results=[DispatchAttemptResult(success=False, http_status=503)]
        )
        call(gate_a_message(), GATE_A_SNAPSHOT, adapter=adapter)
        self.assertEqual(len(adapter.calls), 1)


# ---------------------------------------------------------------------------
# Section 6: Concurrency / CAS review
# ---------------------------------------------------------------------------


class ConcurrencyReviewTests(unittest.TestCase):
    """InMemoryTransitionLedger is a single-process, GIL-serialized fake.
    These tests demonstrate the intended state-machine behavior under
    interleaved calls; they do NOT demonstrate multi-process atomicity,
    which only a real transactional backend can provide. See
    PRODUCTION_CAS_REQUIREMENT in InMemoryTransitionLedger's docstring."""

    def test_two_reservations_for_same_key_only_one_succeeds(self):
        ledger = InMemoryTransitionLedger()
        key = "shared-transition"
        first = ledger.reserve_if_absent(key)
        second = ledger.reserve_if_absent(key)
        self.assertTrue(first)
        self.assertFalse(second)

    def test_two_different_gmail_messages_same_transition_key_one_dispatch(self):
        # This is the same property as
        # test_same_transition_from_different_message_ids_dispatches_once,
        # phrased as a direct "two workers, same transition_key" check
        # against the ledger rather than through the full event pipeline.
        ledger = InMemoryTransitionLedger()
        dispatched = []
        for worker_message_id in ("worker-A", "worker-B"):
            if ledger.reserve_if_absent("same-transition"):
                dispatched.append(worker_message_id)
        self.assertEqual(dispatched, ["worker-A"])

    def test_in_memory_fake_does_not_claim_multi_process_atomicity(self):
        # This test documents the limitation as an executable fact: the
        # in-memory dict has no locking of its own, so this class's
        # correctness under real concurrent processes depends entirely on
        # CPython's GIL serializing the two dict operations inside
        # reserve_if_absent, which is an implementation accident of this
        # fake, not a portable guarantee, and does not exist at all across
        # separate Cloud Run instances (separate processes/machines).
        ledger = InMemoryTransitionLedger()
        self.assertIsInstance(ledger._state, dict)


# ---------------------------------------------------------------------------
# Fake dispatch / payload safety / A1 boundary (Phase A, re-verified)
# ---------------------------------------------------------------------------


class FakeDispatchTests(unittest.TestCase):
    def test_fake_dispatch_success_records_call_and_no_network_module_used(self):
        outcome, _, _, adapter = call(gate_a_message(), GATE_A_SNAPSHOT)
        self.assertEqual(outcome.decision, A2Decision.DISPATCHED)
        self.assertEqual(len(adapter.calls), 1)
        import scripts.a2_dispatch as mod

        for name in ("requests", "urllib", "http.client", "socket"):
            self.assertNotIn(name, mod.__dict__)


class PayloadSafetyTests(unittest.TestCase):
    def test_payload_never_contains_full_body_or_subject_text(self):
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

    def test_payload_and_adapter_never_carry_credentials(self):
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


class LegacyPollingAndA1BoundaryTests(unittest.TestCase):
    def test_legacy_polling_cron_schedules_are_unchanged(self):
        gate_a_yml = (
            REPO_ROOT / ".github" / "workflows" / "approval-check-phase2.yml"
        ).read_text(encoding="utf-8")
        design_yml = (
            REPO_ROOT / ".github" / "workflows" / "design-selection-check.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('cron: "11,26,41,56 * * * *"', gate_a_yml)
        self.assertIn('cron: "9,24,39,54 * * * *"', design_yml)

    def test_a2_module_never_imports_imaplib_or_smtplib(self):
        source = (REPO_ROOT / "scripts" / "a2_dispatch.py").read_text(encoding="utf-8")
        self.assertNotIn("imaplib", source)
        self.assertNotIn("smtplib", source)
        self.assertIsNone(re.search(r"^\s*import\s+requests", source, re.MULTILINE))

    def test_a2_module_lives_outside_the_protected_a1_directory(self):
        self.assertFalse(
            (REPO_ROOT / "cloud" / "approval_receiver" / "a2_dispatch.py").exists()
        )

    def test_same_event_cannot_cause_two_dispatches(self):
        dedupe, ledger = fresh_stores()
        adapter = FakeDispatchAdapter()
        message = gate_a_message(message_id="msg-once")
        for _ in range(3):
            call(message, GATE_A_SNAPSHOT, dedupe=dedupe, ledger=ledger, adapter=adapter)
        self.assertEqual(len(adapter.calls), 1)


if __name__ == "__main__":
    unittest.main()
