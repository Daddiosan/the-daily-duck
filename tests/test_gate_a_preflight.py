from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.fetch_gate_a_artifact import ArtifactSelectionError, GitHubReadError
from scripts.gate_a_preflight import (
    GateAPreflightError,
    PreflightAction,
    evaluate_gate_a_relevance,
    main,
)


ISSUE = "2026-10-05"
PRIOR_ISSUE = "2026-10-04"


class GateAPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name) / "automation_state"
        self.state_dir.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, filename, payload):
        (self.state_dir / filename).write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

    def write_raw(self, filename, payload):
        (self.state_dir / filename).write_text(payload, encoding="utf-8")

    def decide(self):
        return evaluate_gate_a_relevance(
            state_dir=self.state_dir,
            expected_issue_date=ISSUE,
        )

    def pipeline(self, artifact_fetch):
        decision = self.decide()
        if decision.action is PreflightAction.NO_ACTION_REQUIRED:
            return "SUCCESS_NOOP"
        artifact_fetch()
        return "NORMAL_DISPATCH"

    def test_case_1_waiting_for_gate_a_with_valid_approval_uses_normal_path(self):
        calls = []

        def valid_approval_path():
            calls.append("artifact_then_valid_approval")

        self.assertEqual(self.pipeline(valid_approval_path), "NORMAL_DISPATCH")
        self.assertEqual(calls, ["artifact_then_valid_approval"])

    def test_case_2_already_approved_is_success_noop(self):
        self.write(
            "approved_story.json",
            {"issue_date": ISSUE, "state": "APPROVED_STORY"},
        )
        decision = self.decide()
        self.assertIs(decision.action, PreflightAction.NO_ACTION_REQUIRED)
        self.assertEqual(decision.reason, "ALREADY_APPROVED")

    def test_case_3_design_selected_is_success_noop(self):
        self.write(
            "design_options.json",
            {
                "issue_date": ISSUE,
                "state": "DESIGN_SELECTED_READY_TO_PUBLISH",
            },
        )
        decision = self.decide()
        self.assertIs(decision.action, PreflightAction.NO_ACTION_REQUIRED)
        self.assertEqual(decision.reason, "DESIGN_ALREADY_COMPLETED")

    def test_case_4_website_published_is_success_noop(self):
        self.write(
            "website_publish_result.json",
            {"issue_date": ISSUE, "action": "PUBLISHED"},
        )
        decision = self.decide()
        self.assertIs(decision.action, PreflightAction.NO_ACTION_REQUIRED)
        self.assertEqual(decision.reason, "WEBSITE_ALREADY_PUBLISHED")

    def test_case_5_x_posted_terminal_is_success_noop(self):
        self.write(
            "x_publish_result.json",
            {"issue_date": ISSUE, "action": "X_POSTED"},
        )
        decision = self.decide()
        self.assertIs(decision.action, PreflightAction.NO_ACTION_REQUIRED)
        self.assertEqual(decision.reason, "TERMINAL_X_POSTED")

    def test_case_6_unfinished_missing_artifact_remains_failure(self):
        def missing_artifact():
            raise ArtifactSelectionError("missing expected artifact")

        with self.assertRaises(ArtifactSelectionError):
            self.pipeline(missing_artifact)

    def test_case_7_duplicate_periodic_wakes_are_idempotent_success_noops(self):
        self.write(
            "approved_story.json",
            {"issue_date": ISSUE, "state": "APPROVED_STORY"},
        )

        def forbidden_artifact_fetch():
            self.fail("artifact fetch must be skipped after Gate A completion")

        self.assertEqual(self.pipeline(forbidden_artifact_fetch), "SUCCESS_NOOP")
        self.assertEqual(self.pipeline(forbidden_artifact_fetch), "SUCCESS_NOOP")

    def test_case_8_terminal_skips_stale_and_anomalous_artifact_lookups(self):
        self.write(
            "x_publish_result.json",
            {"issue_date": ISSUE, "action": "X_POSTED"},
        )

        def stale_artifact_result():
            raise ArtifactSelectionError("stale_run=20")

        def artifact_api_anomaly():
            raise GitHubReadError("list successful Daily Duck runs", 503)

        for fetch in (stale_artifact_result, artifact_api_anomaly):
            with self.subTest(fetch=fetch.__name__):
                self.assertEqual(self.pipeline(fetch), "SUCCESS_NOOP")

    def test_mixed_issue_dates_fail_closed(self):
        self.write(
            "approved_story.json",
            {"issue_date": ISSUE, "state": "APPROVED_STORY"},
        )
        self.write(
            "website_publish_result.json",
            {"issue_date": PRIOR_ISSUE, "action": "PUBLISHED"},
        )
        with self.assertRaisesRegex(GateAPreflightError, "contradictory issue dates"):
            self.decide()

    def test_prior_issue_mismatch_cannot_noop_and_missing_artifact_fails(self):
        self.write(
            "approved_story.json",
            {"issue_date": PRIOR_ISSUE, "state": "APPROVED_STORY"},
        )

        def missing_current_artifact():
            raise ArtifactSelectionError("missing expected current-issue artifact")

        with self.assertRaises(ArtifactSelectionError):
            self.pipeline(missing_current_artifact)

    def test_future_issue_date_fails_closed(self):
        self.write(
            "approved_story.json",
            {"issue_date": "2026-10-06", "state": "APPROVED_STORY"},
        )
        with self.assertRaisesRegex(GateAPreflightError, "does not match"):
            self.decide()

    def test_corrupt_state_fails_closed(self):
        (self.state_dir / "approved_story.json").write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(GateAPreflightError, "corrupt"):
            self.decide()

    def test_unknown_state_fails_closed(self):
        self.write(
            "approved_story.json",
            {"issue_date": ISSUE, "state": "MYSTERY_STATE"},
        )
        with self.assertRaisesRegex(GateAPreflightError, "unknown state"):
            self.decide()

    def test_date_alias_disagreement_fails_closed(self):
        self.write(
            "approved_story.json",
            {
                "issue_date": ISSUE,
                "date": PRIOR_ISSUE,
                "state": "APPROVED_STORY",
            },
        )
        with self.assertRaisesRegex(GateAPreflightError, "aliases disagree"):
            self.decide()

    def test_missing_issue_date_cannot_be_replaced_by_date_alias(self):
        self.write(
            "approved_story.json",
            {"date": ISSUE, "state": "APPROVED_STORY"},
        )
        with self.assertRaisesRegex(GateAPreflightError, "issue_date is missing"):
            self.decide()

    def test_duplicate_json_keys_fail_closed(self):
        for payload in (
            '{"issue_date":"2026-10-06","issue_date":"2026-10-05",'
            '"state":"APPROVED_STORY"}',
            '{"issue_date":"2026-10-05","state":"MYSTERY_STATE",'
            '"state":"APPROVED_STORY"}',
        ):
            with self.subTest(payload=payload):
                self.write_raw("approved_story.json", payload)
                with self.assertRaisesRegex(GateAPreflightError, "duplicate JSON key"):
                    self.decide()

    def test_coherent_prior_issue_is_not_used_as_current_noop_evidence(self):
        self.write(
            "approved_story.json",
            {"issue_date": PRIOR_ISSUE, "state": "APPROVED_STORY"},
        )
        self.write(
            "x_publish_result.json",
            {"issue_date": PRIOR_ISSUE, "action": "X_POSTED"},
        )
        decision = self.decide()
        self.assertIs(decision.action, PreflightAction.CHECK_REQUIRED)
        self.assertEqual(decision.reason, "PRIOR_ISSUE_STATE_ONLY")

    def test_cli_failure_does_not_write_success_outputs(self):
        (self.state_dir / "approved_story.json").write_text("not-json", encoding="utf-8")
        github_output = Path(self.temp.name) / "github-output"
        self.assertEqual(
            main(
                [
                    "--expected-issue-date",
                    ISSUE,
                    "--state-dir",
                    str(self.state_dir),
                    "--github-output",
                    str(github_output),
                ]
            ),
            1,
        )
        self.assertFalse(github_output.exists())

    def test_cli_noop_writes_stable_outputs(self):
        self.write(
            "approved_story.json",
            {"issue_date": ISSUE, "state": "APPROVED_STORY"},
        )
        github_output = Path(self.temp.name) / "github-output"
        self.assertEqual(
            main(
                [
                    "--expected-issue-date",
                    ISSUE,
                    "--state-dir",
                    str(self.state_dir),
                    "--github-output",
                    str(github_output),
                ]
            ),
            0,
        )
        self.assertEqual(
            github_output.read_text(encoding="utf-8"),
            "action=NO_ACTION_REQUIRED\n"
            "reason=ALREADY_APPROVED\n"
            "evidence=approved_story.json:APPROVED_STORY\n",
        )


class GateAWorkflowOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.workflow = (
            root / ".github/workflows/approval-check-phase2.yml"
        ).read_text(encoding="utf-8")

    def test_preflight_is_strictly_before_artifact_fetch(self):
        self.assertLess(
            self.workflow.index("python scripts/gate_a_preflight.py"),
            self.workflow.index("python scripts/fetch_gate_a_artifact.py"),
        )

    def test_noop_guards_all_side_effecting_gate_a_steps(self):
        guarded_steps = (
            "Fetch newest usable Daily Duck Automation artifact",
            "Prepare Gate A package",
            "Check Gate A story selection",
            "Commit APPROVED_STORY when created",
        )
        for name in guarded_steps:
            with self.subTest(name=name):
                marker = f"- name: {name}\n        if: "
                start = self.workflow.index(marker)
                condition = self.workflow[start : start + 220]
                self.assertIn(
                    "steps.preflight.outputs.action == 'CHECK_REQUIRED'",
                    condition,
                )

        trigger_start = self.workflow.index(
            "- name: Trigger Design Options automatically"
        )
        trigger = self.workflow[trigger_start : trigger_start + 320]
        self.assertIn(
            "if: steps.approval_commit.outputs.committed == 'true'",
            trigger,
        )

    def test_failure_alert_contract_is_unchanged(self):
        notify_start = self.workflow.index("- name: Notify failure by email")
        notify = self.workflow[notify_start : notify_start + 240]
        self.assertIn("if: failure()", notify)
        self.assertNotIn("preflight.outputs", notify)

    def test_polling_and_schedule_are_unchanged(self):
        self.assertIn('cron: "11,26,41,56 * * * *"', self.workflow)
        self.assertIn("workflow_dispatch:", self.workflow)


if __name__ == "__main__":
    unittest.main()
