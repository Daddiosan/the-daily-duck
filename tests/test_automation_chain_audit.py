import unittest
from datetime import datetime, timezone

from scripts.automation_chain_audit import WORKFLOWS, evaluate, pending_alerts

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
DATE = "2026-09-04"


def run(name, run_id, conclusion="success", attempt=1):
    return {"id": run_id, "name": name, "status": "completed", "conclusion": conclusion, "run_attempt": attempt, "created_at": "2026-09-04T11:00:00Z", "html_url": f"https://example.test/runs/{run_id}"}


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
