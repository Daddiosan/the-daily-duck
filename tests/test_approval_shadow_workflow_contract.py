import re
import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INGRESS_PATH = ROOT / ".github/workflows/approval-shadow.yml"
RECONCILE_PATH = ROOT / ".github/workflows/approval-shadow-reconcile.yml"
INGRESS = INGRESS_PATH.read_text(encoding="utf-8")
RECONCILE = RECONCILE_PATH.read_text(encoding="utf-8")
ALL = INGRESS + "\n" + RECONCILE


class ApprovalShadowWorkflowContractTests(unittest.TestCase):
    def test_01_ingress_is_manual_only(self):
        self.assertIn("workflow_dispatch:", INGRESS)
        self.assertNotIn("schedule:", INGRESS)

    def test_02_reconciliation_uses_only_workflow_run(self):
        self.assertIn("workflow_run:", RECONCILE)
        self.assertNotIn("schedule:", RECONCILE)
        self.assertNotIn("workflow_dispatch:", RECONCILE)

    def test_03_reconciliation_names_only_two_pollers(self):
        self.assertIn("The Daily Duck - Gate A Approval Check", RECONCILE)
        self.assertIn("The Daily Duck - Design Selection Check", RECONCILE)
        self.assertNotIn("The Daily Duck - Website Publish", RECONCILE)

    def test_04_permissions_are_read_only(self):
        for text in (INGRESS, RECONCILE):
            self.assertIn("contents: read", text)
            self.assertIn("actions: read", text)

    def test_05_write_permissions_are_forbidden(self):
        for permission in (
            "contents: write", "actions: write", "id-token: write",
            "deployments: write", "issues: write", "pull-requests: write",
        ):
            self.assertNotIn(permission, ALL)

    def test_06_checkout_credentials_are_not_persisted(self):
        self.assertEqual(ALL.count("persist-credentials: false"), 2)

    def test_07_artifacts_are_always_uploaded_for_30_days(self):
        self.assertEqual(ALL.count("uses: actions/upload-artifact@v4"), 2)
        self.assertEqual(ALL.count("retention-days: 30"), 2)
        self.assertGreaterEqual(ALL.count("if: always()"), 4)

    def test_08_production_secrets_are_absent(self):
        for secret in (
            "GMAIL_APP_PASSWORD", "GMAIL_ADDRESS", "OPENAI_API_KEY",
            "GEMINI_API_KEY", "X_API_KEY", "X_ACCESS_TOKEN",
        ):
            self.assertNotIn(secret, ALL)

    def test_09_git_mutation_commands_are_absent(self):
        for command in ("git add", "git commit", "git push", "git reset", "git pull"):
            self.assertNotIn(command, ALL)

    def test_10_downstream_dispatch_commands_are_absent(self):
        self.assertNotIn("gh workflow run", ALL)
        self.assertNotRegex(ALL, r"actions/workflows/.+/dispatches")
        self.assertNotRegex(ALL, r"--method\s+(POST|PUT|PATCH|DELETE)")

    def test_11_side_effect_scripts_are_absent(self):
        for script in (
            "publish_website.py", "publish_x.py", "send_email.py",
            "send_design_approval_email.py", "check_story_approval.py",
            "check_design_selection.py",
        ):
            self.assertNotIn(script, ALL)

    def test_12_observation_paths_use_runner_temp(self):
        self.assertIn("$RUNNER_TEMP/phase3b-shadow-event", INGRESS)
        self.assertIn("$RUNNER_TEMP/phase3b-shadow-comparison", RECONCILE)
        self.assertNotRegex(ALL, r">\s*[\"']?automation_state/")
        self.assertNotRegex(ALL, r">\s*[\"']?monitor_state/")

    def test_13_no_confusable_trusted_identity_input_exists(self):
        input_block = INGRESS.split("permissions:", 1)[0]
        for name in ("trusted_principal:", "authenticated_principal:", "actor:"):
            self.assertNotIn(name, input_block)
        self.assertIn("GITHUB_ACTOR", (ROOT / "scripts/approval_shadow.py").read_text())

    def test_14_allowlist_uses_non_secret_repository_variable(self):
        self.assertIn("vars.APPROVAL_SHADOW_ACTORS", INGRESS)
        self.assertNotIn("secrets.APPROVAL_SHADOW_ACTORS", INGRESS)

    def test_15_reconciliation_api_calls_are_get_only(self):
        calls = re.findall(r"gh api[^\n]*(?:\\\n[^\n]*)*", RECONCILE)
        self.assertTrue(calls)
        self.assertEqual(RECONCILE.count("gh api --method GET"), 2)

    def test_16_snapshot_sha_comes_from_checked_out_main(self):
        self.assertIn("SHADOW_COMMIT_SHA=$(git rev-parse HEAD)", INGRESS)

    def test_17_reconciliation_is_limited_to_main(self):
        self.assertIn("branches:\n      - main", RECONCILE)

    def test_18_shadow_scripts_import_no_mutating_transport_clients(self):
        forbidden = {"subprocess", "smtplib", "imaplib", "requests", "socket"}
        for script in ("approval_shadow.py", "approval_shadow_compare.py"):
            tree = ast.parse((ROOT / "scripts" / script).read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertFalse(imported & forbidden, f"{script}: {imported & forbidden}")

    def test_19_shadow_scripts_reference_no_production_side_effect_modules(self):
        scripts = "\n".join(
            (ROOT / "scripts" / name).read_text(encoding="utf-8")
            for name in ("approval_shadow.py", "approval_shadow_compare.py")
        )
        for value in (
            "check_story_approval", "check_design_selection", "send_email",
            "send_design_approval_email", "publish_website", "publish_x",
            "generate_image_concepts",
        ):
            self.assertNotIn(value, scripts)

    def test_20_no_shadow_workflow_uses_any_secrets_context(self):
        self.assertNotIn("secrets.", ALL)
        self.assertEqual(ALL.count("github.token"), 2)


if __name__ == "__main__":
    unittest.main()
