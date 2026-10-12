from pathlib import Path
import unittest


WORKFLOW = Path(".github/workflows/backfill-2026-10-11-design-options.yml")


class BackfillWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_is_manual_and_human_gated(self):
        self.assertIn("workflow_dispatch:", self.text)
        self.assertIn("SEND-2026-10-11-DESIGN-OPTIONS", self.text)
        self.assertIn('test "$GITHUB_REF" = "refs/heads/main"', self.text)
        self.assertIn('test "$GITHUB_ACTOR" = "Daddiosan"', self.text)
        self.assertIn('test "$GITHUB_TRIGGERING_ACTOR" = "Daddiosan"', self.text)
        self.assertIn("environment: Production", self.text)
        self.assertIn("ref: ${{ github.sha }}", self.text)
        self.assertNotIn("schedule:", self.text)

    def test_is_fixed_to_one_issue_and_exact_artifact(self):
        self.assertIn('TARGET_ISSUE_DATE: "2026-10-11"', self.text)
        self.assertIn('EXPECTED_RUN_ID: "38099673013"', self.text)
        self.assertIn('EXPECTED_ARTIFACT_ID: "11686304073"', self.text)
        self.assertNotIn("force_regenerate", self.text)

    def test_uses_isolated_temp_state_and_read_only_repository_permission(self):
        self.assertIn("${{ runner.temp }}/daily-duck-backfill-2026-10-11", self.text)
        self.assertIn("contents: read", self.text)
        self.assertNotIn("contents: write", self.text)
        self.assertNotIn("git push", self.text)
        self.assertNotIn("git commit", self.text)

    def test_actions_and_dependencies_are_immutable(self):
        self.assertNotIn("actions/checkout@v", self.text)
        self.assertNotIn("actions/setup-python@v", self.text)
        self.assertNotIn("actions/upload-artifact@v", self.text)
        self.assertIn("requirements-backfill-2026-10-11.txt", self.text)
        self.assertNotIn("pip install --upgrade", self.text)

    def test_does_not_invoke_publication(self):
        self.assertNotIn("gh workflow run website-publish", self.text)
        self.assertNotIn("gh workflow run x-publish", self.text)
        self.assertIn("WEBSITE_PUBLISH_EXECUTED: NO", self.text)
        self.assertIn("X_PUBLISH_EXECUTED: NO", self.text)

    def test_duplicate_guard_precedes_generation_and_send(self):
        duplicate = self.text.index("Stop before generation when the design email already exists")
        generate = self.text.index("Generate exactly 3 concepts, previews, and titles")
        send = self.text.index("Send the one permitted design-selection email")
        self.assertLess(duplicate, generate)
        self.assertLess(generate, send)

    def test_dry_run_precedes_send(self):
        dry_run = self.text.index("Dry-run the complete design email")
        send = self.text.index("Send the one permitted design-selection email")
        self.assertLess(dry_run, send)

    def test_sent_mail_is_verified_after_send(self):
        send = self.text.index("Send the one permitted design-selection email")
        verify = self.text.index("Verify the sent message in Gmail")
        self.assertLess(send, verify)
        self.assertIn("steps.verify_sent.outcome == 'success'", self.text)

    def test_prepared_evidence_is_uploaded_before_smtp(self):
        prepared = self.text.index("Upload prepared recovery evidence before SMTP")
        send = self.text.index("Send the one permitted design-selection email")
        final = self.text.index("Upload final isolated recovery evidence")
        self.assertLess(prepared, send)
        self.assertLess(send, final)


if __name__ == "__main__":
    unittest.main()
