"""Small offline guard tests. No real provider, SMTP, IMAP or image-generation call."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TASK = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("recovery_design", TASK / "run_design.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

SETTINGS = dict(model="gpt-image-2", n=1, size="1536x1024", quality="medium",
                output_format="png", prompt="One friendly duck in a fictional icy landscape.")


class Guards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.evidence = patch.object(runner, "EVIDENCE", Path(self.temp.name))
        self.evidence.start()
        self.addCleanup(self.evidence.stop)

    def test_fourth_request_is_blocked_before_endpoint(self):
        endpoint = SimpleNamespace(generate=unittest.mock.Mock(return_value=SimpleNamespace(usage=None)))
        budget = runner.BudgetImages(endpoint, {"requests": []})
        for _ in range(3):
            budget.generate(**SETTINGS)
        with self.assertRaisesRegex(RuntimeError, "limit"):
            budget.generate(**SETTINGS)
        self.assertEqual(endpoint.generate.call_count, 3)

    def test_uncertain_request_is_recorded_and_never_retried(self):
        endpoint = SimpleNamespace(generate=unittest.mock.Mock(side_effect=TimeoutError))
        ledger = {"requests": []}
        budget = runner.BudgetImages(endpoint, ledger)
        with self.assertRaisesRegex(RuntimeError, "no retry"):
            budget.generate(**SETTINGS)
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            budget.generate(**SETTINGS)
        self.assertEqual(endpoint.generate.call_count, 1)
        self.assertEqual(ledger["requests"][0]["state"], "UNKNOWN_OR_FAILED_NO_RETRY")
        self.assertTrue((runner.EVIDENCE / "cost_ledger.json").exists())

    def test_changed_quality_or_large_prompt_blocks_before_endpoint(self):
        endpoint = SimpleNamespace(generate=unittest.mock.Mock())
        budget = runner.BudgetImages(endpoint, {"requests": []})
        for change in [dict(quality="high"), dict(prompt="x" * 6001)]:
            with self.assertRaises(RuntimeError):
                budget.generate(**(SETTINGS | change))
        endpoint.generate.assert_not_called()

    def test_usage_cost_jump_stops_before_another_image(self):
        usage = SimpleNamespace(model_dump=lambda: {"input_tokens": 500, "output_tokens": 12000})
        endpoint = SimpleNamespace(generate=unittest.mock.Mock(return_value=SimpleNamespace(usage=usage)))
        budget = runner.BudgetImages(endpoint, {"requests": []})
        with self.assertRaisesRegex(RuntimeError, "projection"):
            budget.generate(**SETTINGS)
        with self.assertRaisesRegex(RuntimeError, "Projected"):
            budget.generate(**SETTINGS)
        self.assertEqual(endpoint.generate.call_count, 1)

    def test_live_mode_refuses_main_and_local_execution(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "identity"):
                runner.live_guard()

    def test_restored_story_and_three_distinct_title_lanes(self):
        approved, concepts, titles, prompts = runner.inputs()
        self.assertEqual(approved["selected_story_number"], 3)
        self.assertEqual(len(concepts), 3)
        self.assertEqual(len(set(t["title"] for t in titles)), 3)
        self.assertTrue(all(len(p.encode("utf-8")) <= 6000 for p in prompts))

    def test_existing_sent_email_blocks_before_generation(self):
        with patch.object(runner, "sent_matches", return_value=[object()]):
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                runner.no_duplicate()

    def test_smtp_uncertainty_keeps_token_digest_and_attempt_before_send(self):
        from PIL import Image
        from hashlib import sha256
        approved, concepts, titles, _ = runner.inputs()
        candidate = Path(self.temp.name) / "candidate"
        (candidate / "automation_state").mkdir(parents=True)
        previews = []
        for number, color in enumerate(["yellow", "blue", "white"], 1):
            path = candidate / f"test-only-{number}.png"
            Image.new("RGB", (2, 2), color).save(path)
            previews.append({"number": number, "image_path": str(path),
                             "sha256": sha256(path.read_bytes()).hexdigest()})
        target = candidate / "automation_state/design_options.json"
        runner.write(target, {"state": "DESIGN_OPTIONS_READY", "issue_date": runner.DATE,
                              "approved_story": approved, "image_concepts": concepts,
                              "title_ideas": titles, "design_previews": previews})
        original_cwd = Path.cwd()
        try:
            with patch.object(runner, "CANDIDATE", candidate), \
                 patch.object(runner, "live_guard"), \
                 patch.object(runner, "sent_matches", return_value=[]), \
                 patch.dict(os.environ, {"GMAIL_ADDRESS": "sender@example.invalid",
                                         "EMAIL_TO": "reviewer@example.invalid"}), \
                 patch.object(runner.mailer, "send_message", side_effect=TimeoutError) as smtp:
                with self.assertRaises(TimeoutError):
                    runner.send()
                self.assertEqual(runner.read(target)["state"], "EMAIL_SEND_PENDING")
                self.assertEqual(len(runner.read(target)["approval_token_digest"]), 64)
                self.assertEqual(runner.read(runner.EVIDENCE / "email_attempt.json")["state"],
                                 "ATTEMPTING_NO_RETRY")
                with self.assertRaisesRegex(RuntimeError, "Prior SMTP"):
                    runner.send()
                self.assertEqual(smtp.call_count, 1)
        finally:
            os.chdir(original_cwd)

    def test_workflow_has_no_write_permission_or_publication(self):
        import yaml
        workflow = yaml.safe_load((runner.REPO / ".github/workflows/design-options.yml").read_text())
        self.assertEqual(workflow["permissions"], {"contents": "read", "actions": "read"})
        steps = workflow["jobs"]["generate-and-email"]["steps"]
        commands = "\n".join(str(s.get("run", "")) for s in steps)
        for forbidden in ["git push", "publish_website.py", "publish_x.py", "gh workflow run", "reset --hard"]:
            self.assertNotIn(forbidden, commands)
        self.assertIn("GITHUB_RUN_ATTEMPT", commands)
        self.assertIn("total_count", commands)
        self.assertEqual([s.get("run") for s in steps].count(
            "python recovery/2026-11-02/run_design.py --generate"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
