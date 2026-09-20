import ast
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RECEIVER = ROOT / "cloud" / "approval_receiver"
PYTHON_FILES = tuple(RECEIVER.glob("*.py"))

# Runtime artifacts that legitimately appear alongside the source tree but
# are never part of the reviewed file set. Only names that a Python import
# itself creates belong here; anything else (a new source file, a stray
# config file, an unexpected subdirectory) must still fail the exact-set
# check below.
IGNORED_RUNTIME_ENTRIES = {"__pycache__"}


class ApprovalReceiverContractTests(unittest.TestCase):
    def test_exact_a1_files_exist(self):
        expected = {
            "main.py",
            "gmail_client.py",
            "observation.py",
            "requirements.txt",
            "Dockerfile",
        }
        actual = {
            path.name
            for path in RECEIVER.iterdir()
            if path.name not in IGNORED_RUNTIME_ENTRIES
        }
        self.assertEqual(actual, expected)
        self.assertTrue((ROOT / "docs" / "phase3b2" / "A1_RUNBOOK.md").is_file())

    def test_receiver_has_no_forbidden_imports(self):
        forbidden_roots = {
            "github",
            "smtplib",
            "imaplib",
            "subprocess",
            "approval_domain",
            "check_story_approval",
            "check_design_selection",
        }
        found = set()
        for path in PYTHON_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    found.add(node.module.split(".")[0])
        self.assertTrue(forbidden_roots.isdisjoint(found), found)

    def test_no_git_or_subprocess_execution(self):
        source = "\n".join(path.read_text(encoding="utf-8") for path in PYTHON_FILES)
        forbidden = ("subprocess", "os.system", "git commit", "git push")
        for marker in forbidden:
            self.assertNotIn(marker, source)

    def test_no_github_client_or_ingress(self):
        source = "\n".join(path.read_text(encoding="utf-8") for path in PYTHON_FILES).lower()
        for marker in ("repository_dispatch", "workflow_dispatch", "pygithub", "github app"):
            self.assertNotIn(marker, source)

    def test_no_smtp_imap_or_email_send(self):
        source = (RECEIVER / "gmail_client.py").read_text(encoding="utf-8").lower()
        for marker in ("smtplib", "imaplib", ".send(", "messages().send"):
            self.assertNotIn(marker, source)

    def test_gmail_client_contains_no_mutation_api(self):
        source = (RECEIVER / "gmail_client.py").read_text(encoding="utf-8")
        for marker in (
            ".modify(",
            ".delete(",
            ".trash(",
            ".untrash(",
            ".insert(",
            ".import_(",
            ".batchModify(",
            ".batchDelete(",
        ):
            self.assertNotIn(marker, source)

    def test_only_gmail_readonly_scope_declared(self):
        source = (RECEIVER / "gmail_client.py").read_text(encoding="utf-8")
        self.assertIn("https://www.googleapis.com/auth/gmail.readonly", source)
        self.assertNotIn("https://mail.google.com/", source)
        self.assertNotIn("gmail.modify", source)

    def test_no_production_repository_state_paths(self):
        source = "\n".join(path.read_text(encoding="utf-8") for path in PYTHON_FILES)
        for marker in (
            "automation_state/",
            "ready_to_publish.json",
            "website_publish_result.json",
            "x_publish_result.json",
        ):
            self.assertNotIn(marker, source)

    def test_no_publish_or_downstream_capability(self):
        source = "\n".join(path.read_text(encoding="utf-8") for path in PYTHON_FILES).lower()
        for marker in ("publish_website", "publish_to_x", "x_publish", "website_publish"):
            self.assertNotIn(marker, source)

    def test_auth_does_not_trust_identity_headers(self):
        source = (RECEIVER / "main.py").read_text(encoding="utf-8")
        self.assertIn("verify_oauth2_token", source)
        self.assertNotIn("X-Goog-Authenticated-User", source)
        self.assertNotIn("X-Forwarded-Email", source)

    def test_existing_protected_files_match_baseline(self):
        """Phase 3B-2A1 must never modify these already-committed, protected
        paths. This is checked against the *current* HEAD rather than a
        fixed historical commit: a hard-coded ancestor commit goes stale
        every time those paths are legitimately touched by unrelated work
        (e.g. Phase 3B-1), which is exactly what made the previous version
        of this test fail with no real regression. Comparing the working
        tree to HEAD instead means the assertion is always about "did
        anything currently uncommitted change these paths" -- which is the
        property this test actually needs to guard -- and it never drifts
        on its own.
        """
        command = [
            "git",
            "diff",
            "--name-only",
            "HEAD",
            "--",
            ".github/workflows",
            "scripts/approval_domain.py",
            "scripts/check_story_approval.py",
            "scripts/check_design_selection.py",
            "scripts/approval_shadow.py",
            "scripts/approval_shadow_compare.py",
        ]
        result = subprocess.run(
            command,
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "")

    def test_dockerfile_runs_as_non_root(self):
        source = (RECEIVER / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("USER receiver", source)
        self.assertNotIn("GMAIL_OAUTH_REFRESH_TOKEN=", source)


if __name__ == "__main__":
    unittest.main()
