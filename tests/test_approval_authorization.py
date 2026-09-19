import unittest

from scripts.approval_domain import (
    ApprovalSource,
    ApprovalStage,
    ApprovalValidationError,
    TrustedPrincipalContext,
    TrustedPrincipalSource,
    authorize_principal,
    build_approval_command,
    trusted_principal_from_github_context,
    trusted_principal_from_gmail_metadata,
)


ALLOWED = {"github-owner", "second-owner", "owner@example.com"}


class PrincipalAuthorizationTests(unittest.TestCase):
    def test_trusted_allowed_actor_without_payload_claim_is_authorized(self):
        result = authorize_principal(
            trusted_context=trusted_principal_from_github_context("github-owner"),
            allowed_principals=ALLOWED,
        )
        self.assertEqual(result.principal, "github-owner")
        self.assertIsNone(result.claimed_principal)

    def test_disallowed_actor_cannot_spoof_allowed_payload_claim(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            authorize_principal(
                trusted_context=trusted_principal_from_github_context("attacker"),
                claimed_principal="github-owner",
                allowed_principals=ALLOWED,
            )
        self.assertEqual(caught.exception.reason, "PRINCIPAL_MISMATCH")

    def test_payload_claim_cannot_replace_missing_trusted_context(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            authorize_principal(
                trusted_context=None,
                claimed_principal="github-owner",
                allowed_principals=ALLOWED,
            )
        self.assertEqual(caught.exception.reason, "MISSING_TRUSTED_PRINCIPAL")

    def test_matching_payload_claim_is_audited_but_not_required(self):
        result = authorize_principal(
            trusted_context=trusted_principal_from_github_context("github-owner"),
            claimed_principal="github-owner",
            allowed_principals=ALLOWED,
        )
        self.assertEqual(result.principal, "github-owner")
        self.assertEqual(result.claimed_principal, "github-owner")

    def test_different_allowed_payload_claim_is_rejected(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            authorize_principal(
                trusted_context=trusted_principal_from_github_context("github-owner"),
                claimed_principal="second-owner",
                allowed_principals=ALLOWED,
            )
        self.assertEqual(caught.exception.reason, "PRINCIPAL_MISMATCH")

    def test_disallowed_payload_claim_mismatch_is_rejected(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            authorize_principal(
                trusted_context=trusted_principal_from_github_context("github-owner"),
                claimed_principal="attacker",
                allowed_principals=ALLOWED,
            )
        self.assertEqual(caught.exception.reason, "PRINCIPAL_MISMATCH")

    def test_case_normalization_is_explicit(self):
        result = authorize_principal(
            trusted_context=trusted_principal_from_github_context("GitHub-Owner"),
            claimed_principal="GITHUB-OWNER",
            allowed_principals={"github-owner"},
        )
        self.assertEqual(result.principal, "github-owner")

    def test_surrounding_whitespace_is_normalized(self):
        result = authorize_principal(
            trusted_context=trusted_principal_from_gmail_metadata(
                "  OWNER@EXAMPLE.COM  "
            ),
            claimed_principal=" owner@example.com ",
            allowed_principals={" owner@example.com "},
        )
        self.assertEqual(result.principal, "owner@example.com")

    def test_empty_allowlist_fails_closed(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            authorize_principal(
                trusted_context=trusted_principal_from_github_context("github-owner"),
                allowed_principals=set(),
            )
        self.assertEqual(caught.exception.reason, "EMPTY_PRINCIPAL_ALLOWLIST")

    def test_empty_trusted_principal_is_rejected(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            authorize_principal(
                trusted_context=trusted_principal_from_github_context("   "),
                allowed_principals=ALLOWED,
            )
        self.assertEqual(caught.exception.reason, "MISSING_TRUSTED_PRINCIPAL")

    def test_malformed_trusted_principal_is_controlled_rejection(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            authorize_principal(
                trusted_context=trusted_principal_from_github_context(
                    "github owner<script>"
                ),
                allowed_principals=ALLOWED,
            )
        self.assertEqual(caught.exception.reason, "MALFORMED_TRUSTED_PRINCIPAL")

    def test_gmail_metadata_uses_same_authorization_primitive(self):
        result = authorize_principal(
            trusted_context=trusted_principal_from_gmail_metadata(
                "owner@example.com"
            ),
            allowed_principals=ALLOWED,
        )
        self.assertEqual(result.principal, "owner@example.com")
        self.assertEqual(
            result.source, TrustedPrincipalSource.GMAIL_MESSAGE_METADATA
        )

    def test_github_context_uses_same_authorization_primitive(self):
        result = authorize_principal(
            trusted_context=trusted_principal_from_github_context("github-owner"),
            allowed_principals=ALLOWED,
        )
        self.assertEqual(result.principal, "github-owner")
        self.assertEqual(
            result.source, TrustedPrincipalSource.GITHUB_WORKFLOW_CONTEXT
        )

    def test_plain_payload_string_is_not_a_trusted_context(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            authorize_principal(
                trusted_context="github-owner",
                allowed_principals=ALLOWED,
            )
        self.assertEqual(
            caught.exception.reason, "INVALID_TRUSTED_PRINCIPAL_CONTEXT"
        )

    def test_build_command_uses_trusted_identity_not_payload_claim(self):
        command = build_approval_command(
            stage=ApprovalStage.GATE_A,
            issue_date="2026-09-19",
            command="3",
            source_type=ApprovalSource.EVENT,
            trusted_principal=trusted_principal_from_github_context("github-owner"),
            claimed_principal=None,
            allowed_principals=ALLOWED,
            source_event_id="event-1",
        )
        self.assertEqual(command.authorized_principal, "github-owner")
        self.assertEqual(
            command.principal_source,
            TrustedPrincipalSource.GITHUB_WORKFLOW_CONTEXT,
        )

    def test_build_command_blocks_payload_self_authorization(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            build_approval_command(
                stage=ApprovalStage.GATE_A,
                issue_date="2026-09-19",
                command="3",
                source_type=ApprovalSource.EVENT,
                trusted_principal=None,
                claimed_principal="github-owner",
                allowed_principals=ALLOWED,
                source_event_id="event-1",
            )
        self.assertEqual(caught.exception.reason, "MISSING_TRUSTED_PRINCIPAL")

    def test_event_rejects_gmail_principal_provenance(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            build_approval_command(
                stage=ApprovalStage.GATE_A,
                issue_date="2026-09-19",
                command="3",
                source_type=ApprovalSource.EVENT,
                trusted_principal=trusted_principal_from_gmail_metadata(
                    "owner@example.com"
                ),
                allowed_principals=ALLOWED,
                source_event_id="event-1",
            )
        self.assertEqual(
            caught.exception.reason, "TRUSTED_PRINCIPAL_SOURCE_MISMATCH"
        )

    def test_gmail_poll_rejects_github_principal_provenance(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            build_approval_command(
                stage=ApprovalStage.GATE_A,
                issue_date="2026-09-19",
                command="3",
                source_type=ApprovalSource.GMAIL_POLL,
                trusted_principal=trusted_principal_from_github_context(
                    "github-owner"
                ),
                allowed_principals=ALLOWED,
            )
        self.assertEqual(
            caught.exception.reason, "TRUSTED_PRINCIPAL_SOURCE_MISMATCH"
        )

    def test_reconciliation_accepts_gmail_metadata_provenance(self):
        command = build_approval_command(
            stage=ApprovalStage.GATE_A,
            issue_date="2026-09-19",
            command="3",
            source_type=ApprovalSource.RECONCILIATION,
            trusted_principal=trusted_principal_from_gmail_metadata(
                "owner@example.com"
            ),
            allowed_principals=ALLOWED,
            message_id="<reconciled@example.com>",
        )
        self.assertEqual(
            command.principal_source,
            TrustedPrincipalSource.GMAIL_MESSAGE_METADATA,
        )

    def test_reconciliation_accepts_verified_record_provenance(self):
        command = build_approval_command(
            stage=ApprovalStage.GATE_A,
            issue_date="2026-09-19",
            command="3",
            source_type=ApprovalSource.RECONCILIATION,
            trusted_principal=TrustedPrincipalContext(
                "github-owner",
                TrustedPrincipalSource.VERIFIED_RECONCILIATION_RECORD,
            ),
            allowed_principals=ALLOWED,
            source_event_id="verified-record-1",
        )
        self.assertEqual(
            command.principal_source,
            TrustedPrincipalSource.VERIFIED_RECONCILIATION_RECORD,
        )

    def test_reconciliation_rejects_github_context_provenance(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            build_approval_command(
                stage=ApprovalStage.GATE_A,
                issue_date="2026-09-19",
                command="3",
                source_type=ApprovalSource.RECONCILIATION,
                trusted_principal=trusted_principal_from_github_context(
                    "github-owner"
                ),
                allowed_principals=ALLOWED,
                source_event_id="event-1",
            )
        self.assertEqual(
            caught.exception.reason, "TRUSTED_PRINCIPAL_SOURCE_MISMATCH"
        )

    def test_duck_typed_fake_trusted_context_is_rejected(self):
        class FakeTrustedContext:
            principal = "github-owner"
            source = TrustedPrincipalSource.GITHUB_WORKFLOW_CONTEXT

        with self.assertRaises(ApprovalValidationError) as caught:
            authorize_principal(
                trusted_context=FakeTrustedContext(),
                allowed_principals=ALLOWED,
            )
        self.assertEqual(
            caught.exception.reason, "INVALID_TRUSTED_PRINCIPAL_CONTEXT"
        )

    def test_dict_fake_trusted_context_is_rejected(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            authorize_principal(
                trusted_context={
                    "principal": "github-owner",
                    "source": "GITHUB_WORKFLOW_CONTEXT",
                },
                allowed_principals=ALLOWED,
            )
        self.assertEqual(
            caught.exception.reason, "INVALID_TRUSTED_PRINCIPAL_CONTEXT"
        )

    def test_disallowed_trusted_actor_without_claim_is_rejected(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            authorize_principal(
                trusted_context=trusted_principal_from_github_context("attacker"),
                allowed_principals=ALLOWED,
            )
        self.assertEqual(
            caught.exception.reason, "UNAUTHORIZED_TRUSTED_PRINCIPAL"
        )

    def test_none_allowlist_is_controlled_rejection(self):
        with self.assertRaises(ApprovalValidationError) as caught:
            authorize_principal(
                trusted_context=trusted_principal_from_github_context(
                    "github-owner"
                ),
                allowed_principals=None,
            )
        self.assertEqual(
            caught.exception.reason, "MALFORMED_PRINCIPAL_ALLOWLIST"
        )

    def test_unicode_casefold_collisions_are_rejected_before_folding(self):
        for raw_principal, ascii_collision in (
            ("straße", "strasse"),
            ("Kate", "kate"),
            ("Ｇithub-owner", "github-owner"),
        ):
            with self.subTest(raw_principal=raw_principal), self.assertRaises(
                ApprovalValidationError
            ) as caught:
                authorize_principal(
                    trusted_context=trusted_principal_from_github_context(
                        raw_principal
                    ),
                    allowed_principals={ascii_collision},
                )
            self.assertEqual(
                caught.exception.reason, "MALFORMED_TRUSTED_PRINCIPAL"
            )


if __name__ == "__main__":
    unittest.main()
