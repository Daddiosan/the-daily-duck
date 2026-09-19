import unittest

from scripts.approval_domain import (
    ApprovalSource,
    ApprovalStage,
    ApprovalValidationError,
    DispatchOutcome,
    MAX_DESIGN_BATCH_DIGITS,
    TransitionOutcome,
    build_approval_command,
    decide_downstream_dispatch,
    decide_transition,
    trusted_principal_from_github_context,
    trusted_principal_from_gmail_metadata,
)
from scripts.check_design_selection import extract_command
from scripts.check_story_approval import normalize_reply


ISSUE = "2026-09-19"
ALLOWED = {"owner@example.com", "github-owner"}


def gate(
    command="3",
    *,
    issue=ISSUE,
    source=ApprovalSource.EVENT,
    event_id="event-1",
    message_id=None,
    upstream_run_id="123456",
):
    return build_approval_command(
        stage=ApprovalStage.GATE_A,
        issue_date=issue,
        expected_issue_date=ISSUE,
        command=command,
        source_type=source,
        trusted_principal=(
            trusted_principal_from_github_context("github-owner")
            if source is ApprovalSource.EVENT
            else trusted_principal_from_gmail_metadata("owner@example.com")
        ),
        allowed_principals=ALLOWED,
        source_event_id=event_id if source is ApprovalSource.EVENT else None,
        message_id=message_id,
        upstream_run_id=upstream_run_id,
    )


def design(
    command="1 3",
    *,
    issue=ISSUE,
    source=ApprovalSource.EVENT,
    event_id="event-2",
    batch="4",
    expected_batch="4",
    message_id=None,
    upstream_run_id="123456",
):
    return build_approval_command(
        stage=ApprovalStage.DESIGN_SELECTION,
        issue_date=issue,
        expected_issue_date=ISSUE,
        command=command,
        source_type=source,
        trusted_principal=(
            trusted_principal_from_github_context("github-owner")
            if source is ApprovalSource.EVENT
            else trusted_principal_from_gmail_metadata("owner@example.com")
        ),
        allowed_principals=ALLOWED,
        source_event_id=event_id if source is ApprovalSource.EVENT else None,
        message_id=message_id,
        upstream_run_id=upstream_run_id,
        design_batch_id=batch,
        expected_design_batch_id=expected_batch,
    )


class ApprovalNormalizationAndValidationTests(unittest.TestCase):
    def test_normal_gate_a_approval(self):
        command = gate("3")
        self.assertEqual(command.command, "SELECT_STORY:3")
        decision = decide_transition(
            command,
            current_state="WAITING_STORY_SELECTION",
            current_issue_date=ISSUE,
        )
        self.assertEqual(decision.outcome, TransitionOutcome.APPLY)
        self.assertEqual(decision.next_state, "APPROVED_STORY")

    def test_gate_a_matches_legacy_strict_normalization(self):
        self.assertEqual(normalize_reply(" 3\r\n"), "3")
        self.assertEqual(gate(normalize_reply(" 3\r\n")).command, "SELECT_STORY:3")

    def test_invalid_gate_a_command(self):
        for value in ("0", "6", "3 OK", "３", "3\n4", 3):
            with self.subTest(value=value), self.assertRaises(
                ApprovalValidationError
            ):
                gate(value)

    def test_wrong_issue_date_is_rejected_during_validation(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            gate(issue="2026-09-18")
        self.assertEqual(caught.exception.reason, "STALE_ISSUE")

    def test_unauthorized_principal_is_rejected(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            build_approval_command(
                stage=ApprovalStage.GATE_A,
                issue_date=ISSUE,
                command="3",
                source_type=ApprovalSource.EVENT,
                trusted_principal=trusted_principal_from_github_context("attacker"),
                allowed_principals=ALLOWED,
                source_event_id="event-1",
            )
        self.assertEqual(caught.exception.reason, "UNAUTHORIZED_TRUSTED_PRINCIPAL")

    def test_normal_design_selection(self):
        command = design("1 3")
        self.assertEqual(command.command, "SELECT_DESIGN:1:3")
        decision = decide_transition(
            command,
            current_state="WAITING_FINAL_SELECTION",
            current_issue_date=ISSUE,
            current_design_batch_id="4",
        )
        self.assertEqual(decision.outcome, TransitionOutcome.APPLY)
        self.assertEqual(decision.dispatch_target, "website-publish.yml")

    def test_design_normalization_matches_legacy_semantics(self):
        legacy = extract_command("１　３")
        self.assertIsNotNone(legacy)
        self.assertEqual(legacy[:3], ("FINAL", 1, 3))
        self.assertEqual(design("１　３").command, "SELECT_DESIGN:1:3")

    def test_next_three_normalizes_case_and_width(self):
        self.assertEqual(design("next   3").command, "NEXT_3")
        self.assertEqual(design("ＮＥＸＴ　３").command, "NEXT_3")

    def test_invalid_design_selection(self):
        for value in ("0 1", "1 4", "1", "NEXT", "1 2 OK", (1, 2)):
            with self.subTest(value=value), self.assertRaises(
                ApprovalValidationError
            ):
                design(value)

    def test_stale_design_batch_is_rejected(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            design(batch="3")
        self.assertEqual(caught.exception.reason, "STALE_DESIGN_BATCH")

    def test_design_selection_requires_batch(self):
        for value in (None, "", 0, "batch-4"):
            with self.subTest(value=value), self.assertRaises(
                ApprovalValidationError
            ) as caught:
                design(batch=value, expected_batch=None)
            expected_reason = (
                "MISSING_DESIGN_BATCH_ID"
                if value is None or value == ""
                else "INVALID_DESIGN_BATCH_ID"
            )
            self.assertEqual(caught.exception.reason, expected_reason)

    def test_superscript_batch_is_controlled_rejection(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            design(batch="²", expected_batch=None)
        self.assertEqual(caught.exception.reason, "INVALID_DESIGN_BATCH_ID")

    def test_unicode_decimal_batches_remain_canonical(self):
        full_width = design(batch="１", expected_batch="１")
        arabic_indic = design(batch="٤", expected_batch="٤")
        self.assertEqual(full_width.design_batch_id, "1")
        self.assertEqual(arabic_indic.design_batch_id, "4")

    def test_normal_small_design_batch_remains_canonical(self):
        command = design(batch="00042", expected_batch=None)
        self.assertEqual(command.design_batch_id, "42")

    def test_design_batch_digit_limit_boundary_is_accepted(self):
        boundary = "9" * MAX_DESIGN_BATCH_DIGITS
        command = design(batch=boundary, expected_batch=None)
        self.assertEqual(command.design_batch_id, boundary)

    def test_design_batch_digit_limit_plus_one_is_controlled_rejection(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            design(batch="9" * (MAX_DESIGN_BATCH_DIGITS + 1), expected_batch=None)
        self.assertEqual(caught.exception.reason, "INVALID_DESIGN_BATCH_ID")

    def test_4300_plus_digit_batch_is_controlled_rejection(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            design(batch="9" * 4301, expected_batch=None)
        self.assertEqual(caught.exception.reason, "INVALID_DESIGN_BATCH_ID")

    def test_huge_python_integer_batch_is_controlled_rejection(self):
        huge_integer = 10 ** (MAX_DESIGN_BATCH_DIGITS + 100)
        with self.assertRaises(ApprovalValidationError) as caught:
            design(batch=huge_integer, expected_batch=None)
        self.assertEqual(caught.exception.reason, "INVALID_DESIGN_BATCH_ID")

    def test_next_three_requires_batch(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            design("NEXT 3", batch=None, expected_batch=None)
        self.assertEqual(caught.exception.reason, "MISSING_DESIGN_BATCH_ID")

    def test_gate_a_rejects_unexpected_design_batch(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            build_approval_command(
                stage=ApprovalStage.GATE_A,
                issue_date=ISSUE,
                command="3",
                source_type=ApprovalSource.EVENT,
                trusted_principal=trusted_principal_from_github_context(
                    "github-owner"
                ),
                allowed_principals=ALLOWED,
                source_event_id="event-with-batch",
                design_batch_id="4",
            )
        self.assertEqual(caught.exception.reason, "UNEXPECTED_DESIGN_BATCH")

    def test_event_requires_well_formed_source_event_id(self):
        for event_id in (None, "", "bad id", "x" * 129):
            with self.subTest(event_id=event_id), self.assertRaises(
                ApprovalValidationError
            ):
                gate(event_id=event_id)

    def test_legacy_gmail_poll_without_optional_metadata_is_explicit(self):
        command = build_approval_command(
            stage=ApprovalStage.GATE_A,
            issue_date=ISSUE,
            command="3",
            source_type=ApprovalSource.GMAIL_POLL,
            trusted_principal=trusted_principal_from_gmail_metadata(
                "owner@example.com"
            ),
            allowed_principals=ALLOWED,
        )
        self.assertTrue(command.legacy_metadata)
        self.assertEqual(command.source_identity, "trusted-principal:owner@example.com")


class IdempotencyAndTransitionTests(unittest.TestCase):
    def test_same_event_twice_has_same_idempotency_key(self):
        first = gate()
        duplicate = gate()
        self.assertEqual(first.idempotency_key, duplicate.idempotency_key)
        decision = decide_transition(
            duplicate,
            current_state="WAITING_STORY_SELECTION",
            current_issue_date=ISSUE,
            applied_idempotency_keys={first.idempotency_key},
        )
        self.assertEqual(decision.outcome, TransitionOutcome.NO_OP_ALREADY_APPLIED)

    def test_same_semantic_normalization_has_same_keys(self):
        left = design("１　３")
        right = design("1 3")
        self.assertEqual(left.command, right.command)
        self.assertEqual(left.idempotency_key, right.idempotency_key)
        self.assertEqual(left.transition_key, right.transition_key)

    def test_different_issue_has_different_key(self):
        other = build_approval_command(
            stage=ApprovalStage.GATE_A,
            issue_date="2026-09-20",
            command="3",
            source_type=ApprovalSource.EVENT,
            trusted_principal=trusted_principal_from_github_context("github-owner"),
            allowed_principals=ALLOWED,
            source_event_id="event-1",
            upstream_run_id="123456",
        )
        self.assertNotEqual(gate().idempotency_key, other.idempotency_key)
        self.assertNotEqual(gate().transition_key, other.transition_key)

    def test_different_stage_has_different_key(self):
        self.assertNotEqual(gate("3").idempotency_key, design("1 3").idempotency_key)
        self.assertNotEqual(gate("3").transition_key, design("1 3").transition_key)

    def test_different_command_has_different_transition_key(self):
        self.assertNotEqual(gate("2").transition_key, gate("3").transition_key)

    def test_different_design_batch_has_different_key(self):
        other = build_approval_command(
            stage=ApprovalStage.DESIGN_SELECTION,
            issue_date=ISSUE,
            command="1 3",
            source_type=ApprovalSource.EVENT,
            trusted_principal=trusted_principal_from_github_context("github-owner"),
            allowed_principals=ALLOWED,
            source_event_id="event-2",
            upstream_run_id="123456",
            design_batch_id="5",
        )
        self.assertNotEqual(design().idempotency_key, other.idempotency_key)
        self.assertNotEqual(design().transition_key, other.transition_key)

    def test_different_upstream_run_has_same_transition_key(self):
        first = gate(upstream_run_id="run-100")
        second = gate(upstream_run_id="run-200")
        self.assertNotEqual(first.idempotency_key, second.idempotency_key)
        self.assertEqual(first.transition_key, second.transition_key)

    def test_legacy_poll_and_event_with_run_share_transition_key(self):
        poll = gate(
            source=ApprovalSource.GMAIL_POLL,
            event_id=None,
            upstream_run_id=None,
        )
        event = gate(event_id="event-with-run", upstream_run_id="run-300")
        self.assertTrue(poll.legacy_metadata)
        self.assertNotEqual(poll.idempotency_key, event.idempotency_key)
        self.assertEqual(poll.transition_key, event.transition_key)

    def test_different_message_ids_only_change_delivery_key(self):
        first = gate(
            source=ApprovalSource.GMAIL_POLL,
            event_id=None,
            message_id="<message-1@example.com>",
            upstream_run_id=None,
        )
        second = gate(
            source=ApprovalSource.GMAIL_POLL,
            event_id=None,
            message_id="<message-2@example.com>",
            upstream_run_id=None,
        )
        self.assertNotEqual(first.idempotency_key, second.idempotency_key)
        self.assertEqual(first.transition_key, second.transition_key)

    def test_duplicate_gate_a_approval_is_no_op(self):
        command = gate()
        decision = decide_transition(
            command,
            current_state="WAITING_STORY_SELECTION",
            current_issue_date=ISSUE,
            applied_transition_keys={command.transition_key},
        )
        self.assertEqual(decision.outcome, TransitionOutcome.NO_OP_ALREADY_APPLIED)

    def test_stale_gate_a_approval_is_rejected(self):
        decision = decide_transition(
            gate(),
            current_state="WAITING_STORY_SELECTION",
            current_issue_date="2026-09-20",
        )
        self.assertEqual(decision.outcome, TransitionOutcome.REJECT_STALE)

    def test_duplicate_design_selection_is_no_op(self):
        command = design()
        decision = decide_transition(
            command,
            current_state="WAITING_FINAL_SELECTION",
            current_issue_date=ISSUE,
            current_design_batch_id="4",
            applied_idempotency_keys={command.idempotency_key},
        )
        self.assertEqual(decision.outcome, TransitionOutcome.NO_OP_ALREADY_APPLIED)

    def test_next_three_batch_rotation_and_duplicates(self):
        batch_a = design("NEXT 3", batch="4", expected_batch="4")
        first_a = decide_transition(
            batch_a,
            current_state="WAITING_FINAL_SELECTION",
            current_issue_date=ISSUE,
            current_design_batch_id="4",
        )
        duplicate_a = decide_transition(
            batch_a,
            current_state="WAITING_FINAL_SELECTION",
            current_issue_date=ISSUE,
            current_design_batch_id="5",
            applied_transition_keys={batch_a.transition_key},
        )
        batch_b = design("NEXT 3", batch="5", expected_batch="5")
        first_b = decide_transition(
            batch_b,
            current_state="WAITING_FINAL_SELECTION",
            current_issue_date=ISSUE,
            current_design_batch_id="5",
            applied_transition_keys={batch_a.transition_key},
        )
        duplicate_b = decide_transition(
            batch_b,
            current_state="WAITING_FINAL_SELECTION",
            current_issue_date=ISSUE,
            current_design_batch_id="6",
            applied_transition_keys={batch_a.transition_key, batch_b.transition_key},
        )
        self.assertEqual(first_a.outcome, TransitionOutcome.APPLY)
        self.assertEqual(first_a.next_state, "DESIGN_OPTIONS_READY")
        self.assertEqual(duplicate_a.outcome, TransitionOutcome.NO_OP_ALREADY_APPLIED)
        self.assertNotEqual(batch_a.transition_key, batch_b.transition_key)
        self.assertEqual(first_b.outcome, TransitionOutcome.APPLY)
        self.assertEqual(duplicate_b.outcome, TransitionOutcome.NO_OP_ALREADY_APPLIED)

    def test_same_design_batch_different_transport_metadata_same_transition(self):
        event = design(
            "NEXT 3",
            batch="4",
            event_id="event-batch-4",
            upstream_run_id="run-event",
        )
        poll = design(
            "NEXT 3",
            batch="04",
            source=ApprovalSource.GMAIL_POLL,
            event_id=None,
            message_id="<batch-4@example.com>",
            upstream_run_id=None,
        )
        self.assertEqual(event.design_batch_id, "4")
        self.assertEqual(poll.design_batch_id, "4")
        self.assertNotEqual(event.idempotency_key, poll.idempotency_key)
        self.assertEqual(event.transition_key, poll.transition_key)

    def test_same_final_selection_same_batch_is_no_op(self):
        first = design("1 2", batch="4")
        duplicate = design("１　２", batch=4, event_id="event-duplicate")
        decision = decide_transition(
            duplicate,
            current_state="WAITING_FINAL_SELECTION",
            current_issue_date=ISSUE,
            current_design_batch_id=4,
            applied_transition_keys={first.transition_key},
        )
        self.assertEqual(first.transition_key, duplicate.transition_key)
        self.assertEqual(decision.outcome, TransitionOutcome.NO_OP_ALREADY_APPLIED)

    def test_same_final_selection_different_batch_is_distinct(self):
        batch_a = design("1 2", batch="4", expected_batch="4")
        batch_b = design("1 2", batch="5", expected_batch="5")
        self.assertNotEqual(batch_a.transition_key, batch_b.transition_key)

    def test_final_selection_from_old_batch_is_stale(self):
        old = design("1 2", batch="4", expected_batch="4")
        decision = decide_transition(
            old,
            current_state="WAITING_FINAL_SELECTION",
            current_issue_date=ISSUE,
            current_design_batch_id="5",
        )
        self.assertEqual(decision.outcome, TransitionOutcome.REJECT_STALE)

    def test_malformed_current_batch_returns_reject_invalid(self):
        decision = decide_transition(
            design("1 2"),
            current_state="WAITING_FINAL_SELECTION",
            current_issue_date=ISSUE,
            current_design_batch_id="²",
        )
        self.assertEqual(decision.outcome, TransitionOutcome.REJECT_INVALID)

    def test_overlimit_current_batch_returns_reject_invalid(self):
        decision = decide_transition(
            design("1 2"),
            current_state="WAITING_FINAL_SELECTION",
            current_issue_date=ISSUE,
            current_design_batch_id="9" * (MAX_DESIGN_BATCH_DIGITS + 1),
        )
        self.assertEqual(decision.outcome, TransitionOutcome.REJECT_INVALID)
        self.assertEqual(decision.reason, "INVALID_CURRENT_DESIGN_BATCH_ID")

    def test_stale_design_selection_is_rejected(self):
        decision = decide_transition(
            design(),
            current_state="WAITING_FINAL_SELECTION",
            current_issue_date="2026-09-20",
            current_design_batch_id="4",
        )
        self.assertEqual(decision.outcome, TransitionOutcome.REJECT_STALE)

    def test_event_first_then_poll_with_different_metadata_applies_once(self):
        event = design(
            event_id="event-77",
            upstream_run_id="run-event-77",
        )
        poll = design(
            source=ApprovalSource.GMAIL_POLL,
            event_id=None,
            message_id="<mail-poll-77@example.com>",
            upstream_run_id=None,
        )
        first = decide_transition(
            event,
            current_state="WAITING_FINAL_SELECTION",
            current_issue_date=ISSUE,
            current_design_batch_id="4",
        )
        self.assertEqual(first.outcome, TransitionOutcome.APPLY)
        self.assertNotEqual(event.idempotency_key, poll.idempotency_key)
        self.assertEqual(event.transition_key, poll.transition_key)
        second = decide_transition(
            poll,
            current_state="WAITING_FINAL_SELECTION",
            current_issue_date=ISSUE,
            current_design_batch_id="4",
            applied_transition_keys={event.transition_key},
        )
        self.assertEqual(second.outcome, TransitionOutcome.NO_OP_ALREADY_APPLIED)

    def test_poll_first_then_event_with_different_metadata_applies_once(self):
        poll = gate(
            source=ApprovalSource.GMAIL_POLL,
            event_id=None,
            upstream_run_id=None,
        )
        event = gate(event_id="event-88", upstream_run_id="run-event-88")
        first = decide_transition(
            poll,
            current_state="WAITING_STORY_SELECTION",
            current_issue_date=ISSUE,
        )
        self.assertEqual(first.outcome, TransitionOutcome.APPLY)
        self.assertNotEqual(poll.idempotency_key, event.idempotency_key)
        self.assertEqual(poll.transition_key, event.transition_key)
        second = decide_transition(
            event,
            current_state="WAITING_STORY_SELECTION",
            current_issue_date=ISSUE,
            applied_transition_keys={poll.transition_key},
        )
        self.assertEqual(second.outcome, TransitionOutcome.NO_OP_ALREADY_APPLIED)

    def test_different_event_ids_same_semantic_approval_apply_once(self):
        first = gate(event_id="event-a")
        second = gate(event_id="event-b")
        self.assertNotEqual(first.idempotency_key, second.idempotency_key)
        self.assertEqual(first.transition_key, second.transition_key)
        decision = decide_transition(
            second,
            current_state="WAITING_STORY_SELECTION",
            current_issue_date=ISSUE,
            applied_transition_keys={first.transition_key},
        )
        self.assertEqual(decision.outcome, TransitionOutcome.NO_OP_ALREADY_APPLIED)

    def test_different_upstream_run_is_no_op_when_transition_is_recorded(self):
        first = gate(event_id="event-run-a", upstream_run_id="run-a")
        retry = gate(event_id="event-run-b", upstream_run_id="run-b")
        decision = decide_transition(
            retry,
            current_state="WAITING_STORY_SELECTION",
            current_issue_date=ISSUE,
            applied_transition_keys={first.transition_key},
        )
        self.assertEqual(first.transition_key, retry.transition_key)
        self.assertEqual(decision.outcome, TransitionOutcome.NO_OP_ALREADY_APPLIED)

    def test_reconciliation_recovers_missed_event(self):
        command = build_approval_command(
            stage=ApprovalStage.GATE_A,
            issue_date=ISSUE,
            command="2",
            source_type=ApprovalSource.RECONCILIATION,
            trusted_principal=trusted_principal_from_gmail_metadata(
                "owner@example.com"
            ),
            allowed_principals=ALLOWED,
            message_id="<missed@example.com>",
        )
        decision = decide_transition(
            command,
            current_state="WAITING_STORY_SELECTION",
            current_issue_date=ISSUE,
        )
        self.assertEqual(decision.outcome, TransitionOutcome.APPLY)

    def test_state_already_advanced_never_rolls_back(self):
        decision = decide_transition(
            design("2 2"),
            current_state="PUBLISHED",
            current_issue_date=ISSUE,
            current_command="SELECT_DESIGN:1:3",
            current_design_batch_id="4",
        )
        self.assertEqual(decision.outcome, TransitionOutcome.REJECT_CONFLICT)
        self.assertIsNone(decision.next_state)

    def test_duplicate_workflow_invocation_is_safe_no_op(self):
        decision = decide_transition(
            gate(),
            current_state="APPROVED_STORY",
            current_issue_date=ISSUE,
            current_command="SELECT_STORY:3",
        )
        self.assertEqual(decision.outcome, TransitionOutcome.NO_OP_ALREADY_APPLIED)

    def test_committed_state_can_recover_failed_dispatch(self):
        retry = decide_downstream_dispatch(
            ApprovalStage.GATE_A,
            current_state="APPROVED_STORY",
            downstream_completed=False,
        )
        completed = decide_downstream_dispatch(
            ApprovalStage.GATE_A,
            current_state="APPROVED_STORY",
            downstream_completed=True,
        )
        self.assertEqual(retry.outcome, DispatchOutcome.DISPATCH)
        self.assertEqual(retry.target, "design-options.yml")
        self.assertEqual(completed.outcome, DispatchOutcome.NO_OP_ALREADY_COMPLETED)


if __name__ == "__main__":
    unittest.main()
