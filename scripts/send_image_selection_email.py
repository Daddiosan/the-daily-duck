#!/usr/bin/env python3
"""
The Daily Duck - Gate B image selection email

Sends exactly five attached candidate images.
Valid replies:
- 1 / 2 / 3 / 4 / 5 = choose final image
- NEXT 5 = reject this batch and generate five new executions
"""

from __future__ import annotations

import json
import mimetypes
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

STATE_DIR = Path("automation_state")
CANDIDATES_PATH = STATE_DIR / "image_candidates.json"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_candidates() -> dict[str, Any]:
    if not CANDIDATES_PATH.exists():
        raise FileNotFoundError(f"Missing {CANDIDATES_PATH}")
    data = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))
    if data.get("state") != "WAITING_IMAGE_SELECTION":
        raise RuntimeError("image_candidates.json is not WAITING_IMAGE_SELECTION.")
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 5:
        raise ValueError("Exactly five image candidates are required.")
    return data


def main() -> int:
    data = load_candidates()
    issue_date = str(data.get("issue_date") or datetime.now().date().isoformat())
    batch = int(data.get("batch", 1))
    subject = f"The Daily Duck — Image Selection — {issue_date} — Batch {batch}"

    concept = data.get("selected_image_concept") or {}
    concept_title = concept.get("title", "") if isinstance(concept, dict) else ""

    body = f"""The Daily Duck — Gate B

Gate Aで選択した画像コンセプト:
{concept_title}

Batch {batch} の画像候補を5枚添付しました。

==================================================
FINAL SELECTION
==================================================
気に入った画像がある場合:
1
2
3
4
5

のいずれか1文字だけを返信してください。

5枚すべて気に入らない場合:
NEXT 5

だけを返信してください。

NEXT 5 の場合:
- Gate Aで選んだ画像コンセプトは変更しません。
- 同じコンセプトから新しい画像を5枚生成します。
- READY_TO_PUBLISH には進みません。
- 新しいBatchのメールを送ります。

重要:
- 1〜5以外では画像確定しません。
- NEXT 5 は承認ではありません。
- Gate B完了前にWebサイト/Xへ公開しません。
"""

    gmail = required_env("GMAIL_ADDRESS")
    password = required_env("GMAIL_APP_PASSWORD")
    recipients = [x.strip() for x in required_env("EMAIL_TO").split(",") if x.strip()]

    msg = EmailMessage()
    msg["From"] = gmail
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    for candidate in sorted(data["candidates"], key=lambda x: int(x["number"])):
        number = int(candidate["number"])
        path = Path(candidate["image_path"])
        if not path.exists():
            raise FileNotFoundError(f"Candidate image missing: {path}")
        mime, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (mime or "image/png").split("/", 1)
        msg.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=f"{number}-{path.name}",
        )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(gmail, password)
        smtp.send_message(msg)

    data["email_subject"] = subject
    data["email_sent_at"] = datetime.now(timezone.utc).isoformat()
    CANDIDATES_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Gate B email sent. Batch: {batch}")
    print(f"Subject: {subject}")
    print("Valid replies: 1 / 2 / 3 / 4 / 5 / NEXT 5")
    print("STATE: WAITING_IMAGE_SELECTION")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
