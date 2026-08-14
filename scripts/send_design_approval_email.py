#!/usr/bin/env python3
from __future__ import annotations

import json
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


def esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def main() -> int:
    package = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    if package.get("state") != "DESIGN_PREVIEWS_READY":
        raise RuntimeError(
            f"Expected DESIGN_PREVIEWS_READY, got {package.get('state')!r}"
        )

    issue_date = first_text(package.get("issue_date"))
    concepts = package["image_concepts"]
    titles = package["title_ideas"]
    previews = package["design_previews"]

    preview_by_number = {int(x["number"]): x for x in previews}
    subject = f"The Daily Duck — Image + Title Approval — {issue_date}"

    text_lines = [
        "THE DAILY DUCK — IMAGE + TITLE APPROVAL", "",
        "画像3案とタイトル3案から、それぞれ1つ選んでください。",
        "添付された画像そのものが最終候補です。", "",
        "IMAGE / 画像候補", ""
    ]

    html_parts = [
        "<html><body style='font-family:Arial,sans-serif;color:#10254a;'>",
        "<h2>The Daily Duck — Image + Title Approval</h2>",
        "<p>画像3案とタイトル3案から、それぞれ1つ選んでください。"
        "<br><strong>表示される画像そのものが最終候補です。</strong></p>",
    ]

    for concept in concepts:
        n = int(concept["number"])
        title_ja = first_text(concept.get("title_ja"))
        title_en = first_text(concept.get("title_en"))
        concept_ja = first_text(concept.get("concept_ja"))
        p = Path(preview_by_number[n]["image_path"])
        cid = f"dailyduck-preview-{n}"

        text_lines += [
            f"[IMAGE {n}] {title_ja}",
            f"EN: {title_en}",
            concept_ja,
            f"添付ファイル: {p.name}", ""
        ]

        html_parts += [
            "<div style='margin:24px 0;padding:18px;border:1px solid #ddd;border-radius:14px;'>",
            f"<h3>IMAGE {n} — {esc(title_ja)}</h3>",
            f"<p><strong>{esc(title_en)}</strong><br>{esc(concept_ja)}</p>",
            f"<img src='cid:{cid}' style='display:block;max-width:700px;width:100%;height:auto;border-radius:12px;' alt='Image candidate {n}'>",
            "</div>",
        ]

    text_lines += ["TITLE / タイトル候補", ""]
    html_parts += ["<h3>TITLE IDEAS / タイトル候補</h3>"]

    for item in titles:
        n = int(item["number"])
        title = first_text(item.get("title"))
        meaning = first_text(item.get("meaning_ja"))
        text_lines += [f"[TITLE {n}] {title}", meaning, ""]
        html_parts += [f"<p><strong>TITLE {n}: {esc(title)}</strong><br>{esc(meaning)}</p>"]

    text_lines += [
        "返信方法:", "画像番号 半角スペース タイトル番号",
        "例: 2 1", "",
        "画像2 + タイトル1 を最終採用します。",
        "有効な返信: 1 1 ～ 3 3"
    ]
    html_parts += [
        "<div style='margin-top:28px;padding:18px;background:#f4f6f8;border-radius:12px;'>",
        "<strong>返信方法</strong><br>画像番号 + 半角スペース + タイトル番号<br><br>",
        "<span style='font-size:26px;font-weight:bold;'>2 1</span><br>",
        "＝ 画像2 + タイトル1 を最終採用",
        "</div></body></html>"
    ]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = required_env("GMAIL_ADDRESS")
    recipients = [x.strip() for x in required_env("EMAIL_TO").split(",") if x.strip()]
    msg["To"] = ", ".join(recipients)
    msg.set_content("\n".join(text_lines))
    msg.add_alternative("".join(html_parts), subtype="html")
    html_part = msg.get_payload()[-1]

    for n in (1, 2, 3):
        p = Path(preview_by_number[n]["image_path"])
        data = p.read_bytes()
        html_part.add_related(
            data,
            maintype="image",
            subtype="jpeg",
            cid=f"<dailyduck-preview-{n}>",
            filename=p.name,
            disposition="inline",
        )

    for n in (1, 2, 3):
        p = Path(preview_by_number[n]["image_path"])
        msg.add_attachment(
            p.read_bytes(),
            maintype="image",
            subtype="jpeg",
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

    print("Sent one email containing 3 JPEG images + 3 title ideas.")
    print("STATE: WAITING_DESIGN_SELECTION")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
