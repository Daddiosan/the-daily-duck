import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.automation_chain_audit import (
    SCHEDULED_WORKFLOWS,
    WORKFLOWS,
    evaluate,
    live_inputs,
    pending_alerts,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
DATE = "2026-09-04"


def run(
    name,
    run_id,
    conclusion="success",
    attempt=1,
    event=None,
    created_at="2026-09-04T11:00:00Z",
):
    return {
        "id": run_id,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "run_attempt": attempt,
        "event": event
        or ("schedule" if name in SCHEDULED_WORKFLOWS else "workflow_dispatch"),
        "created_at": created_at,
        "html_url": f"https://example.test/runs/{run_id}",
    }


def healthy_runs():
    return {name: [run(name, index)] for index, name in enumerate(WORKFLOWS, 1)}


def healthy_states():
    return {
        "approved_story": {"issue_date": DATE, "approved_at": "2026-09-04T08:00:00Z"},
        "design_options": {"issue_date": DATE, "state": "WAITING_FINAL_SELECTION", "final_email_sent_at": "2026-09-04T08:10:00Z"},
        "design_selection_result": {"issue_date": DATE, "action": "READY_TO_PUBLISH", "checked_at": "2026-09-04T09:00:00Z"},
        "ready_to_publish": {"issue_date": DATE, "state": "READY_TO_PUBLISH", "checked_at": "2026-09-04T09:00:00Z"},
        "website_publish_result": {"issue_date": DATE, "action": "PUBLISHED", "published_at": "2026-09-04T09:10:00Z"},
        "x_publish_result": {"issue_date": DATE, "action": "X_POSTED", "x_posted_at": "2026-09-04T09:20:00Z"},
    }


class AuditTests(unittest.TestCase):
    def test_healthy_chain_has_no_false_positive(self):
        self.assertEqual(evaluate(healthy_runs(), healthy_states(), NOW), [])

    def test_each_downstream_workflow_failure(self):
        for name in list(WORKFLOWS)[2:]:
            with self.subTest(name=name):
                runs = healthy_runs()
                runs[name] = [run(name, 99, "failure")]
                self.assertIn(WORKFLOWS[name][1], {p.failure_stage for p in evaluate(runs, healthy_states(), NOW)})

    def test_downstream_state_timeouts(self):
        cases = (
            ("design_options", None, "Design Options state timeout"),
            ("ready_to_publish", None, "Design Selection state transition"),
            ("website_publish_result", None, "Website Publish state inconsistency"),
            ("x_publish_result", None, "X Publish final state inconsistency"),
        )
        for key, value, expected in cases:
            with self.subTest(key=key):
                states = healthy_states(); states[key] = value
                self.assertIn(expected, {p.failure_stage for p in evaluate(healthy_runs(), states, NOW)})

    def test_downstream_run_completion_timeout(self):
        name = "The Daily Duck - Design Options"
        runs = healthy_runs()
        stalled = run(name, 42)
        stalled.update(status="in_progress", conclusion=None, created_at="2026-09-04T10:00:00Z")
        runs[name] = [stalled]
        self.assertIn("Design Options completion timeout", {p.failure_stage for p in evaluate(runs, healthy_states(), NOW)})

    def test_recent_production_schedule_delays_are_not_stale(self):
        runs = healthy_runs()
        gate = "The Daily Duck - Gate A Approval Check"
        design = "The Daily Duck - Design Selection Check"
        runs[gate] = [run(gate, 36205052920, created_at="2026-09-26T00:29:42Z")]
        runs[design] = [run(design, 36204231949, created_at="2026-09-26T00:16:33Z")]
        now = datetime(2026, 9, 26, 1, 34, 57, tzinfo=timezone.utc)
        stages = {p.failure_stage for p in evaluate(runs, {}, now)}
        self.assertNotIn("Gate A Approval Check scheduled trigger", stages)
        self.assertNotIn("Design Selection Check scheduled trigger", stages)

    def test_poller_schedule_is_stale_after_bounded_scheduler_tolerance(self):
        name = "The Daily Duck - Gate A Approval Check"
        runs = healthy_runs()
        runs[name] = [run(name, 99, created_at="2026-09-04T03:59:59Z")]
        stages = {p.failure_stage for p in evaluate(runs, healthy_states(), NOW)}
        self.assertIn("Gate A Approval Check scheduled trigger", stages)

    def test_manual_run_does_not_mask_stale_schedule(self):
        name = "The Daily Duck - Design Selection Check"
        runs = healthy_runs()
        runs[name] = [
            run(name, 98, event="schedule", created_at="2026-09-04T03:00:00Z"),
            run(name, 99, event="workflow_dispatch", created_at="2026-09-04T11:59:00Z"),
        ]
        problems = evaluate(runs, healthy_states(), NOW)
        incident = next(p for p in problems if p.failure_stage == "Design Selection Check scheduled trigger")
        self.assertEqual(incident.run_id, 98)

    def test_daily_schedule_waits_for_scheduler_grace(self):
        name = "The Daily Duck Automation"
        runs = healthy_runs()
        runs[name] = [run(name, 99, created_at="2026-09-03T12:00:00Z")]
        before_deadline = datetime(2026, 9, 4, 1, 6, tzinfo=timezone.utc)
        after_deadline = datetime(2026, 9, 4, 1, 8, tzinfo=timezone.utc)
        before = {p.run_status for p in evaluate(runs, {}, before_deadline)}
        after = {p.run_status for p in evaluate(runs, {}, after_deadline)}
        self.assertNotIn("NO_RUN_TODAY", before)
        self.assertIn("NO_RUN_TODAY", after)

    @patch("scripts.automation_chain_audit.gh_json")
    def test_live_inputs_fetches_latest_scheduled_run_separately(self, gh_json):
        def response(endpoint):
            if "event=schedule" in endpoint:
                return {"workflow_runs": [{"id": 2, "event": "schedule"}]}
            return {"workflow_runs": [{"id": 1, "event": "workflow_dispatch"}]}

        gh_json.side_effect = response
        runs, _ = live_inputs("owner/repo", Path("missing"))
        for name in SCHEDULED_WORKFLOWS:
            self.assertEqual({run["id"] for run in runs[name]}, {1, 2})
        expected_calls = len(WORKFLOWS) + len(SCHEDULED_WORKFLOWS)
        self.assertEqual(gh_json.call_count, expected_calls)

    def test_2026_09_04_failed_attempt_then_successful_retry(self):
        name = "The Daily Duck - Design Options"
        runs = healthy_runs(); runs[name] = [run(name, 33813086005, "failure", 1)]
        self.assertIn("Design Options", {p.failure_stage for p in evaluate(runs, healthy_states(), NOW)})
        runs[name] = [run(name, 33813086005, "success", 2)]
        self.assertNotIn("Design Options", {p.failure_stage for p in evaluate(runs, healthy_states(), NOW)})

    def test_alert_key_is_run_attempt_stage_specific(self):
        name = "The Daily Duck - X Publish"
        runs = healthy_runs(); runs[name] = [run(name, 10, "failure", 1)]
        incident = next(p for p in evaluate(runs, healthy_states(), NOW) if p.failure_stage == "X Publish")
        self.assertEqual(incident.key, "The Daily Duck - X Publish:10:1:X Publish")
        runs[name] = [run(name, 10, "failure", 2)]
        retry = next(p for p in evaluate(runs, healthy_states(), NOW) if p.failure_stage == "X Publish")
        self.assertNotEqual(incident.key, retry.key)
        self.assertEqual(pending_alerts([incident], {incident.key}), [])
        self.assertEqual(pending_alerts([retry], {incident.key}), [retry])


if __name__ == "__main__":
    unittest.main()
