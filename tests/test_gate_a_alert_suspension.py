import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

from scripts import automation_chain_audit as audit
from scripts import send_workflow_failure_email as mail
from tests.test_automation_chain_audit import NOW, healthy_runs, healthy_states, run


GATE_A = "The Daily Duck - Gate A Approval Check"
ROOT = Path(__file__).resolve().parents[1]
MAIL_ENV = {
    "GMAIL_ADDRESS": "sender@example.invalid",
    "GMAIL_APP_PASSWORD": "offline-placeholder",
    "EMAIL_TO": "reader@example.invalid",
}


class GateAAlertSuspensionTests(unittest.TestCase):
    def call_mail(self, environment):
        with patch.dict(os.environ, environment, clear=True), \
             patch.object(mail.smtplib, "SMTP_SSL") as smtp, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mail.main(), 0)
        return smtp

    def gate_failure(self):
        runs = healthy_runs()
        runs[GATE_A] = [run(GATE_A, 99, "failure")]
        return audit.evaluate(runs, healthy_states(), NOW)

    def test_direct_gate_a_warning_skips_before_credentials_and_smtp(self):
        smtp = self.call_mail({"FAILURE_STAGE": "Gate A approval check"})
        smtp.assert_not_called()

    def test_other_workflow_warnings_still_send_once(self):
        for stage in (
            "Daily Duck news collection / Gate A email",
            "Design Options - 3 concepts + 3 images + 3 titles",
            "Design Selection / NEXT 3 / Final Approval",
            "Website Publish", "X Publish", "Gate A approval check extra",
        ):
            with self.subTest(stage=stage):
                smtp = self.call_mail(MAIL_ENV | {"FAILURE_STAGE": stage})
                smtp.return_value.__enter__.return_value.send_message.assert_called_once()

    def test_gate_failure_is_detected_but_not_pending(self):
        current = self.gate_failure()
        self.assertTrue(any(p.workflow_name == GATE_A for p in current))
        self.assertEqual(audit.pending_alerts(current, set()), [])

    def test_gate_schedule_warning_is_suppressed_while_design_schedule_remains(self):
        current = audit.evaluate({}, {}, NOW)
        self.assertTrue(any(p.workflow_name == GATE_A for p in current))
        pending = audit.pending_alerts(current, set())
        self.assertFalse(any(p.workflow_name == GATE_A for p in pending))
        self.assertTrue(any(p.failure_stage == "Design Selection Check scheduled trigger" for p in pending))

    def test_mixed_audit_sends_other_warning_and_does_not_record_gate_as_delivered(self):
        runs = healthy_runs()
        runs[GATE_A] = [run(GATE_A, 99, "failure")]
        design = "The Daily Duck - Design Options"
        runs[design] = [run(design, 100, "failure")]
        fixture = {"now": NOW.isoformat(), "runs": runs, "states": healthy_states()}
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            fixture_path = folder / "fixture.json"
            result_path = folder / "audit.json"
            pending_path = folder / "monitor_state/pending_alerts.json"
            seen_path = folder / "monitor_state/seen_alerts.json"
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            args = ["audit", "--fixture", str(fixture_path), "--result-file", str(result_path),
                    "--pending-file", str(pending_path), "--seen-file", str(seen_path)]
            with patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(audit.main(), 1)  # failure status is preserved
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertTrue(any(p["workflow_name"] == GATE_A for p in payload["problems"]))
            pending = json.loads(pending_path.read_text(encoding="utf-8"))["alerts"]
            self.assertEqual([p["workflow_name"] for p in pending], [design])
            smtp = self.call_mail(MAIL_ENV | {
                "FAILURE_STAGE": "Scheduled workflow health check",
                "AUTOMATION_ALERTS_FILE": str(pending_path),
                "AUDIT_DETAILS": "Gate A details must not leak into a mixed notification",
            })
            message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            self.assertIn("Design Options", message.get_content())
            self.assertNotIn("Gate A", message.get_content())
            original = Path.cwd()
            try:
                os.chdir(folder)
                with contextlib.redirect_stdout(io.StringIO()):
                    runpy.run_path(str(ROOT / "scripts/merge_automation_alert_state.py"))
            finally:
                os.chdir(original)
            keys = json.loads(seen_path.read_text(encoding="utf-8"))["keys"]
            self.assertEqual(keys, [pending[0]["key"]])
            self.assertFalse(any(GATE_A in key for key in keys))

    def test_single_switch_restores_direct_and_audit_warnings(self):
        current = self.gate_failure()
        with patch.object(mail, "GATE_A_ALERTS_TEMPORARILY_SUSPENDED", False):
            self.assertEqual(audit.pending_alerts(current, set()), current)
            smtp = self.call_mail(MAIL_ENV | {"FAILURE_STAGE": "Gate A approval check"})
        smtp.return_value.__enter__.return_value.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
