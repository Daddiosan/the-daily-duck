"""Verify the November 2 live page and exact approved hero; no publication writes."""
import hashlib,json,sys,time,urllib.request
from datetime import datetime,timezone
from pathlib import Path

HERE=Path(__file__).resolve().parent
URL='https://www.thedailyduck.ai/ducks/2026-11-02/'
IMAGE='https://www.thedailyduck.ai/assets/ducks/2026-11-02-skullcape.png'
HERO='964d9f6da6b37f03745bfd672fc0552c284ee038ffcd66df9bc318e390d7e930'
def fetch(url):
    request=urllib.request.Request(url,headers={'Cache-Control':'no-cache','User-Agent':'DailyDuck-Recovery-Verification'})
    with urllib.request.urlopen(request,timeout=25) as response:
        assert response.status==200
        return response.read()
def verify():
    body=fetch(URL)
    text=body.decode('utf-8')
    assert 'A CHILL DOWN YOUR FEATHERS' in text and '2026-11-02' in text
    assert 'Vega Island' in text
    assert 'https://science.nasa.gov/earth/earth-observatory/vega-island-frozen-in-fright/' in text
    assert '2026-11-02-skullcape.png' in text
    image=fetch(IMAGE)
    assert hashlib.sha256(image).hexdigest()==HERO
    return dict(verified=True,issue_date='2026-11-02',title='A CHILL DOWN YOUR FEATHERS',
                page_url=URL,image_url=IMAGE,http_status=200,image_sha256=HERO,
                html_sha256=hashlib.sha256(body).hexdigest(),
                verified_at=datetime.now(timezone.utc).isoformat())
if __name__=='__main__':
    attempts=18 if '--wait' in sys.argv else 1
    for attempt in range(attempts):
        try:
            result=verify()
            (HERE/'web_verification.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
            print(json.dumps(result,indent=2))
            break
        except Exception as exc:
            if attempt+1==attempts:
                print('LIVE_WEB_UNVERIFIED:',type(exc).__name__,file=sys.stderr)
                sys.exit(1)
            time.sleep(5)
