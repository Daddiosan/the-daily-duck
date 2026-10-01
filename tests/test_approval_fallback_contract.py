from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "cloud" / "approval_fallback"


class ApprovalFallbackContractTests(unittest.TestCase):
    def test_exact_small_component_surface(self):
        files = {path.name for path in COMPONENT.iterdir() if path.name != "__pycache__"}
        self.assertEqual(files, {"main.py", "requirements.txt", "Dockerfile"})

    def test_docker_reuses_reviewed_narrow_github_boundary(self):
        dockerfile = (COMPONENT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("cloud/approval_relay/github_dispatch.py", dockerfile)
        self.assertIn("cloud/approval_relay/github_app_dispatch.py", dockerfile)
        self.assertIn("cloud/approval_relay/auth.py", dockerfile)
        for forbidden in (
            "check_design_selection.py",
            "check_story_approval.py",
            "approval_domain.py",
            "automation_state",
        ):
            self.assertNotIn(forbidden, dockerfile)

    def test_no_user_controlled_network_target_or_secret_logging(self):
        source = (COMPONENT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertNotIn("requests", imports)
        self.assertNotIn("urllib", imports)
        self.assertNotIn("subprocess", imports)
        self.assertNotIn("request.get_json", source)
        log_source = source.split("def _log", 1)[1].split("def create_app", 1)[0]
        self.assertNotIn("Authorization", log_source)
        self.assertNotIn("request", log_source)

    def test_downstream_serialization_and_terminal_guards_remain_present(self):
        gate_workflow = (ROOT / ".github/workflows/approval-check-phase2.yml").read_text(
            encoding="utf-8"
        )
        design_workflow = (
            ROOT / ".github/workflows/design-selection-check.yml"
        ).read_text(encoding="utf-8")
        story_checker = (ROOT / "scripts/check_story_approval.py").read_text(
            encoding="utf-8"
        )
        design_checker = (ROOT / "scripts/check_design_selection.py").read_text(
            encoding="utf-8"
        )
        for workflow in (gate_workflow, design_workflow):
            self.assertIn("concurrency:", workflow)
            self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("same_issue_already_approved", story_checker)
        self.assertIn('"ALREADY_SELECTED"', design_checker)


if __name__ == "__main__":
    unittest.main()
