#!/usr/bin/env python3
from __future__ import annotations

import json
import mimetypes
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any

OPTIONS_PATH = Path("automation_state/design_options.json")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def esc(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def mime_for(path: Path) -> tuple[str, str]:
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and "/" in guessed:
        return tuple(guessed.split("/", 1))  # type: ignore[return-value]
    return ("image", "png")


def main() -> int:
    package = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    if package.get("state") != "DESIGN_PREVIEWS_READY":
        raise RuntimeError(
            f"Expected DESIGN_PREVIEWS_READY, got {package.get('state')!r}"
        )

    concepts = package.get("image_concepts")
    titles = package.get("title_ideas")
    previews = package.get("design_previews")

    if not isinstance(concepts, list) or len(concepts) != 3:
        raise ValueError("Exactly 3 concepts are required.")
    if not isinstance(titles, list) or len(titles) != 3:
        raise ValueError("Exactly 3 titles are required.")
    if not isinstance(previews, list) or len(previews) != 3:
        raise ValueError("Exactly 3 preview images are required.")

    issue_date = first_text(package.get("issue_date"))
    subject = f"The Daily Duck — Image + Title Approval — {issue_date}"
    by_number = {int(x["number"]): x for x in previews}

    text = [
        "THE DAILY DUCK — IMAGE + TITLE APPROVAL",
        "",
        "画像3案とタイトル3案から、それぞれ1つ選んでください。",
        "このメールの画像そのものが最終画像候補です。",
        "",
    ]
    html = [
        "<html><body style='font-family:Arial,sans-serif;color:#10254a'>",
        "<h2>The Daily Duck — Image + Title Approval</h2>",
        "<p>画像3案とタイトル3案から、それぞれ1つ選んでください。<br>",
        "<strong>表示されている画像そのものが最終画像候補です。</strong></p>",
    ]

    for concept in concepts:
        n = int(concept["number"])
        p = Path(by_number[n]["image_path"])
        cid = f"dailyduck-preview-{n}"
        ja = first_text(concept.get("title_ja"))
        en = first_text(concept.get("title_en"))
        desc = first_text(concept.get("concept_ja"))

        text += [f"[IMAGE {n}] {ja}", f"EN: {en}", desc, ""]
        html += [
            "<div style='margin:24px 0;padding:16px;border:1px solid #ddd;border-radius:14px'>",
            f"<h3>IMAGE {n} — {esc(ja)}</h3>",
            f"<p><strong>{esc(en)}</strong><br>{esc(desc)}</p>",
            f"<img src='cid:{cid}' style='max-width:700px;width:100%;height:auto;border-radius:12px'>",
            "</div>",
        ]

    text += ["TITLE / タイトル候補", ""]
    html += ["<h3>TITLE / タイトル候補</h3>"]

    for item in titles:
        n = int(item["number"])
        title = first_text(item.get("title"))
        meaning = first_text(item.get("meaning_ja"))
        text += [f"[TITLE {n}] {title}", meaning, ""]
        html += [f"<p><strong>TITLE {n}: {esc(title)}</strong><br>{esc(meaning)}</p>"]

    text += [
        "返信方法: 画像番号 半角スペース タイトル番号",
        "例: 2 1",
        "",
        "2 1 = 画像2 + タイトル1 を最終採用",
        "有効な返信: 1 1 ～ 3 3",
    ]
    html += [
        "<div style='padding:18px;background:#f4f6f8;border-radius:12px'>",
        "<strong>返信方法</strong><br>画像番号 + 半角スペース + タイトル番号<br><br>",
        "<span style='font-size:26px;font-weight:bold'>2 1</span><br>",
        "＝ 画像2 + タイトル1 を最終採用",
        "</div></body></html>",
    ]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = required_env("GMAIL_ADDRESS")
    recipients = [x.strip() for x in required_env("EMAIL_TO").split(",") if x.strip()]
    msg["To"] = ", ".join(recipients)
    msg.set_content("\n".join(text))
    msg.add_alternative("".join(html), subtype="html")
    html_part = msg.get_payload()[-1]

    for n in (1, 2, 3):
        p = Path(by_number[n]["image_path"])
        maintype, subtype = mime_for(p)
        html_part.add_related(
            p.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            cid=f"<dailyduck-preview-{n}>",
            filename=p.name,
            disposition="inline",
        )

    for n in (1, 2, 3):
        p = Path(by_number[n]["image_path"])
        maintype, subtype = mime_for(p)
        msg.add_attachment(
            p.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=p.name,
        )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(required_env("GMAIL_ADDRESS"), required_env("GMAIL_APP_PASSWORD"))
        smtp.send_message(msg)

    package["email_subject"] = subject
    package["state"] = "WAITING_DESIGN_SELECTION"
    OPTIONS_PATH.write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("EMAIL SENT: exactly 3 images + exactly 3 titles.")
    print("STATE: WAITING_DESIGN_SELECTION")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
