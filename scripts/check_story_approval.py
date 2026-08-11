#!/usr/bin/env python3
"""
The Daily Duck - Gate A approval checker

Accepts ONLY:
  1 OK
  2 OK
  3 OK
  4 OK
  5 OK

Plain "OK" is invalid.

On valid approval:
- preserves the Gate A package
- stores selected image concept number + content
- writes automation_state/approved_story.json
- state = APPROVED_STORY
"""

from __future__ import annotations

import email
import imaplib
import json
import os
import re
import sys
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path
from typing import Any

PACKAGE_PATH = Path("gate_a_package.json")
STATE_DIR = Path("automation_state")
APPROVED_PATH = STATE_DIR / "approved_story.json"

VALID_RE = re.compile(r"^([1-5])\s+OK$", re.IGNORECASE)


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


def message_text(msg: email.message.Message) -> str:
    parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", "")).lower()
            if "attachment" in disp:
                continue
            if ctype == "text/plain":
                payload = part.get_payload(decode=True)
                if payload is not None:
                    charset = part.get_content_charset() or "utf-8"
                    parts.append(payload.decode(charset, errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            charset = msg.get_content_charset() or "utf-8"
            parts.append(payload.decode(charset, errors="replace"))

    return "\n".join(parts)


def strip_quoted_reply(text: str) -> str:
    kept: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()

        if stripped.startswith(">"):
            break
        if re.match(r"^On .+ wrote:$", stripped, flags=re.I):
            break
        if re.match(r"^\d{4}年\d{1,2}月\d{1,2}日.+<.+>:$", stripped):
            break
        if stripped in ("-----Original Message-----", "-----元のメッセージ-----"):
            break
        if re.match(r"^(From|Sent|To|Subject):\s", stripped, flags=re.I):
            break

        kept.append(line)

    return "\n".join(kept).strip()


def normalize_reply(text: str) -> str:
    text = strip_quoted_reply(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    # Approval command must be the only meaningful content in the new reply.
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def load_package() -> dict[str, Any]:
    if not PACKAGE_PATH.exists():
        raise FileNotFoundError(f"Missing {PACKAGE_PATH}")
    data = json.loads(PACKAGE_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("gate_a_package.json must be a JSON object.")
    concepts = data.get("image_concepts")
    if not isinstance(concepts, list) or len(concepts) != 5:
        raise ValueError("gate_a_package.json must contain exactly five image_concepts.")
    return data


def authorized_senders() -> set[str]:
    recipients = {
        x.strip().lower()
        for x in required_env("EMAIL_TO").split(",")
        if x.strip()
    }
    return recipients


def find_valid_approval(imap: imaplib.IMAP4_SSL, package: dict[str, Any]) -> tuple[int, str, str] | None:
    issue_date = str(package.get("date") or package.get("issue_date") or "").strip()
    expected_subject = "The Daily Duck — Story Approval"
    if issue_date:
        expected_subject += f" — {issue_date}"

    allowed = authorized_senders()

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

        if expected_subject not in subject:
            continue
        if allowed and sender not in allowed:
            continue

        normalized = normalize_reply(message_text(msg))
        print(f"Gate A candidate reply normalized as: {normalized!r}")

        match = VALID_RE.fullmatch(normalized)
        if match:
            return int(match.group(1)), sender, normalized

    return None


def main() -> int:
    package = load_package()

    # Idempotency: never overwrite an already-approved state for the same issue.
    if APPROVED_PATH.exists():
        existing = json.loads(APPROVED_PATH.read_text(encoding="utf-8"))
        if existing.get("state") == "APPROVED_STORY":
            print("APPROVED_STORY already exists. No change.")
            print("STATE: APPROVED_STORY")
            return 0

    gmail = required_env("GMAIL_ADDRESS")
    password = required_env("GMAIL_APP_PASSWORD")

    with imaplib.IMAP4_SSL("imap.gmail.com", 993) as imap:
        imap.login(gmail, password)
        imap.select("INBOX")
        print("Gmail login successful.")

        found = find_valid_approval(imap, package)

    if not found:
        print("No exact Gate A approval found.")
        print("Valid values are: 1 OK / 2 OK / 3 OK / 4 OK / 5 OK")
        print("STATE: WAITING_STORY_APPROVAL")
        return 0

    selected_number, sender, normalized = found
    selected_concept = package["image_concepts"][selected_number - 1]

    approved = dict(package)
    approved.update(
        {
            "state": "APPROVED_STORY",
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "approval_reply": normalized,
            "approval_sender": sender,
            "selected_image_concept_number": selected_number,
            "selected_image_concept": selected_concept,
        }
    )

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    APPROVED_PATH.write_text(
        json.dumps(approved, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"EXACT GATE A APPROVAL FOUND: {normalized}")
    print(f"Selected image concept: {selected_number}")
    print(f"Saved approved story to {APPROVED_PATH}")
    print("STATE: APPROVED_STORY")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
