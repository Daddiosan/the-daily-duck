#!/usr/bin/env python3
from __future__ import annotations
import email, imaplib, json, os, re
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path

STATE=Path("automation_state"); P=STATE/"design_options.json"; OUT=STATE/"selected_design.json"; RESULT=STATE/"design_selection_result.json"
VALID=re.compile(r"^([1-3])\s+([1-3])$")
def env(n):
    v=os.getenv(n,"").strip()
    if not v: raise RuntimeError(f"Missing {n}")
    return v
def dec(v):
    try:return str(make_header(decode_header(v or "")))
    except:return v or ""
def body(msg):
    chunks=[]
    for p in msg.walk() if msg.is_multipart() else [msg]:
        if p.get_content_type()!="text/plain": continue
        b=p.get_payload(decode=True)
        if b: chunks.append(b.decode(p.get_content_charset() or "utf-8",errors="replace"))
    return "\n".join(chunks)
def normalize(t):
    out=[]
    for line in t.replace("\r","").split("\n"):
        s=line.strip()
        if s.startswith(">") or re.match(r"^On .+ wrote:$",s,re.I) or s in ("-----Original Message-----","-----元のメッセージ-----"): break
        if re.match(r"^(From|Sent|To|Subject):\s",s,re.I): break
        if s: out.append(s)
    return re.sub(r"\s+"," "," ".join(out)).strip()
def result(action,**kw):
    RESULT.write_text(json.dumps({"action":action,"checked_at":datetime.now(timezone.utc).isoformat(),**kw},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
def main():
    d=json.loads(P.read_text(encoding="utf-8")); subject=d["email_subject"]
    allowed={x.strip().lower() for x in env("EMAIL_TO").split(",") if x.strip()}
    found=None
    with imaplib.IMAP4_SSL("imap.gmail.com",993) as im:
        im.login(env("GMAIL_ADDRESS"),env("GMAIL_APP_PASSWORD")); im.select("INBOX")
        st,ids=im.search(None,"ALL")
        for mid in reversed(ids[0].split()[-250:]):
            st,p=im.fetch(mid,"(RFC822)")
            if st!="OK" or not p or not isinstance(p[0],tuple): continue
            m=email.message_from_bytes(p[0][1]); sender=parseaddr(dec(m.get("From")))[1].lower()
            if subject not in dec(m.get("Subject")) or (allowed and sender not in allowed): continue
            cmd=normalize(body(m)); mm=VALID.fullmatch(cmd)
            if mm: found=(int(mm.group(1)),int(mm.group(2)),sender,cmd); break
    if not found:
        result("WAIT"); print("STATE: WAITING_DESIGN_SELECTION"); return
    ic,tc,sender,cmd=found
    concept=d["image_concepts"][ic-1]; title=d["title_ideas"][tc-1]
    approved=d["approved_story"]
    selected={
      "state":"DESIGN_SELECTED","issue_date":d["issue_date"],
      "selected_at":datetime.now(timezone.utc).isoformat(),
      "selected_image_concept_number":ic,"selected_image_concept":concept,
      "selected_title_number":tc,"selected_title":title["title"],
      "selected_title_detail":title,"approval_reply":cmd,"approval_sender":sender,
      "approved_story":approved
    }
    OUT.write_text(json.dumps(selected,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    # Also enrich approved_story for compatibility with downstream scripts.
    approved["selected_image_concept_number"]=ic; approved["selected_image_concept"]=concept
    approved["selected_title_number"]=tc; approved["selected_title"]=title["title"]
    (STATE/"approved_story.json").write_text(json.dumps(approved,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    result("DESIGN_SELECTED",image_concept=ic,title=tc)
    print(f"DESIGN SELECTED: image {ic}, title {tc}")
if __name__=="__main__": main()
