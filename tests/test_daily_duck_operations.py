from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from scripts import automation_chain_audit as audit
from scripts import daily_duck_operations as operations
from scripts import send_workflow_failure_email as mail


NOW = datetime.fromisoformat("2026-11-23T01:20:00+09:00")
INITIAL = {
    "version": 1,
    "first_seen": {},
    "paused_at": None,
    "send_date": None,
    "send_count": 0,
    "last_send_at": None,
}


class DailyDuckOperationsTests(unittest.TestCase):
    def test_third_hour_and_jst_daily_cap(self):
        state = INITIAL
        for slot in range(8):
            now = NOW + timedelta(hours=3 * slot)
            state, send, _ = operations.advance(state, {"failed"}, now)
            self.assertEqual(send, slot < 6)
            repeated, send, _ = operations.advance(state, {"failed"}, now)
            self.assertEqual(repeated, state)
            self.assertFalse(send)
        tomorrow = NOW + timedelta(days=1)
        state, send, _ = operations.advance(state, {"failed"}, tomorrow)
        self.assertTrue(send)
        self.assertEqual(state["send_count"], 1)
        self.assertEqual(state["send_date"], "2026-11-24")

    def test_three_hour_interval_survives_jst_midnight(self):
        now = NOW.replace(hour=23)
        state, _, _ = operations.advance(INITIAL, {"failed"}, now)
        _, send, _ = operations.advance(state, {"failed"}, now + timedelta(hours=2))
        self.assertFalse(send)
        _, send, _ = operations.advance(state, {"failed"}, now + timedelta(hours=3))
        self.assertTrue(send)

    def test_pause_at_twenty_days_and_respond(self):
        state, _, _ = operations.advance(INITIAL, {"failed"}, NOW)
        _, _, paused = operations.advance(
            state, {"failed"}, NOW + timedelta(days=20, seconds=-1)
        )
        self.assertFalse(paused)
        state, _, paused = operations.advance(state, {"failed"}, NOW + timedelta(days=20))
        self.assertTrue(paused)
        paused_at = state["paused_at"]
        state, _, paused = operations.advance(state, set(), NOW + timedelta(days=21))
        self.assertFalse(paused)
        self.assertEqual(state["paused_at"], paused_at)
        response = operations.acknowledge(state)
        self.assertIsNone(response["paused_at"])
        self.assertEqual(response["last_send_at"], state["last_send_at"])

    def test_new_or_recovered_failure_does_not_inherit_age(self):
        state, _, _ = operations.advance(INITIAL, {"old"}, NOW)
        future = NOW + timedelta(days=20)
        for keys in (set(), {"unrelated"}):
            updated, send, paused = operations.advance(state, keys, future)
            self.assertEqual(send, bool(keys))
            self.assertFalse(paused)
            self.assertNotIn("old", updated["first_seen"])

    def test_daily_issue_survives_rollover_until_delivery_and_gate_a_stays_muted(self):
        workflow = "The Daily Duck Automation"
        state, _, _ = operations.advance(INITIAL, {workflow}, NOW)
        gate_a = audit.problem("Gate A Approval Check", "2026-11-24", NOW, {
            "name": "The Daily Duck - Gate A Approval Check"
        }, "completed/failure", "Check approval")
        tomorrow = NOW + timedelta(days=1)
        alerts, keys = audit.reminder_problems([gate_a], state, {}, tomorrow)
        self.assertEqual(keys, {workflow})
        self.assertEqual(alerts[0].issue_date, "2026-11-24")
        self.assertEqual(alerts[0].run_status, "MAIL_NOT_VERIFIED")
        alerts, keys = audit.reminder_problems(
            [gate_a], state, {"daily_delivery": {"sent_verified": True}}, tomorrow
        )
        self.assertEqual((alerts, keys), ([], set()))

    def test_operations_state_uses_explicit_branch_and_blob_sha(self):
        with patch.object(operations, "github_api") as api:
            operations.write_state("owner/repo", INITIAL, "current-blob")
            endpoint, body = api.call_args.args
            self.assertEqual(endpoint, "repos/owner/repo/contents/operations.json")
            self.assertEqual(body["branch"], operations.STATE_BRANCH)
            self.assertEqual(body["sha"], "current-blob")
            api.return_value = {"content": body["content"], "sha": "next-blob"}
            self.assertEqual(operations.read_state("owner/repo"), (INITIAL, "next-blob"))
            self.assertTrue(api.call_args.args[0].endswith(f"?ref={operations.STATE_BRANCH}"))

    def test_workflow_failure_mail_uses_monitor_only(self):
        with patch.dict(os.environ, {"GITHUB_WORKFLOW": "The Daily Duck Automation"}, clear=True):
            with patch.object(mail.smtplib, "SMTP_SSL") as smtp:
                self.assertEqual(mail.main(), 0)
                smtp.assert_not_called()

    def test_generation_guard_outputs_allowed_or_paused(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "output"
            with patch.dict(os.environ, {
                "GITHUB_OUTPUT": str(output), "GITHUB_REPOSITORY": "owner/repo",
            }), patch.object(operations.sys, "argv", ["ops", "check-generation"]):
                for paused_at, allowed in ((None, "true"), (NOW.isoformat(), "false")):
                    with patch.object(operations, "read_state", return_value=(
                        INITIAL | {"paused_at": paused_at}, "current-blob"
                    )):
                        operations.main()
                    self.assertEqual(output.read_text().splitlines()[-1], f"allowed={allowed}")
