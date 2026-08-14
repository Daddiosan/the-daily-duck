#!/usr/bin/env python3
from __future__ import annotations

import email
import imaplib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path
from typing import Any

STATE_DIR = Path("automation_state")
OPTIONS_PATH = STATE_DIR / "design_options.json"
READY_PATH = STATE_DIR / "ready_to_publish.json"
RESULT_PATH = STATE_DIR / "design_selection_result.json"
CANONICAL_DIR = Path("automation_images") / "canonical"

VALID_RE = re.compile(r"^([1-3])\s+([1-3])$")


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
    chunks: list[str] = []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
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
    return "\n".join(chunks)


def fresh_reply_lines(text: str) -> list[str]:
    """Return only the newly written portion of a mail reply.

    Gmail/iPhone replies can append signatures and quoted history.  We stop at
    the quoted-history boundary, but do not require the whole fresh section to
    contain only the approval command.
    """
    fresh: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        stripped = line.strip()

        # Quoted / previous-message boundaries.
        if stripped.startswith(">"):
            break
        if re.match(r"^On .+ wrote:$", stripped, flags=re.I):
            break
        if re.match(r"^.+ wrote:$", stripped, flags=re.I):
            break
        if stripped in (
            "-----Original Message-----",
            "-----元のメッセージ-----",
            "----- 引用元メッセージ -----",
            "---------- Forwarded message ---------",
        ):
            break
        if re.match(r"^(From|Sent|To|Subject):\s", stripped, flags=re.I):
            break

        fresh.append(stripped)
    return fresh


def extract_selection(text: str) -> tuple[int, int, str] | None:
    """Find one standalone `1 1` .. `3 3` command in the fresh reply.

    A signature such as "Sent from my iPhone" is allowed.  Commands appearing
    only in quoted history are ignored.  If multiple different commands appear
    in the fresh section, reject the message as ambiguous.
    """
    matches: list[tuple[int, int, str]] = []

    for line in fresh_reply_lines(text):
        candidate = re.sub(r"[\u00a0\t]+", " ", line).strip()
        match = VALID_RE.fullmatch(candidate)
        if match:
            matches.append(
                (int(match.group(1)), int(match.group(2)), candidate)
            )

    if not matches:
        return None

    unique = {(a, b) for a, b, _ in matches}
    if len(unique) != 1:
        return None

    return matches[0]


def save_result(action: str, **extra: Any) -> None:
    payload = {
        "action": action,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    package = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    package_state = package.get("state")

    # Scheduled checks keep running every 15 minutes. Once this design has
    # already been selected, return a harmless no-op instead of failing.
    if package_state != "WAITING_DESIGN_SELECTION":
        save_result(
            "ALREADY_SELECTED",
            state=package_state,
            issue_date=package.get("issue_date"),
        )
        print(f"Design selection already completed: {package_state!r}")
        print("STATE: ALREADY_SELECTED")
        return 0

    subject = str(package.get("email_subject", "")).strip()
    if not subject:
        raise ValueError("design_options.json is missing email_subject.")

    allowed = {
        x.strip().lower()
        for x in required_env("EMAIL_TO").split(",")
        if x.strip()
    }

    found = None
    with imaplib.IMAP4_SSL("imap.gmail.com", 993) as imap:
        imap.login(
            required_env("GMAIL_ADDRESS"),
            required_env("GMAIL_APP_PASSWORD"),
        )
        imap.select("INBOX")
        status, data = imap.search(None, "ALL")
        if status != "OK":
            raise RuntimeError("IMAP search failed.")

        for msg_id in reversed(data[0].split()[-250:]):
            status, payload = imap.fetch(msg_id, "(RFC822)")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                continue

            msg = email.message_from_bytes(payload[0][1])
            msg_subject = decode_mime(msg.get("Subject"))
            sender = parseaddr(decode_mime(msg.get("From")))[1].lower()

            if subject not in msg_subject:
                continue
            if allowed and sender not in allowed:
                continue

            selection = extract_selection(message_text(msg))
            if selection:
                image_number, title_number, command = selection
                found = (
                    image_number,
                    title_number,
                    sender,
                    command,
                )
                break

    if found is None:
        save_result("WAIT")
        print("No valid 1 1 .. 3 3 reply found.")
        print("STATE: WAITING_DESIGN_SELECTION")
        return 0

    image_number, title_number, sender, normalized = found

    concepts = package["image_concepts"]
    titles = package["title_ideas"]
    previews = package["design_previews"]

    selected_concept = next(
        x for x in concepts if int(x["number"]) == image_number
    )
    selected_title = next(
        x for x in titles if int(x["number"]) == title_number
    )
    selected_preview = next(
        x for x in previews if int(x["number"]) == image_number
    )

    source_image = Path(selected_preview["image_path"])
    if not source_image.exists():
        raise FileNotFoundError(
            f"Selected preview image does not exist: {source_image}"
        )

    issue_date = str(package.get("issue_date", "")).strip()
    CANONICAL_DIR.mkdir(parents=True, exist_ok=True)
    suffix = source_image.suffix.lower() or ".jpg"
    canonical_path = CANONICAL_DIR / f"{issue_date}{suffix}"
    shutil.copy2(source_image, canonical_path)

    approved = package.get("approved_story")
    if not isinstance(approved, dict):
        raise ValueError("approved_story is missing.")

    # Preserve selection in approved story for website/X compatibility.
    approved = dict(approved)
    approved["selected_image_concept_number"] = image_number
    approved["selected_image_concept"] = selected_concept
    approved["selected_title_number"] = title_number
    approved["selected_title"] = selected_title["title"]

    READY_PATH.write_text(
        json.dumps(
            {
                "state": "READY_TO_PUBLISH",
                "issue_date": issue_date,
                "ready_at": datetime.now(timezone.utc).isoformat(),
                "gate_a_approved_story": approved,
                "selected_image_concept_number": image_number,
                "selected_image_concept": selected_concept,
                "selected_title_number": title_number,
                "selected_title": selected_title["title"],
                "selected_title_detail": selected_title,
                "selected_preview": selected_preview,
                "canonical_image_path": canonical_path.as_posix(),
                "design_approval_reply": normalized,
                "design_approval_sender": sender,
                "publish_started": False,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    package["state"] = "DESIGN_SELECTED_READY_TO_PUBLISH"
    package["selected_image_number"] = image_number
    package["selected_title_number"] = title_number
    package["selected_title"] = selected_title["title"]
    OPTIONS_PATH.write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    save_result(
        "READY_TO_PUBLISH",
        image_number=image_number,
        title_number=title_number,
        selected_title=selected_title["title"],
        canonical_image_path=canonical_path.as_posix(),
    )

    print(f"FINAL IMAGE: preview {image_number}")
    print(f"FINAL TITLE: title {title_number} — {selected_title['title']}")
    print(f"CANONICAL: {canonical_path}")
    print("STATE: READY_TO_PUBLISH")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
