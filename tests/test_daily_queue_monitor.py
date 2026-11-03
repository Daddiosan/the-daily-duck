import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scripts import automation_chain_audit as audit
from scripts import daily_generation_guard as guard

WORKFLOW = 'The Daily Duck Automation'
NOW = datetime(2026, 11, 2, 22, 15, tzinfo=timezone.utc)  # 07:15 JST


def run(status='queued', age=18, conclusion=None):
    return {'name': WORKFLOW, 'id': 1, 'event': 'schedule', 'status': status,
            'conclusion': conclusion, 'created_at': (NOW - timedelta(minutes=age)).isoformat()}


def daily_problems(item=None, states=None, now=NOW):
    runs = {WORKFLOW: [item]} if item else {}
    return [p for p in audit.evaluate(runs, states or {}, now)
            if p.failure_stage.startswith('Daily Automation')]


class DailyQueueMonitorTests(unittest.TestCase):
    def test_no_run_after_deadline(self):
        self.assertEqual(daily_problems()[0].run_status, 'NO_RUN_TODAY')

    def test_queued_before_morning_deadline(self):
        self.assertEqual(daily_problems(run(), now=NOW - timedelta(seconds=1)), [])

    def test_queued_within_run_grace(self):
        self.assertEqual(daily_problems(run(age=9)), [])

    def test_queued_at_ten_minutes_without_attempt(self):
        self.assertEqual(daily_problems(run(age=10))[0].run_status, 'QUEUED')

    def test_queued_overdue_without_attempt(self):
        self.assertEqual(daily_problems(run())[0].run_status, 'QUEUED')

    def test_in_progress_within_run_grace(self):
        self.assertEqual(daily_problems(run('in_progress', age=9)), [])

    def test_in_progress_overdue_without_attempt(self):
        self.assertEqual(daily_problems(run('in_progress'))[0].run_status, 'IN_PROGRESS')

    def test_verified_delivery_suppresses_pending_alert(self):
        states = {'daily_delivery': {'issue_date': '2026-11-03', 'sent_verified': True}}
        self.assertEqual(daily_problems(run(), states), [])

    def test_unverified_receipt_does_not_suppress(self):
        states = {'daily_delivery': {'issue_date': '2026-11-03', 'sent_verified': False}}
        self.assertEqual(daily_problems(run(), states)[0].run_status, 'QUEUED')

    def test_unknown_smtp_outcome_retains_stop_and_alert(self):
        states = {'daily_attempt': {'issue_date': '2026-11-03',
                                   'created_at': (NOW - timedelta(minutes=10)).isoformat()}}
        self.assertEqual(daily_problems(run('completed', conclusion='success'), states)[0].run_status,
                         'MAIL_NOT_VERIFIED')
        with patch.dict(os.environ, {'GITHUB_REF': 'refs/heads/main', 'GITHUB_RUN_ATTEMPT': '1',
                                     'GITHUB_REPOSITORY': 'review/repository'}), \
             patch.object(guard, 'find_sent', return_value=None), \
             patch.object(guard, 'prior_attempt', return_value=True), \
             patch.object(guard.Path, 'read_text', return_value='[]'):
            result = guard.preflight('2026-11-03')
        self.assertFalse(result['should_run'])
        self.assertEqual(result['status'], 'HOLD_PRIOR_ATTEMPT_REQUIRES_REVIEW')

    def test_completed_unknown_conclusion_is_a_failure(self):
        self.assertTrue(any(p.failure_stage == 'Daily Automation'
                            for p in daily_problems(run('completed'))))

    def test_published_november_2_and_3_cannot_generate_or_send(self):
        archive = json.dumps([{'date': '2026-11-02'}, {'date': '2026-11-03'}])
        with patch.dict(os.environ, {'GITHUB_REF': 'refs/heads/main', 'GITHUB_RUN_ATTEMPT': '1'}), \
             patch.object(guard.Path, 'read_text', return_value=archive), \
             patch.object(guard, 'find_sent') as sent, patch.object(guard, 'prior_attempt') as attempt:
            for day in ('2026-11-02', '2026-11-03'):
                result = guard.preflight(day)
                self.assertFalse(result['should_run'])
                self.assertEqual(result['status'], 'HOLD_ALREADY_PUBLISHED')
        sent.assert_not_called()
        attempt.assert_not_called()


if __name__ == '__main__':
    unittest.main()
