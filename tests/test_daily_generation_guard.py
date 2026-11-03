import email
import json
import os
import tempfile
import unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch,Mock
from scripts import daily_generation_guard as guard
from scripts.approval_token import approval_token_digest
from scripts import automation_chain_audit as audit

class DateTests(unittest.TestCase):
    def test_jst_midnight(self):
        self.assertEqual(guard.issue_date(now=datetime(2026,11,2,14,59,59,tzinfo=timezone.utc)),'2026-11-02')
        self.assertEqual(guard.issue_date(now=datetime(2026,11,2,15,0,tzinfo=timezone.utc)),'2026-11-03')
    def test_manual_date_does_not_roll_at_midnight(self):
        self.assertEqual(guard.issue_date('2026-11-03',datetime(2026,11,4,tzinfo=timezone.utc)),'2026-11-03')
    def test_0657_and_0700_are_same_issue(self):
        for minute in (57,58,59):
            self.assertEqual(guard.issue_date(now=datetime(2026,11,2,21,minute,tzinfo=timezone.utc)),'2026-11-03')
        self.assertEqual(guard.issue_date(now=datetime(2026,11,2,22,tzinfo=timezone.utc)),'2026-11-03')
    def test_strict_and_leap_dates(self):
        self.assertEqual(guard.issue_date('2028-02-29'),'2028-02-29')
        for invalid in ('2026-02-29','2026-11-31','2026-1-3','2026-11-03\n','today'):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                guard.issue_date(invalid)
    def test_naive_clock_rejected(self):
        with self.assertRaises(ValueError): guard.issue_date(now=datetime(2026,11,3))

class GuardTests(unittest.TestCase):
    def setUp(self):
        self.original=Path.cwd()
        self.temp=tempfile.TemporaryDirectory()
        os.chdir(self.temp.name)
        Path('data').mkdir()
        Path('data/archive.json').write_text('[]',encoding='utf-8')
        self.env=patch.dict(os.environ,{'GITHUB_REPOSITORY':'owner/repo','GITHUB_RUN_ATTEMPT':'1','GITHUB_REF':'refs/heads/main'},clear=True)
        self.env.start()
    def tearDown(self):
        self.env.stop();os.chdir(self.original);self.temp.cleanup()
    def test_ready_has_no_side_effects(self):
        with patch.object(guard,'find_sent',return_value=None),patch.object(guard,'prior_attempt',return_value=False):
            result=guard.preflight('2026-11-03')
        self.assertTrue(result['should_run'])
        self.assertFalse(Path('daily_attempt').exists())
    def test_published_november_2_refused_before_network(self):
        Path('data/archive.json').write_text('[{"date":"2026-11-02"}]',encoding='utf-8')
        with patch.object(guard,'find_sent') as sent,patch.object(guard,'prior_attempt') as prior:
            result=guard.preflight('2026-11-02')
        self.assertEqual(result['status'],'HOLD_ALREADY_PUBLISHED')
        sent.assert_not_called();prior.assert_not_called()
    def test_delivered_issue_skips_before_models_or_attempt_claim(self):
        with patch.object(guard,'find_sent',return_value='sent-id'),patch.object(guard,'prior_attempt') as prior:
            result=guard.preflight('2026-11-03')
        self.assertEqual(result['status'],'ALREADY_SENT')
        self.assertFalse(result['should_run']);prior.assert_not_called()
        self.assertTrue(json.loads(Path('daily_delivery.json').read_text(encoding='utf-8'))['sent_verified'])
    def test_unknown_old_attempt_cannot_be_retried(self):
        with patch.object(guard,'find_sent',return_value=None),patch.object(guard,'prior_attempt',return_value=True):
            result=guard.preflight('2026-11-03')
        self.assertEqual(result['status'],'HOLD_PRIOR_ATTEMPT_REQUIRES_REVIEW')
        self.assertFalse(result['should_run'])
    def test_workflow_rerun_refused_before_network(self):
        with patch.dict(os.environ,{'GITHUB_RUN_ATTEMPT':'2'}),patch.object(guard,'find_sent') as sent:
            result=guard.preflight('2026-11-03')
        self.assertFalse(result['should_run']);sent.assert_not_called()
    def test_isolated_branch_cannot_send_before_main_human_gate(self):
        with patch.dict(os.environ,{'GITHUB_REF':'refs/heads/fix/candidate'}),patch.object(guard,'find_sent') as sent:
            result=guard.preflight('2026-11-03')
        self.assertEqual(result['status'],'HOLD_UNREVIEWED_REF');sent.assert_not_called()
    def test_unverifiable_sent_aborts(self):
        with patch.object(guard,'find_sent',side_effect=RuntimeError):
            with self.assertRaises(RuntimeError): guard.preflight('2026-11-03')
    def test_attempt_markers_are_main_scoped_and_not_deleted(self):
        marker={'name':'daily-duck-attempt-2026-11-03','expired':True,'workflow_run':{'head_branch':'main'}}
        with patch.object(guard,'gh_json',return_value={'total_count':1,'artifacts':[marker]}):
            self.assertTrue(guard.prior_attempt('2026-11-03','owner/repo'))
        marker['workflow_run']['head_branch']='isolated-review'
        with patch.object(guard,'gh_json',return_value={'total_count':1,'artifacts':[marker]}):
            self.assertFalse(guard.prior_attempt('2026-11-03','owner/repo'))
    def test_partial_marker_history_refused(self):
        with patch.object(guard,'gh_json',return_value={'total_count':101,'artifacts':[]}):
            with self.assertRaises(RuntimeError): guard.prior_attempt('2026-11-03','owner/repo')
    def test_pre_smtp_duplicate_recheck_blocks(self):
        with patch.object(guard,'find_sent',return_value='sent-id'):
            with self.assertRaises(RuntimeError): guard.assert_not_sent('2026-11-03')
    def test_unverified_smtp_receipt_requires_review(self):
        with patch.object(guard,'find_sent',return_value=None):
            with self.assertRaises(RuntimeError): guard.verify_delivery('2026-11-03','0'*64)
        self.assertFalse(Path('daily_delivery.json').exists())
    def test_verified_delivery_receipt_contains_no_token(self):
        with patch.object(guard,'find_sent',return_value='sent-id') as sent:
            result=guard.verify_delivery('2026-11-03','0'*64)
        sent.assert_called_once_with('2026-11-03','0'*64)
        self.assertTrue(result['sent_verified'])
        self.assertNotIn('token',Path('daily_delivery.json').read_text(encoding='utf-8'))
    def test_smtp_is_not_opened_when_duplicate_found(self):
        from scripts import send_email
        with patch.dict(os.environ,{'DAILY_DUCK_DELIVERY_GUARD':'1','DAILY_DUCK_TARGET_DATE':'2026-11-03',
                  'GMAIL_ADDRESS':'sender@example.invalid','GMAIL_APP_PASSWORD':'test-only-placeholder','EMAIL_TO':'reader@example.invalid'}), \
             patch.object(guard,'find_sent',return_value='sent-id'),patch.object(send_email.smtplib,'SMTP_SSL') as smtp:
            with self.assertRaises(RuntimeError): send_email.send_email('test subject','test body')
        smtp.assert_not_called()

class HeaderTests(unittest.TestCase):
    def message(self,day='2026-11-03',sender='sender@example.invalid'):
        msg=email.message.EmailMessage()
        msg['From']=sender
        msg['Subject']="The Daily Duck — Choose Today's Story — "+day+" — Approval Token "+'A'*32
        return msg
    def test_date_sender_and_bound_token(self):
        digest=approval_token_digest(stage='GATE_A',issue_date='2026-11-03',batch=None,token='A'*32)
        self.assertTrue(guard.matching_sent(self.message(),'2026-11-03','sender@example.invalid',digest))
        self.assertFalse(guard.matching_sent(self.message(),'2026-11-02','sender@example.invalid'))
        self.assertFalse(guard.matching_sent(self.message(),'2026-11-03','other@example.invalid'))
        self.assertFalse(guard.matching_sent(self.message(),'2026-11-03','sender@example.invalid','0'*64))
    def test_failed_verification_does_not_send_mail(self):
        with patch.object(guard,'find_sent',return_value=None):
            with self.assertRaises(RuntimeError): guard.verify_delivery('2026-11-03','0'*64)

class MonitorTests(unittest.TestCase):
    def evaluate(self,delivery=None,at=None):
        old={'name':'The Daily Duck Automation','id':1,'event':'schedule','status':'completed',
             'conclusion':'success','run_attempt':1,'created_at':'2026-11-02T01:25:44Z'}
        now=at or datetime(2026,11,2,22,15,tzinfo=timezone.utc)
        states={'approved_story':{'issue_date':'2026-11-01'},'daily_delivery':delivery}
        return [p for p in audit.evaluate({'The Daily Duck Automation':[old]},states,now)
                if p.failure_stage=='Daily Automation scheduled trigger']
    def test_no_alert_before_0715_and_current_issue_after(self):
        self.assertEqual(self.evaluate(at=datetime(2026,11,2,22,14,59,tzinfo=timezone.utc)),[])
        problem=self.evaluate()[0]
        self.assertEqual(problem.issue_date,'2026-11-03')
    def test_external_wake_verified_delivery_suppresses_missing_cron_alert(self):
        self.assertEqual(self.evaluate({'issue_date':'2026-11-03','sent_verified':True}),[])
    def test_old_manual_issue_does_not_mask_missing_today(self):
        self.assertTrue(self.evaluate({'issue_date':'2026-11-02','sent_verified':True}))
    def test_missing_each_day_has_distinct_alert_key(self):
        today=self.evaluate()[0]
        tomorrow=self.evaluate(at=datetime(2026,11,3,22,15,tzinfo=timezone.utc))[0]
        self.assertNotEqual(today.key,tomorrow.key)
    def test_attempt_without_delivery_alerts_at_ten_minutes(self):
        states={'daily_attempt':{'issue_date':'2026-11-03','created_at':'2026-11-02T22:05:00Z'}}
        for second,expected in ((-1,False),(0,True)):
            now=datetime(2026,11,2,22,15,tzinfo=timezone.utc)
            from datetime import timedelta
            problems=audit.evaluate({},states,now+timedelta(seconds=second))
            self.assertEqual(any(p.failure_stage=='Daily Automation candidate delivery' for p in problems),expected)
        states['daily_delivery']={'issue_date':'2026-11-03','sent_verified':True}
        self.assertFalse(any(p.failure_stage=='Daily Automation candidate delivery'
                             for p in audit.evaluate({},states,now)))
    def test_live_receipts_require_daily_workflow_main_and_nonexpired_artifact(self):
        today=datetime(2026,11,2,22,15,tzinfo=timezone.utc)
        for branch,run_id,expired,accepted in (('main',42,False,True),('fix/candidate',42,False,False),
                                               ('main',43,False,False),('main',42,True,False)):
            with self.subTest(branch=branch,run_id=run_id,expired=expired):
                def response(endpoint):
                    if '/artifacts?' in endpoint:
                        name=endpoint.split('name=',1)[1].split('&',1)[0]
                        return {'artifacts':[{'name':name,'expired':expired,'created_at':'2026-11-02T21:57:00Z',
                                              'workflow_run':{'id':run_id,'head_branch':branch}}]}
                    return {'workflow_runs':[{'id':42}]} if '/daily-duck.yml/' in endpoint else {'workflow_runs':[]}
                with patch.object(audit,'gh_json',side_effect=response):
                    _,states=audit.live_inputs('owner/repo',Path('missing'),today)
                self.assertEqual(states['daily_delivery'] is not None,accepted)
                self.assertEqual(states['daily_attempt'] is not None,branch=='main' and run_id==42)

class WorkflowTests(unittest.TestCase):
    def test_paid_stages_guarded_and_attempt_precedes_them(self):
        import yaml
        root=Path(__file__).resolve().parents[1]
        data=yaml.safe_load((root/'.github/workflows/daily-duck.yml').read_text(encoding='utf-8'))
        self.assertEqual(data['concurrency'],{'group':'daily-duck-news-generation','cancel-in-progress':False})
        events=data.get('on',data.get(True))
        self.assertEqual(events['schedule'][0]['cron'],'57 21 * * *')
        steps=data['jobs']['daily-duck']['steps']
        names=[s['name'] for s in steps]
        marker=names.index('Persist a dated attempt before any paid model or SMTP call')
        for name in ('Collect Daily Duck news','Rank Daily Duck news','Send Daily Duck email'):
            index=names.index(name)
            self.assertLess(marker,index)
            self.assertEqual(steps[index]['if'],"steps.delivery_gate.outputs.should_run == 'true'")
        self.assertEqual(data['permissions']['contents'],'read')
        self.assertNotIn('publish_website','\n'.join(str(s.get('run','')) for s in steps))
        self.assertNotIn('publish_x','\n'.join(str(s.get('run','')) for s in steps))

if __name__=='__main__': unittest.main()
