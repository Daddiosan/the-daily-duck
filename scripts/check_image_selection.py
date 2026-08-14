#!/usr/bin/env python3
from __future__ import annotations
import email, imaplib, json, os, re, shutil
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path
STATE=Path("automation_state"); C=STATE/"image_candidates.json"; A=STATE/"approved_story.json"; R=STATE/"ready_to_publish.json"; REGEN=STATE/"image_regeneration_request.json"; RESULT=STATE/"gate_b_result.json"; CAN=Path("automation_images/canonical")
CHOICE=re.compile(r"^[1-5]$"); NEXT=re.compile(r"^NEXT\s+5$",re.I)
def env(n):
    v=os.getenv(n,"").strip()
    if not v: raise RuntimeError(f"Missing {n}")
    return v
def dec(v):
    try:return str(make_header(decode_header(v or "")))
    except:return v or ""
def msgtext(m):
    ps=m.walk() if m.is_multipart() else [m]; z=[]
    for p in ps:
        if p.get_content_type()!="text/plain":continue
        b=p.get_payload(decode=True)
        if b:z.append(b.decode(p.get_content_charset() or "utf-8",errors="replace"))
    return "\n".join(z)
def norm(t):
    z=[]
    for l in t.replace("\r","").split("\n"):
        s=l.strip()
        if s.startswith(">") or re.match(r"^On .+ wrote:$",s,re.I) or s in ("-----Original Message-----","-----元のメッセージ-----"):break
        if re.match(r"^(From|Sent|To|Subject):\s",s,re.I):break
        if s:z.append(s)
    return re.sub(r"\s+"," "," ".join(z)).strip()
def wr(action,**x): RESULT.write_text(json.dumps({"action":action,"checked_at":datetime.now(timezone.utc).isoformat(),**x},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
def main():
    d=json.loads(C.read_text(encoding="utf-8")); subject=d["email_subject"]; allowed={x.strip().lower() for x in env("EMAIL_TO").split(",") if x.strip()}
    found=None
    with imaplib.IMAP4_SSL("imap.gmail.com",993) as im:
        im.login(env("GMAIL_ADDRESS"),env("GMAIL_APP_PASSWORD"));im.select("INBOX");_,ids=im.search(None,"ALL")
        for mid in reversed(ids[0].split()[-250:]):
            st,p=im.fetch(mid,"(RFC822)")
            if st!="OK" or not p or not isinstance(p[0],tuple):continue
            m=email.message_from_bytes(p[0][1]);sender=parseaddr(dec(m.get("From")))[1].lower()
            if subject not in dec(m.get("Subject")) or (allowed and sender not in allowed):continue
            cmd=norm(msgtext(m))
            if CHOICE.fullmatch(cmd) or NEXT.fullmatch(cmd):found=(cmd.upper(),sender);break
    if not found:wr("WAIT");print("STATE: WAITING_IMAGE_SELECTION");return
    cmd,sender=found; issue=str(d.get("issue_date","")); batch=int(d.get("batch",1))
    if NEXT.fullmatch(cmd):
        REGEN.write_text(json.dumps({"action":"NEXT_5","issue_date":issue,"rejected_batch":batch,"requested_at":datetime.now(timezone.utc).isoformat(),"requested_by":sender},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        wr("REGENERATE_IMAGES",batch=batch,command=cmd);print("STATE: REGENERATE_IMAGES");return
    n=int(cmd); sel=next(x for x in d["candidates"] if int(x["number"])==n); src=Path(sel["image_path"])
    CAN.mkdir(parents=True,exist_ok=True); dst=CAN/f"{issue}{src.suffix.lower() or '.png'}";shutil.copy2(src,dst)
    a=json.loads(A.read_text(encoding="utf-8"))
    ready={"state":"READY_TO_PUBLISH","issue_date":issue,"ready_at":datetime.now(timezone.utc).isoformat(),
      "gate_a_approved_story":a,"selected_image_concept_number":a.get("selected_image_concept_number"),
      "selected_image_concept":a.get("selected_image_concept"),"selected_title_number":a.get("selected_title_number"),
      "selected_title":a.get("selected_title"),"selected_image_number":n,"selected_image_batch":batch,
      "selected_candidate":sel,"canonical_image_path":dst.as_posix(),"gate_b_reply":cmd,"gate_b_sender":sender,"publish_started":False}
    R.write_text(json.dumps(ready,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    d["state"]="IMAGE_SELECTED";d["selected_image_number"]=n;C.write_text(json.dumps(d,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    if REGEN.exists():REGEN.unlink()
    wr("READY_TO_PUBLISH",batch=batch,selected=n);print("STATE: READY_TO_PUBLISH")
if __name__=="__main__":main()
