#!/usr/bin/env python3
from __future__ import annotations
import json, os, smtplib
from email.message import EmailMessage
from pathlib import Path

P=Path("automation_state/design_options.json")
def env(n):
    v=os.getenv(n,"").strip()
    if not v: raise RuntimeError(f"Missing {n}")
    return v
def main():
    d=json.loads(P.read_text(encoding="utf-8"))
    issue=d["issue_date"]
    subject=f"The Daily Duck — Design Approval — {issue}"
    lines=[
      f"THE DAILY DUCK — DESIGN APPROVAL — {issue}","",
      "画像案3案とタイトル案3案を作成しました。","",
      "━━━━━━━━━━━━━━━━━━","IMAGE CONCEPTS / 画像案","━━━━━━━━━━━━━━━━━━",""
    ]
    for x in d["image_concepts"]:
        lines += [f"[IMAGE {x['number']}] {x.get('title_ja','')}",
                  x.get("concept_ja",""),f"EN: {x.get('title_en','')}",
                  x.get("concept_en",""),""]
    lines += ["━━━━━━━━━━━━━━━━━━","TITLE IDEAS / タイトル案","━━━━━━━━━━━━━━━━━━",""]
    for x in d["title_ideas"]:
        lines += [f"[TITLE {x['number']}] {x['title']}",x.get("meaning_ja",""),""]
    lines += ["━━━━━━━━━━━━━━━━━━","返信方法","━━━━━━━━━━━━━━━━━━",
              "画像番号、半角スペース、タイトル番号だけを返信してください。",
              "例：画像2 + タイトル1 →  2 1","","有効な返信: 1 1 ～ 3 3"]
    body="\n".join(lines)
    msg=EmailMessage(); msg["Subject"]=subject; msg["From"]=env("GMAIL_ADDRESS")
    tos=[x.strip() for x in env("EMAIL_TO").split(",") if x.strip()]
    msg["To"]=", ".join(tos); msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com",465) as s:
        s.login(env("GMAIL_ADDRESS"),env("GMAIL_APP_PASSWORD")); s.send_message(msg)
    d["email_subject"]=subject; d["state"]="WAITING_DESIGN_SELECTION"
    P.write_text(json.dumps(d,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("Design approval email sent.")
if __name__=="__main__": main()
