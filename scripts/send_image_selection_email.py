#!/usr/bin/env python3
from __future__ import annotations

import json
import mimetypes
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any

STATE_PATH = Path("automation_state/image_candidates.json")
PREVIEW_PATH = Path("image_selection_email.txt")
SUBJECT_PREFIX = "The Daily Duck — Final Image Selection"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Required file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return data


def validate_state(data: dict[str, Any]) -> list[dict[str, Any]]:
    state = str(data.get("state", "")).strip().upper()
    if state != "IMAGE_CANDIDATES_READY":
        raise ValueError(f"Expected IMAGE_CANDIDATES_READY, got {state!r}.")

    candidates = data.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 5:
        raise ValueError("Exactly five image candidates are required.")

    for i, item in enumerate(candidates, start=1):
        if not isinstance(item, dict) or int(item.get("number", 0)) != i:
            raise ValueError("Image candidates must be numbered exactly 1-5.")
        path = Path(str(item.get("image_path", "")))
        if not path.exists():
            raise FileNotFoundError(f"Candidate image not found: {path}")

    return candidates


def get_story_title(data: dict[str, Any]) -> str:
    story = data.get("story")
    if isinstance(story, dict):
        for key in ("title_ja", "title", "headline_ja", "headline"):
            value = story.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return "Approved Daily Duck Story"


def parse_recipients(raw: str) -> list[str]:
    recipients = [x.strip() for x in raw.split(",") if x.strip()]
    if not recipients:
        raise RuntimeError("EMAIL_TO contains no valid recipients.")
    return recipients


def build_plain(data: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    lines = [
        "THE DAILY DUCK — 最終画像を選択",
        "",
        f"承認済み記事: {get_story_title(data)}",
        "",
        "承認済みの1つの画像コンセプトから、実画像を5案作成しました。",
        "添付画像 1〜5 を確認して、最終的に使用する画像を1つ選んでください。",
        "",
    ]

    for item in candidates:
        n = item["number"]
        title = item.get("concept_title_ja") or item.get("concept_title_en") or ""
        lines.append(f"{n}. {title}")

    lines += [
        "",
        "返信方法:",
        "採用する場合は 1 / 2 / 3 / 4 / 5 のいずれか1文字だけ返信してください。\n気に入らない場合は NEXT 5 と返信してください。",
        "",
        "例:",
        "3",
        "",
        "選択した画像そのものをWebサイトとXで共通使用するcanonical imageとして固定します。",
        "選択が完了するまで公開は行いません。",
        "",
        "The Daily Duck",
        "One day. One story. One duck. 🐤",
    ]
    return "\n".join(lines) + "\n"


def build_html(data: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    cards = []
    for item in candidates:
        n = int(item["number"])
        title = str(item.get("concept_title_ja") or item.get("concept_title_en") or "")
        cid = f"daily-duck-candidate-{n}"
        cards.append(
            f"""
            <div style="margin:22px 0;padding:16px;border:1px solid #ddd;border-radius:14px;">
              <div style="font-size:28px;font-weight:700;margin-bottom:10px;">{n}</div>
              <img src="cid:{cid}" alt="Candidate {n}"
                   style="width:100%;max-width:760px;height:auto;border-radius:10px;display:block;">
              <div style="font-size:18px;font-weight:600;margin-top:10px;">{title}</div>
            </div>
            """
        )

    return f"""
    <html>
      <body style="font-family:Arial,'Hiragino Kaku Gothic ProN','Yu Gothic',sans-serif;
                   max-width:820px;margin:auto;color:#14233b;">
        <h1>The Daily Duck — 最終画像を選択</h1>
        <p><strong>承認済み記事:</strong> {get_story_title(data)}</p>
        <p>同じ承認済みコンセプトから作った5案です。採用番号、または NEXT 5 を返信してください。</p>
        {''.join(cards)}
        <div style="margin:28px 0;padding:18px;background:#fff6d8;border-radius:12px;">
          <strong>返信:</strong> 1 / 2 / 3 / 4 / 5 のいずれか1文字だけ<br>
          例: <strong>3</strong><br>再生成: <strong>NEXT 5</strong>
        </div>
        <p>選択した画像そのものをWebサイトとXの共通canonical imageとして固定します。</p>
        <p>One day. One story. One duck. 🐤</p>
      </body>
    </html>
    """


def main() -> None:
    data = load_json(STATE_PATH)
    candidates = validate_state(data)

    gmail_address = required_env("GMAIL_ADDRESS")
    gmail_app_password = required_env("GMAIL_APP_PASSWORD")
    recipients = parse_recipients(required_env("EMAIL_TO"))

    plain = build_plain(data, candidates)
    html = build_html(data, candidates)
    PREVIEW_PATH.write_text(plain, encoding="utf-8")

    msg = EmailMessage()
    msg["Subject"] = f"{SUBJECT_PREFIX} — {get_story_title(data)}"
    msg["From"] = gmail_address
    msg["To"] = ", ".join(recipients)
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")

    html_part = msg.get_payload()[-1]

    for item in candidates:
        n = int(item["number"])
        image_path = Path(str(item["image_path"]))
        image_bytes = image_path.read_bytes()
        mime_type, _ = mimetypes.guess_type(image_path.name)
        maintype, subtype = (mime_type or "image/png").split("/", 1)

        # Inline image for HTML email.
        html_part.add_related(
            image_bytes,
            maintype=maintype,
            subtype=subtype,
            cid=f"<daily-duck-candidate-{n}>",
            filename=f"DailyDuck_{n}.png",
            disposition="inline",
        )

        # Also attach each image explicitly so it is easy to open/save on phones.
        msg.add_attachment(
            image_bytes,
            maintype=maintype,
            subtype=subtype,
            filename=f"DailyDuck_{n}.png",
        )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(gmail_address, gmail_app_password)
        smtp.send_message(msg)

    print("Final image selection email sent.")
    print("Candidate count: 5")
    print("Valid replies: 1 / 2 / 3 / 4 / 5")
    print(f"Preview saved: {PREVIEW_PATH}")


if __name__ == "__main__":
    main()
