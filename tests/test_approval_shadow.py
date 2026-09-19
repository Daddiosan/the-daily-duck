import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.approval_shadow import (
    SAFETY_FIELDS,
    ShadowInputError,
    create_observation,
    parse_allowlist,
    production_snapshot,
    render_summary,
    write_outputs,
)
from scripts.approval_domain import ApprovalStage


ISSUE = "2026-09-19"
NOW = datetime(2026, 9, 19, 1, 2, 3, tzinfo=timezone.utc)


class ShadowFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "automation_state"
        self.state.mkdir()
        self.gate = self.root / "gate_a_package.json"
        self.write(self.gate, {"issue_date": ISSUE, "state": "WAITING_STORY_SELECTION"})

    def close(self):
        self.temp.cleanup()

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def design(self, *, batch=4, state="WAITING_FINAL_SELECTION", **extra):
        value = {
            "issue_date": ISSUE,
            "state": state,
            "preview_batch_number": batch,
            **extra,
        }
        self.write(self.state / "design_options.json", value)

    def env(self, **changes):
        value = {
            "GITHUB_RUN_ID": "123456",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_ACTOR": "github-owner",
            "GITHUB_SHA": "a" * 40,
            "SHADOW_STAGE": "GATE_A",
            "SHADOW_ISSUE_DATE": ISSUE,
            "SHADOW_COMMAND": "3",
            "SHADOW_DESIGN_BATCH_ID": "",
            "SHADOW_UPSTREAM_RUN_ID": "daily-100",
            "SHADOW_CLAIMED_PRINCIPAL": "",
            "SHADOW_OBSERVED_UPSTREAM_RUN_ID": "daily-100",
            "APPROVAL_SHADOW_ACTORS": "github-owner,second-owner",
        }
        value.update(changes)
        return value


class ApprovalShadowTests(unittest.TestCase):
    def setUp(self):
        self.fx = ShadowFixture()

    def tearDown(self):
        self.fx.close()

    def observe(self, env=None, gate=True):
        return create_observation(
            env or self.fx.env(),
            repo_root=self.fx.root,
            gate_package_path=self.fx.gate if gate else None,
            now=NOW,
        )

    def test_01_allowlist_is_comma_separated_and_deduplicated(self):
        self.assertEqual(parse_allowlist(" Alice, bob,Alice "), ("Alice", "bob"))

    def test_02_empty_allowlist_fails_closed(self):
        with self.assertRaises(ShadowInputError) as caught:
            parse_allowlist("  ")
        self.assertEqual(caught.exception.reason, "EMPTY_SHADOW_ALLOWLIST")

    def test_03_empty_allowlist_entry_is_rejected(self):
        with self.assertRaises(ShadowInputError) as caught:
            parse_allowlist("alice,,bob")
        self.assertEqual(caught.exception.reason, "MALFORMED_SHADOW_ALLOWLIST")

    def test_04_valid_gate_a_event_is_observed(self):
        result, valid = self.observe()
        self.assertTrue(valid)
        self.assertEqual(result["canonical_command"], "SELECT_STORY:3")
        self.assertEqual(result["decision"], "APPLY")
        self.assertEqual(result["would_dispatch"], "design-options.yml")

    def test_05_valid_design_event_is_observed(self):
        self.fx.design()
        env = self.fx.env(
            SHADOW_STAGE="DESIGN_SELECTION",
            SHADOW_COMMAND="1 3",
            SHADOW_DESIGN_BATCH_ID="4",
        )
        result, valid = self.observe(env, gate=False)
        self.assertTrue(valid)
        self.assertEqual(result["canonical_command"], "SELECT_DESIGN:1:3")
        self.assertEqual(result["would_dispatch"], "website-publish.yml")

    def test_06_next_three_is_data_only(self):
        self.fx.design()
        env = self.fx.env(
            SHADOW_STAGE="DESIGN_SELECTION",
            SHADOW_COMMAND="NEXT 3",
            SHADOW_DESIGN_BATCH_ID="4",
        )
        result, valid = self.observe(env, gate=False)
        self.assertTrue(valid)
        self.assertEqual(result["canonical_command"], "NEXT_3")
        self.assertEqual(result["proposed_next_state"], "DESIGN_OPTIONS_READY")
        self.assertIsNone(result["would_dispatch"])

    def test_07_malformed_stage_is_controlled(self):
        result, valid = self.observe(self.fx.env(SHADOW_STAGE="PUBLISH"))
        self.assertFalse(valid)
        self.assertEqual(result["reason"], "INVALID_STAGE")

    def test_08_malformed_date_is_controlled(self):
        result, valid = self.observe(self.fx.env(SHADOW_ISSUE_DATE="09/19/2026"))
        self.assertFalse(valid)
        self.assertEqual(result["reason"], "INVALID_ISSUE_DATE")

    def test_09_malformed_gate_command_is_controlled(self):
        result, valid = self.observe(self.fx.env(SHADOW_COMMAND="3 OK"))
        self.assertFalse(valid)
        self.assertEqual(result["reason"], "INVALID_GATE_A_COMMAND")

    def test_10_unauthorized_actor_is_rejected(self):
        result, valid = self.observe(self.fx.env(GITHUB_ACTOR="attacker"))
        self.assertFalse(valid)
        self.assertEqual(result["reason"], "UNAUTHORIZED_TRUSTED_PRINCIPAL")
        self.assertEqual(result["authorization_outcome"], "REJECTED")

    def test_11_spoofed_claimed_principal_is_rejected(self):
        result, valid = self.observe(
            self.fx.env(SHADOW_CLAIMED_PRINCIPAL="second-owner")
        )
        self.assertFalse(valid)
        self.assertEqual(result["reason"], "PRINCIPAL_MISMATCH")

    def test_12_matching_claim_is_audit_only(self):
        result, valid = self.observe(
            self.fx.env(SHADOW_CLAIMED_PRINCIPAL="github-owner")
        )
        self.assertTrue(valid)
        self.assertEqual(result["authorized_principal"], "github-owner")
        self.assertEqual(result["claimed_principal"], "github-owner")

    def test_13_rerun_attempt_fails_closed(self):
        result, valid = self.observe(self.fx.env(GITHUB_RUN_ATTEMPT="2"))
        self.assertFalse(valid)
        self.assertEqual(result["reason"], "RERUN_NOT_NEW_OBSERVATION")

    def test_14_stale_issue_is_rejected(self):
        result, valid = self.observe(self.fx.env(SHADOW_ISSUE_DATE="2026-09-18"))
        self.assertFalse(valid)
        self.assertEqual(result["reason"], "STALE_ISSUE")

    def test_15_wrong_design_batch_is_rejected(self):
        self.fx.design(batch=5)
        env = self.fx.env(
            SHADOW_STAGE="DESIGN_SELECTION",
            SHADOW_COMMAND="1 1",
            SHADOW_DESIGN_BATCH_ID="4",
        )
        result, valid = self.observe(env, gate=False)
        self.assertFalse(valid)
        self.assertEqual(result["reason"], "STALE_DESIGN_BATCH")

    def test_16_design_batch_is_required(self):
        self.fx.design()
        env = self.fx.env(
            SHADOW_STAGE="DESIGN_SELECTION",
            SHADOW_COMMAND="1 1",
            SHADOW_DESIGN_BATCH_ID="",
        )
        result, valid = self.observe(env, gate=False)
        self.assertFalse(valid)
        self.assertEqual(result["reason"], "MISSING_DESIGN_BATCH_ID")

    def test_17_gate_a_prohibits_design_batch(self):
        result, valid = self.observe(self.fx.env(SHADOW_DESIGN_BATCH_ID="4"))
        self.assertFalse(valid)
        self.assertEqual(result["reason"], "UNEXPECTED_DESIGN_BATCH")

    def test_18_advanced_same_gate_transition_is_no_op(self):
        self.fx.write(
            self.fx.state / "approved_story.json",
            {
                "issue_date": ISSUE,
                "state": "APPROVED_STORY",
                "approval_reply": "3",
            },
        )
        result, valid = self.observe()
        self.assertTrue(valid)
        self.assertEqual(result["decision"], "NO_OP_ALREADY_APPLIED")

    def test_19_advanced_different_gate_transition_conflicts(self):
        self.fx.write(
            self.fx.state / "approved_story.json",
            {
                "issue_date": ISSUE,
                "state": "APPROVED_STORY",
                "approval_reply": "2",
            },
        )
        result, valid = self.observe()
        self.assertTrue(valid)
        self.assertEqual(result["decision"], "REJECT_CONFLICT")

    def test_20_snapshot_identity_is_deterministic(self):
        first = production_snapshot(
            self.fx.root,
            stage=ApprovalStage.GATE_A,
            requested_issue=ISSUE,
            commit_sha="a" * 40,
            gate_package_path=self.fx.gate,
        )
        second = production_snapshot(
            self.fx.root,
            stage=ApprovalStage.GATE_A,
            requested_issue=ISSUE,
            commit_sha="a" * 40,
            gate_package_path=self.fx.gate,
        )
        self.assertEqual(first["identity"], second["identity"])

    def test_21_snapshot_identity_changes_with_state(self):
        first, _ = self.observe()
        self.fx.write(
            self.fx.state / "approved_story.json",
            {"issue_date": ISSUE, "state": "APPROVED_STORY", "approval_reply": "3"},
        )
        second, _ = self.observe()
        self.assertNotEqual(
            first["production_state_snapshot_identity"],
            second["production_state_snapshot_identity"],
        )

    def test_22_create_observation_does_not_write_repository(self):
        before = sorted(path.relative_to(self.fx.root) for path in self.fx.root.rglob("*"))
        self.observe()
        after = sorted(path.relative_to(self.fx.root) for path in self.fx.root.rglob("*"))
        self.assertEqual(before, after)

    def test_23_all_explicit_safety_fields_are_false(self):
        result, _ = self.observe()
        for key in SAFETY_FIELDS:
            self.assertIs(result[key], False)

    def test_24_observed_upstream_is_separate_from_input_metadata(self):
        result, _ = self.observe(
            self.fx.env(
                SHADOW_UPSTREAM_RUN_ID="claimed-5",
                SHADOW_OBSERVED_UPSTREAM_RUN_ID="observed-9",
            )
        )
        self.assertEqual(result["upstream_run_id"], "claimed-5")
        self.assertEqual(result["observed_upstream_run_id"], "observed-9")

    def test_25_output_outside_runner_temp_is_rejected(self):
        result, _ = self.observe()
        runner = self.fx.root / "runner"
        runner.mkdir()
        with self.assertRaises(ShadowInputError) as caught:
            write_outputs(
                result,
                output_path=self.fx.root / "outside.json",
                summary_path=runner / "summary.md",
                runner_temp=runner,
            )
        self.assertEqual(caught.exception.reason, "OUTPUT_OUTSIDE_RUNNER_TEMP")

    def test_26_outputs_are_written_only_to_runner_temp(self):
        result, _ = self.observe()
        runner = self.fx.root / "runner"
        write_outputs(
            result,
            output_path=runner / "out" / "observation.json",
            summary_path=runner / "out" / "summary.md",
            runner_temp=runner,
        )
        loaded = json.loads((runner / "out" / "observation.json").read_text())
        self.assertEqual(loaded["observation_id"], result["observation_id"])

    def test_27_summary_declares_no_side_effects(self):
        result, _ = self.observe()
        summary = render_summary(result)
        self.assertIn("Production side effects: **NONE**", summary)
        self.assertNotIn("GMAIL_APP_PASSWORD", summary)

    def test_28_observed_checkout_sha_overrides_dispatch_sha(self):
        result, valid = self.observe(
            self.fx.env(SHADOW_COMMIT_SHA="b" * 40, GITHUB_SHA="a" * 40)
        )
        self.assertTrue(valid)
        self.assertEqual(result["commit_sha"], "b" * 40)

    def test_29_trusted_principal_and_event_identity_are_github_derived(self):
        result, valid = self.observe()
        self.assertTrue(valid)
        self.assertEqual(result["authorized_principal"], "github-owner")
        self.assertEqual(result["principal_source"], "GITHUB_WORKFLOW_CONTEXT")
        self.assertEqual(result["source_event_id"], "gh-run:123456")


if __name__ == "__main__":
    unittest.main()
