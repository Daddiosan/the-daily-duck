from pathlib import Path
import unittest


WORKFLOW = Path(".github/workflows/backfill-2026-10-11-final-selection.yml")


class FinalSelectionWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_manual_human_gate_is_fixed_to_main_and_owner(self):
        self.assertIn("workflow_dispatch:", self.text)
        self.assertNotIn("schedule:", self.text)
        self.assertIn("CREATE-2026-10-11-READY-TO-PUBLISH", self.text)
        self.assertIn('test "$GITHUB_REF" = "refs/heads/main"', self.text)
        self.assertIn('test "$GITHUB_ACTOR" = "Daddiosan"', self.text)
        self.assertIn('test "$GITHUB_TRIGGERING_ACTOR" = "Daddiosan"', self.text)
        self.assertIn("ref: ${{ github.sha }}", self.text)

    def test_exact_source_and_output_artifacts_are_fixed(self):
        self.assertIn('SOURCE_RUN_ID: "38198699096"', self.text)
        self.assertIn('SOURCE_ARTIFACT_ID: "11724976131"', self.text)
        self.assertIn("daily-duck-backfill-2026-10-11-final", self.text)
        self.assertIn("daily-duck-backfill-2026-10-11-ready-to-publish", self.text)

    def test_isolated_read_only_and_idempotent(self):
        self.assertIn('$RUNNER_TEMP/daily-duck-final-selection-2026-10-11', self.text)
        self.assertIn('>> "$GITHUB_ENV"', self.text)
        self.assertIn("contents: read", self.text)
        self.assertNotIn("contents: write", self.text)
        self.assertIn("check-duplicate-artifacts", self.text)
        self.assertNotIn("git push", self.text)
        self.assertNotIn("git commit", self.text)

    def test_no_email_generation_or_publication_path(self):
        forbidden = (
            "send_design_approval_email.py",
            "generate_image_concepts.py",
            "design-selection-check.yml",
            "website-publish.yml",
            "x-publish.yml",
            "gh workflow run",
        )
        for value in forbidden:
            self.assertNotIn(value, self.text)
        self.assertIn("EMAIL_SENT: NO", self.text)
        self.assertIn("IMAGE_REGENERATED: NO", self.text)
        self.assertIn("WEBSITE_PUBLISH_EXECUTED: NO", self.text)
        self.assertIn("X_PUBLISH_EXECUTED: NO", self.text)

    def test_only_expected_ready_files_are_uploaded(self):
        self.assertIn("ready_to_publish.json", self.text)
        self.assertIn("final_selection_backfill_report.json", self.text)
        self.assertIn("automation_images/canonical/2026-10-11.png", self.text)
        self.assertNotIn("automation_state/website_publish_result.json", self.text)
        self.assertNotIn("automation_state/x_publish_result.json", self.text)


if __name__ == "__main__":
    unittest.main()
