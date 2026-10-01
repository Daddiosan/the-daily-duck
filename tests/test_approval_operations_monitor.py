from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from scripts.approval_operations_monitor import evaluate_health


NOW = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def healthy_snapshot():
    return {
        "relay_5xx_count": 0,
        "oauth_canary_status": "SUCCESS",
        "pubsub_backlog": 0,
        "oldest_unacked_seconds": 0,
        "watch_renewal_status": "SUCCESS",
        "watch_expiration_ms": int((NOW + timedelta(days=6)).timestamp() * 1000),
        "last_relay_success_at": (NOW - timedelta(minutes=5)).isoformat(),
        "gate_state": "APPROVED_STORY",
        "design_state": "DESIGN_SELECTED_READY_TO_PUBLISH",
        "last_gate_checker_run_at": (NOW - timedelta(hours=4)).isoformat(),
        "last_design_checker_run_at": (NOW - timedelta(hours=4)).isoformat(),
    }


class ApprovalOperationsMonitorTests(unittest.TestCase):
    def codes(self, snapshot):
        return {item.code for item in evaluate_health(snapshot, now=NOW)}

    def test_healthy(self):
        self.assertEqual(self.codes(healthy_snapshot()), set())

    def test_relay_5xx(self):
        snapshot = healthy_snapshot()
        snapshot["relay_5xx_count"] = 1
        self.assertIn("RELAY_5XX", self.codes(snapshot))

    def test_oauth_failure(self):
        snapshot = healthy_snapshot()
        snapshot["oauth_canary_status"] = "AUTH_FAILURE"
        self.assertIn("OAUTH_CANARY_FAILURE", self.codes(snapshot))

    def test_backlog_threshold(self):
        snapshot = healthy_snapshot()
        snapshot["pubsub_backlog"] = 6
        self.assertIn("PUBSUB_BACKLOG_HIGH", self.codes(snapshot))

    def test_oldest_unacked_threshold(self):
        snapshot = healthy_snapshot()
        snapshot["oldest_unacked_seconds"] = 901
        self.assertIn("PUBSUB_OLDEST_UNACKED_HIGH", self.codes(snapshot))

    def test_watch_expiration_warning(self):
        snapshot = healthy_snapshot()
        snapshot["watch_expiration_ms"] = int(
            (NOW + timedelta(hours=47)).timestamp() * 1000
        )
        self.assertIn("WATCH_EXPIRATION_APPROACHING", self.codes(snapshot))

    def test_relay_success_absence_only_matters_with_backlog(self):
        snapshot = healthy_snapshot()
        snapshot["last_relay_success_at"] = (NOW - timedelta(hours=2)).isoformat()
        self.assertNotIn("RELAY_SUCCESS_ABSENT_WITH_BACKLOG", self.codes(snapshot))
        snapshot["pubsub_backlog"] = 1
        self.assertIn("RELAY_SUCCESS_ABSENT_WITH_BACKLOG", self.codes(snapshot))

    def test_pending_gate_a_checker_absence(self):
        snapshot = healthy_snapshot()
        snapshot["gate_state"] = "WAITING_STORY_SELECTION"
        snapshot["last_gate_checker_run_at"] = (
            NOW - timedelta(minutes=31)
        ).isoformat()
        self.assertIn("GATE_A_CHECKER_ABSENT_WHILE_PENDING", self.codes(snapshot))

    def test_pending_design_checker_absence(self):
        snapshot = healthy_snapshot()
        snapshot["design_state"] = "WAITING_FINAL_SELECTION"
        snapshot["last_design_checker_run_at"] = (
            NOW - timedelta(minutes=31)
        ).isoformat()
        self.assertIn("DESIGN_CHECKER_ABSENT_WHILE_PENDING", self.codes(snapshot))

    def test_stale_workflow_is_not_alerted_when_stage_is_terminal(self):
        snapshot = healthy_snapshot()
        snapshot["last_gate_checker_run_at"] = (
            NOW - timedelta(days=1)
        ).isoformat()
        snapshot["last_design_checker_run_at"] = (
            NOW - timedelta(days=1)
        ).isoformat()
        self.assertNotIn("GATE_A_CHECKER_ABSENT_WHILE_PENDING", self.codes(snapshot))
        self.assertNotIn("DESIGN_CHECKER_ABSENT_WHILE_PENDING", self.codes(snapshot))


if __name__ == "__main__":
    unittest.main()
