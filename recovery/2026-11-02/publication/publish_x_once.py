"""One-date X publication adapter; no image/model generation and no automatic retry."""
import hashlib,json,os,shutil,sys
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(HERE))
from scripts import publish_x
import verify_web

DATE='2026-11-02'
ARTICLE=verify_web.URL
SITE=Path(os.environ.get('PUBLICATION_SITE',str(HERE/'offline-site'))).resolve()
STATE=SITE/'automation_state/x_backfill_2026-11-02_result.json'
CARD=SITE/'automation_images/x_backfill/2026-11-02-x-card.png'
REL_CARD='automation_images/x_backfill/2026-11-02-x-card.png'
PACKAGE=HERE/'package'
def require(ok,message):
    if not ok: raise RuntimeError(message)
def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))
def save(state):
    STATE.parent.mkdir(parents=True,exist_ok=True)
    STATE.write_text(json.dumps(state,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def guard():
    expected={'GITHUB_REF':'refs/heads/recovery/2026-11-02','GITHUB_ACTOR':'Daddiosan',
              'GITHUB_TRIGGERING_ACTOR':'Daddiosan','GITHUB_RUN_ATTEMPT':'1',
              'PUBLICATION_CONFIRMATION':'PUBLISH-X-2026-11-02-IMAGE1-TITLE1'}
    require(all(os.environ.get(k)==v for k,v in expected.items()),'Publication approval mismatch')
def package():
    ready=load(PACKAGE/'automation_state/ready_to_publish.json')
    manifest=load(HERE/'package_manifest.json')
    require(ready['issue_date']==DATE and ready['selected_image_number']==ready['selected_title_number']==1,'Wrong selection')
    require(ready['selected_title']=='A CHILL DOWN YOUR FEATHERS','Wrong title')
    require(ready['gate_a_approved_story']['selected_story_number']==3,'Wrong story')
    require(hashlib.sha256((PACKAGE/ready['canonical_image_path']).read_bytes()).hexdigest()==manifest['hero_sha256'],'Hero changed')
    require(hashlib.sha256((PACKAGE/ready['canonical_x_image_path']).read_bytes()).hexdigest()==manifest['x_card_sha256'],'Card changed')
    return ready,manifest
def target(value):
    if not isinstance(value,str): return False
    try:
        p=urlsplit(value)
        return p.hostname in ('www.thedailyduck.ai','thedailyduck.ai') and p.path.rstrip('/')=='/ducks/2026-11-02'
    except ValueError: return False
def links(row):
    values=[row.get('text','')]
    for item in row.get('entities',{}).get('urls',[]):
        values += [item.get(k,'') for k in ('url','expanded_url','unwound_url')]
    return any(target(value) or ARTICLE in value for value in values)
def get(url,auth,params=None):
    response=publish_x.requests.get(url,auth=auth,params=params,timeout=30)
    require(response.status_code==200,'X read failed; no retry')
    data=response.json()
    require(isinstance(data,dict) and not data.get('errors'),'X read incomplete')
    return data
def charge(state,operation,amount):
    total=0.129595+sum(r['reserve_usd'] for r in state['api_operations'])+amount
    require(total<=0.39,'USD0.39 overall cost hold')
    state['api_operations'].append({'operation':operation,'reserve_usd':amount})
    save(state)
def prepare():
    guard()
    require(not STATE.exists(),'Existing X state; inspect instead of overwriting')
    ready,manifest=package()
    live=verify_web.verify()
    require(live['verified'],'Website not verified')
    state={'issue_date':DATE,'state':'CHECKING_DUPLICATES','run_id':os.environ['GITHUB_RUN_ID'],
           'source_sha':os.environ['GITHUB_SHA'],'web_verified':live,'api_operations':[],
           'prior_image_cost_estimate_usd':0.129595,'combined_cap_usd':0.39,
           'x_full_attempt_reserve_usd':0.255,'automatic_retries':0,
           'article_url':ARTICLE,'card_sha256':manifest['x_card_sha256'],
           'card_path':REL_CARD,'post_text':publish_x.build_post_text(ready)}
    require(0.129595+0.255<=0.39,'Projected complete attempt exceeds budget')
    save(state)
    auth=publish_x.oauth()
    charge(state,'GET users/me',0.01)
    me=get(publish_x.ME_URL,auth)['data']
    require(me.get('id'),'X author missing')
    state['author_id']=me['id'];state['username']=me.get('username','')
    charge(state,'GET own timeline, at most 5 post resources',0.025)
    timeline=get('https://api.x.com/2/users/'+me['id']+'/tweets',auth,
                 {'max_results':5,'exclude':'retweets,replies','tweet.fields':'created_at,entities',
                  'start_time':'2026-11-01T00:00:00Z'})
    rows=timeline.get('data') or []
    require(isinstance(rows,list) and len(rows)<=5 and isinstance(timeline.get('meta'),dict),'Timeline incomplete')
    require(all(isinstance(r,dict) and r.get('id') for r in rows),'Malformed timeline')
    state['timeline_rows']=rows
    found=[row for row in rows if links(row)]
    if found:
        state.update(state='DUPLICATE_FOUND_HOLD',existing_post_ids=[r['id'] for r in found]);save(state)
        raise RuntimeError('Existing November 2 URL; do not post')
    require(not timeline['meta'].get('next_token'),'More than five posts require a new budget review; no posting')
    state['duplicate_scan_complete']=True
    state['state']='DUPLICATE_SCAN_CLEAR'
    CARD.parent.mkdir(parents=True,exist_ok=True)
    require(not CARD.exists(),'Existing card; inspect')
    shutil.copy2(PACKAGE/ready['canonical_x_image_path'],CARD)
    save(state)
    print('PASS: live Web verified, complete duplicate scan clear, cost envelope USD0.384595 including prior images')
def arm():
    guard();state=load(STATE)
    require(state['run_id']==os.environ['GITHUB_RUN_ID'] and state['state']=='DUPLICATE_SCAN_CLEAR','Wrong or uncertain state')
    require(state['duplicate_scan_complete'] and state['web_verified']['verified'],'Missing preflight')
    require(hashlib.sha256(CARD.read_bytes()).hexdigest()==state['card_sha256'],'Card hash mismatch')
    state['state']='POST_IN_FLIGHT';state['armed_at']=datetime.now(timezone.utc).isoformat();save(state)
    print('POST_IN_FLIGHT: persist this state before any X write')
def post():
    guard();state=load(STATE)
    require(state['state']=='POST_IN_FLIGHT' and state['run_id']==os.environ['GITHUB_RUN_ID'],'Not armed for this one-shot run')
    require(hashlib.sha256(CARD.read_bytes()).hexdigest()==state['card_sha256'],'Card changed')
    import subprocess
    persisted=json.loads(subprocess.check_output(['git','show','HEAD:automation_state/x_backfill_2026-11-02_result.json'],cwd=SITE))
    require(persisted==state,'Persisted checkpoint mismatch')
    require(subprocess.check_output(['git','ls-remote','origin','refs/heads/main'],cwd=SITE,text=True).split()[0]
            ==subprocess.check_output(['git','rev-parse','HEAD'],cwd=SITE,text=True).strip(),'Main advanced before posting; hold')
    auth=publish_x.oauth()
    try:
        charge(state,'POST media upload (conservative reserve)',0.015)
        media=publish_x.upload_image(CARD,auth)
        state['media_id']=media;save(state)
        charge(state,'POST tweet with article URL',0.20)
        post_id,receipt=publish_x.create_post(state['post_text'],media,auth)
        state.update(state='POSTED_UNVERIFIED',post_id=post_id,post_url=(
            'https://x.com/'+state['username']+'/status/'+post_id if state['username']
            else 'https://x.com/i/web/status/'+post_id),create_receipt=receipt)
        save(state)
        charge(state,'GET created tweet verification',0.005)
        verified=get('https://api.x.com/2/tweets/'+post_id,auth,{'tweet.fields':'author_id,entities,attachments,created_at'})['data']
        require(verified.get('id')==post_id and verified.get('author_id')==state['author_id'],'Post identity mismatch')
        require(links(verified) and 'Vega Island' in verified.get('text',''),'Post issue/content mismatch')
        require(any(str(key).endswith('_'+str(media)) for key in verified.get('attachments',{}).get('media_keys',[])),
                'Post media mismatch')
        state['state']='X_POSTED_VERIFIED';state['verification']=verified
        state['verified_at']=datetime.now(timezone.utc).isoformat()
        state['combined_cost_reserve_usd']=round(0.129595+sum(r['reserve_usd'] for r in state['api_operations']),8)
        save(state);print('X_POSTED_VERIFIED:',state['post_url'])
    except Exception as exc:
        state['state']='POSTED_UNVERIFIED' if state.get('post_id') else 'POST_OUTCOME_UNKNOWN_NO_RETRY'
        state['error_type']=type(exc).__name__;save(state)
        raise RuntimeError('X attempt stopped; persist state and inspect, never retry posting') from None
if __name__=='__main__':
    try:
        {'prepare':prepare,'arm':arm,'post':post}[sys.argv[1]]()
    except Exception as exc:
        print('STOP:',type(exc).__name__,file=sys.stderr)
        sys.exit(1)
