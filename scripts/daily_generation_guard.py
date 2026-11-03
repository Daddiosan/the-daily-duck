"""Date-bound Daily Duck generation/mail guard. No paid API, Web or X calls."""
from __future__ import annotations
import argparse,email,imaplib,json,os,re,subprocess,sys
from datetime import date,datetime,timedelta,timezone
from email.header import decode_header,make_header
from email.utils import parseaddr
from pathlib import Path

JST=timezone(timedelta(hours=9))
MARKER_PREFIX='daily-duck-attempt-'
DELIVERY_PREFIX='daily-duck-delivered-'
WORKFLOW_NAME='The Daily Duck Automation'

def issue_date(target: str='', now: datetime|None=None)->str:
    if target:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',target):
            raise ValueError('target_date must be YYYY-MM-DD')
        return date.fromisoformat(target).isoformat()
    clock=now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError('A timezone-aware clock is required')
    return clock.astimezone(JST).date().isoformat()

def gh_json(endpoint: str)->dict:
    result=subprocess.run(['gh','api',endpoint],capture_output=True,text=True)
    if result.returncode:
        raise RuntimeError('GitHub preflight could not be verified')
    data=json.loads(result.stdout)
    if not isinstance(data,dict):
        raise RuntimeError('GitHub response is not an object')
    return data

def prior_attempt(issue: str, repository: str)->bool:
    data=gh_json(f'repos/{repository}/actions/artifacts?name={MARKER_PREFIX}{issue}&per_page=100')
    items=data.get('artifacts')
    if not isinstance(items,list) or data.get('total_count',0)>len(items):
        raise RuntimeError('Attempt history is incomplete')
    return any(row.get('name')==MARKER_PREFIX+issue and
               row.get('workflow_run',{}).get('head_branch')=='main' for row in items)

def matching_sent(message, issue: str, sender: str, digest: str|None=None)->bool:
    subject=str(make_header(decode_header(message.get('Subject',''))))
    if parseaddr(message.get('From',''))[1].casefold()!=sender.casefold():
        return False
    prefix=f"The Daily Duck — Choose Today's Story — {issue}"
    if not ''.join(subject.split()).startswith(''.join((prefix+' — Approval Token ').split())):
        return False
    match=re.search(r'Approval\s+Token\s+([A-Za-z0-9_-]{32})\s*$',subject)
    if not match:
        return False
    if digest is not None:
        try:
            from scripts.approval_token import validate_approval_token
        except ImportError:
            from approval_token import validate_approval_token
        return validate_approval_token(stored_digest=digest,stage='GATE_A',
                                       issue_date=issue,batch=None,token=match.group(1))
    return True

def find_sent(issue: str, digest: str|None=None)->str|None:
    sender=os.environ.get('GMAIL_ADDRESS','').strip()
    password=os.environ.get('GMAIL_APP_PASSWORD','')
    if not sender or not password:
        raise RuntimeError('Gmail credentials are unavailable')
    with imaplib.IMAP4_SSL('imap.gmail.com',993,timeout=30) as box:
        box.login(sender,password)
        status,rows=box.list()
        if status!='OK' or not rows:
            raise RuntimeError('Cannot inspect Gmail folders')
        sent=next((row.decode().rsplit(' "',1)[-1].strip('"')
                   for row in rows if isinstance(row,bytes) and b'\\Sent' in row),None)
        if sent is None or box.select('"'+sent+'"',readonly=True)[0]!='OK':
            raise RuntimeError('Cannot inspect Sent folder')
        status,found=box.search(None,'SUBJECT','"'+issue+'"')
        if status!='OK' or not found:
            raise RuntimeError('Gmail Sent search is unverified')
        for ident in reversed(found[0].split()):
            status,data=box.fetch(ident,'(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)])')
            if status!='OK' or not data or not isinstance(data[0],tuple):
                raise RuntimeError('Cannot inspect sent message headers')
            msg=email.message_from_bytes(data[0][1])
            if matching_sent(msg,issue,sender,digest):
                return str(msg.get('Message-ID') or 'GMAIL_SENT_VERIFIED')
    return None

def preflight(target: str, now: datetime|None=None)->dict:
    issue=issue_date(target,now)
    if os.environ.get('GITHUB_REF')!='refs/heads/main':
        return dict(issue_date=issue,should_run=False,status='HOLD_UNREVIEWED_REF')
    if os.environ.get('GITHUB_RUN_ATTEMPT','1')!='1':
        return dict(issue_date=issue,should_run=False,status='HOLD_RERUN_REQUIRES_REVIEW')
    archive=json.loads(Path('data/archive.json').read_text(encoding='utf-8'))
    if any(row.get('date')==issue for row in archive):
        return dict(issue_date=issue,should_run=False,status='HOLD_ALREADY_PUBLISHED')
    sent_id=find_sent(issue)
    if sent_id:
        record_delivery(issue,sent_id)
        return dict(issue_date=issue,should_run=False,status='ALREADY_SENT')
    repository=os.environ.get('GITHUB_REPOSITORY','').strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository):
        raise RuntimeError('Repository identity unavailable')
    if prior_attempt(issue,repository):
        return dict(issue_date=issue,should_run=False,status='HOLD_PRIOR_ATTEMPT_REQUIRES_REVIEW')
    return dict(issue_date=issue,should_run=True,status='READY')

def assert_not_sent(issue: str)->None:
    if find_sent(issue):
        raise RuntimeError('Issue mail already exists; refusing a second SMTP attempt')

def record_delivery(issue: str, message_id: str)->dict:
    receipt=dict(issue_date=issue,sent_verified=True,message_id=message_id,
                 run_id=os.environ.get('GITHUB_RUN_ID'),commit_sha=os.environ.get('GITHUB_SHA'),
                 verified_at=datetime.now(timezone.utc).isoformat())
    Path('daily_delivery.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
    return receipt

def verify_delivery(issue: str, digest: str)->dict:
    message_id=find_sent(issue,digest)
    if not message_id:
        raise RuntimeError('SMTP outcome not verified in Sent; do not retry')
    return record_delivery(issue,message_id)

def main()->int:
    parser=argparse.ArgumentParser()
    parser.add_argument('--target-date',default=os.environ.get('DAILY_DUCK_TARGET_DATE',''))
    parser.add_argument('--github-output',type=Path)
    args=parser.parse_args()
    result=preflight(args.target_date)
    if args.github_output:
        with args.github_output.open('a',encoding='utf-8') as output:
            for key,value in result.items():
                output.write(f'{key}={str(value).lower() if isinstance(value,bool) else value}\n')
    if result['should_run']:
        path=Path('daily_attempt/attempt.json')
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(dict(result,run_id=os.environ.get('GITHUB_RUN_ID'),
                        commit_sha=os.environ.get('GITHUB_SHA'),
                        created_at=datetime.now(timezone.utc).isoformat()),indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result))
    return 0

if __name__=='__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('GENERATION_GUARD_UNVERIFIED:',type(exc).__name__,file=sys.stderr)
        sys.exit(1)
