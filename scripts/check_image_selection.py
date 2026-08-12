#!/usr/bin/env python3
"""
The Daily Duck - final image reply checker

Current Phase 2 flow:
- Five real OpenAI images already exist in automation_state/image_candidates/
- User replies with exact "1" through "5"
- That exact existing PNG becomes the canonical image
- State becomes READY_TO_PUBLISH

No NEXT 5 path is used in this version.
"""

from __future__ import annotations

import email
import imaplib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path
from typing import Any

STATE_DIR = Path("automation_state")
CANDIDATES_PATH = STATE_DIR / "image_candidates.json"
APPROVED_PATH = STATE_DIR / "approved_story.json"
READY_PATH = STATE_DIR / "ready_to_publish.json"
RESULT_PATH = STATE_DIR / "gate_b_result.json"
CANONICAL_DIR = Path("automation_images") / "canonical"

CHOICE_RE = re.compile(r"^[1-5]$")
SUBJECT_PREFIX = "The Daily Duck — Final Image Selection"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def decode_mime(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def plain_text(msg: email.message.Message) -> str:
    chunks: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() != "text/plain":
                continue
            if "attachment" in str(part.get("Content-Disposition", "")).lower():
                continue
            payload = part.get_payload(decode=True)
            if payload is not None:
                chunks.append(
                    payload.decode(
                        part.get_content_charset() or "utf-8",
                        errors="replace",
                    )
                )
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            chunks.append(
                payload.decode(
                    msg.get_content_charset() or "utf-8",
                    errors="replace",
                )
            )
    return "\n".join(chunks)


def strip_quoted(text: str) -> str:
    out: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        s = line.strip()
        if s.startswith(">"):
            break
        if re.match(r"^On .+ wrote:$", s, flags=re.I):
            break
        if s in ("-----Original Message-----", "-----元のメッセージ-----"):
            break
        if re.match(r"^(From|Sent|To|Subject):\s", s, flags=re.I):
            break
        out.append(line)
    return "\n".join(out).strip()


def normalize(text: str) -> str:
    fresh = strip_quoted(text)
    lines = [x.strip() for x in fresh.splitlines() if x.strip()]
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def load_candidates() -> dict[str, Any]:
    if not CANDIDATES_PATH.exists():
        raise FileNotFoundError(f"Missing {CANDIDATES_PATH}")

    data = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))

    if data.get("state") != "IMAGE_CANDIDATES_READY":
        raise RuntimeError(
            "image_candidates.json is not IMAGE_CANDIDATES_READY."
        )

    candidates = data.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 5:
        raise ValueError("Exactly five candidates are required.")

    for i, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            raise ValueError(f"Candidate {i} must be an object.")
        if int(candidate.get("number", 0)) != i:
            raise ValueError("Candidates must be numbered exactly 1-5.")
        path = Path(str(candidate.get("image_path", "")))
        if not path.exists():
            raise FileNotFoundError(f"Candidate image missing: {path}")

    return data


def allowed_senders() -> set[str]:
    return {
        x.strip().lower()
        for x in required_env("EMAIL_TO").split(",")
        if x.strip()
    }


def newest_choice(imap: imaplib.IMAP4_SSL) -> tuple[str, str] | None:
    allowed = allowed_senders()

    status, data = imap.search(None, "ALL")
    if status != "OK":
        raise RuntimeError("IMAP search failed.")

    ids = data[0].split()

    for msg_id in reversed(ids[-250:]):
        status, payload = imap.fetch(msg_id, "(RFC822)")
        if status != "OK" or not payload or not isinstance(payload[0], tuple):
            continue

        msg = email.message_from_bytes(payload[0][1])
        subject = decode_mime(msg.get("Subject"))
        sender = parseaddr(decode_mime(msg.get("From")))[1].lower()

        # Replies normally become "Re: The Daily Duck — Final Image Selection — ..."
        if SUBJECT_PREFIX not in subject:
            continue

        if allowed and sender not in allowed:
            continue

        command = normalize(plain_text(msg))
        if CHOICE_RE.fullmatch(command):
            return command, sender

    return None


def write_result(action: str, **extra: Any) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "action": action,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def issue_date_from(data: dict[str, Any], approved: dict[str, Any]) -> str:
    for source in (data, approved):
        for key in ("issue_date", "date"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    story = data.get("story")
    if isinstance(story, dict):
        for key in ("issue_date", "date"):
            value = story.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return datetime.now(timezone.utc).date().isoformat()


def main() -> int:
    data = load_candidates()
    gmail = required_env("GMAIL_ADDRESS")
    password = required_env("GMAIL_APP_PASSWORD")

    with imaplib.IMAP4_SSL("imap.gmail.com", 993) as imap:
        imap.login(gmail, password)
        imap.select("INBOX")
        found = newest_choice(imap)

    if not found:
        write_result("WAIT")
        print("No exact image selection reply found.")
        print("STATE: IMAGE_CANDIDATES_READY")
        return 0

    command, sender = found
    choice = int(command)

    selected = next(
        (
            x
            for x in data["candidates"]
            if int(x.get("number", 0)) == choice
        ),
        None,
    )
    if selected is None:
        raise RuntimeError(f"Candidate {choice} not found.")

    src = Path(str(selected["image_path"]))
    if not src.exists():
        raise FileNotFoundError(f"Selected image missing: {src}")

    if not APPROVED_PATH.exists():
        raise FileNotFoundError(f"Missing {APPROVED_PATH}")
    approved = json.loads(APPROVED_PATH.read_text(encoding="utf-8"))

    issue_date = issue_date_from(data, approved)

    CANONICAL_DIR.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower() or ".png"
    canonical_path = CANONICAL_DIR / f"{issue_date}{ext}"
    shutil.copy2(src, canonical_path)

    ready = {
        "state": "READY_TO_PUBLISH",
        "issue_date": issue_date,
        "ready_at": datetime.now(timezone.utc).isoformat(),
        "gate_a_approved_story": approved,
        "selected_image_number": choice,
        "selected_candidate": selected,
        "canonical_image_path": canonical_path.as_posix(),
        "final_image_reply": command,
        "final_image_sender": sender,
        "publish_started": False,
    }

    READY_PATH.write_text(
        json.dumps(ready, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    data["state"] = "IMAGE_SELECTED"
    data["selected_image_number"] = choice
    data["selected_candidate"] = selected
    data["selected_at"] = datetime.now(timezone.utc).isoformat()
    data["canonical_image_path"] = canonical_path.as_posix()

    CANDIDATES_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    write_result(
        "READY_TO_PUBLISH",
        selected=choice,
        canonical_image_path=canonical_path.as_posix(),
    )

    print(f"EXACT IMAGE SELECTION FOUND: {choice}")
    print(f"Canonical image: {canonical_path}")
    print("STATE: READY_TO_PUBLISH")
    print("Publishing is NOT started in Phase 2.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
