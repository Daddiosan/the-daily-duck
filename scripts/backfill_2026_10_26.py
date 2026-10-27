#!/usr/bin/env python3
"""One-day, isolated recovery helpers for the 2026-10-26 design email.

This module deliberately cannot operate on any other issue date.  It reuses
the normal design-email formatter and attachment logic, while adding the
backfill-only story identity checks, a Sent-mail idempotency guard, and a
non-sending dry run.
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import re
import sys
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Any

try:
    from scripts import send_design_approval_email as design_email
    from scripts.approval_token import (
        approval_token_digest,
        extract_approval_token,
        generate_approval_token,
    )
except ImportError:
    import send_design_approval_email as design_email  # type: ignore[no-redef]
    from approval_token import (  # type: ignore[no-redef]
        approval_token_digest,
        extract_approval_token,
        generate_approval_token,
    )


TARGET_ISSUE_DATE = "2026-10-26"
TARGET_BATCH = 1
TARGET_STORY_NUMBER = 1
TARGET_STORY_ID = 33
TARGET_TITLE_EN = "Tiny Snakes Roll Four Times Faster Than They Slither"
TARGET_TITLE_JA = "小さなヘビ、這うより4倍速く転がる"
TARGET_SOURCE_URL = (
    "https://www.sciencedaily.com/releases/2026/10/261023023718.htm"
)
TARGET_GATE_A_DIGEST = (
    "83191fdde9b2f9848073d312f421940ab1a2d795d70ccbee5ca78294c6e70a1c"
)
SOURCE_RUN_ID = 40353742118
SOURCE_ARTIFACT_ID = 12665810318
BACKFILL_APPROVAL_ID = "BACKFILL-2026-10-26-DESIGN-B1"
DESIGN_SUBJECT_PREFIX = (
    "The Daily Duck — Choose Image + Title — 2026-10-26 — Batch 1"
)
DEFAULT_APPROVED_PATH = Path("automation_state/approved_story.json")
DEFAULT_OPTIONS_PATH = Path("automation_state/design_options.json")


class BackfillValidationError(RuntimeError):
    """Backfill evidence is missing, inconsistent, or outside its scope."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackfillValidationError(f"Cannot read valid JSON from {path}.") from exc
    if not isinstance(data, dict):
        raise BackfillValidationError(f"{path} must contain a JSON object.")
    return data


def _issue_date(data: dict[str, Any]) -> str:
    value = data.get("issue_date") or data.get("date")
    return value.strip() if isinstance(value, str) else ""


def _selected_story(approved: dict[str, Any]) -> dict[str, Any]:
    selected = approved.get("selected_story")
    if not isinstance(selected, dict):
        raise BackfillValidationError("selected_story is missing or invalid.")
    return selected


def validate_approved_story(approved: dict[str, Any]) -> dict[str, Any]:
    """Fail closed unless this is the exact verified 2026-10-26 selection."""

    if _issue_date(approved) != TARGET_ISSUE_DATE:
        raise BackfillValidationError("Backfill refuses any issue except 2026-10-26.")
    if approved.get("state") != "APPROVED_STORY":
        raise BackfillValidationError("Expected APPROVED_STORY state.")
    if approved.get("selected_story_number") != TARGET_STORY_NUMBER:
        raise BackfillValidationError("Expected selected story number 1.")
    if str(approved.get("approval_reply", "")).strip() != "1":
        raise BackfillValidationError("Expected the exact normalized Gate A reply 1.")

    selected = _selected_story(approved)
    expected = {
        "id": TARGET_STORY_ID,
        "candidate_number": TARGET_STORY_NUMBER,
        "title_en": TARGET_TITLE_EN,
        "title_ja": TARGET_TITLE_JA,
        "url": TARGET_SOURCE_URL,
    }
    for field, value in expected.items():
        if selected.get(field) != value:
            raise BackfillValidationError(
                f"Selected story {field} does not match the Gate A artifact."
            )

    package = approved.get("gate_a_package")
    if not isinstance(package, dict):
        raise BackfillValidationError("Embedded gate_a_package is missing.")
    if _issue_date(package) != TARGET_ISSUE_DATE:
        raise BackfillValidationError("Embedded Gate A package has the wrong date.")
    options = package.get("story_options")
    if not isinstance(options, list) or len(options) != 5:
        raise BackfillValidationError("Embedded Gate A package must have five stories.")
    if package.get("approval_token_digest") != TARGET_GATE_A_DIGEST:
        raise BackfillValidationError("Gate A approval token digest is not authoritative.")
    if options[TARGET_STORY_NUMBER - 1] != selected:
        raise BackfillValidationError("Selected story is not candidate 1 from Gate A.")
    return selected


def _decode_mime(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parts: list[str] = []
    for item, charset in decode_header(value):
        if isinstance(item, bytes):
            parts.append(item.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(item)
    return "".join(parts)


def _sent_mailbox(imap: imaplib.IMAP4_SSL) -> str:
    status, rows = imap.list()
    if status != "OK" or not rows:
        raise RuntimeError("Could not list Gmail mailboxes.")
    for row in rows:
        if not isinstance(row, bytes):
            continue
        decoded = row.decode("utf-8", errors="replace")
        if "\\Sent" not in decoded:
            continue
        quoted = re.search(r'"([^"]+)"\s*$', decoded)
        if quoted:
            return quoted.group(1)
        return decoded.rsplit(" ", 1)[-1]
    raise RuntimeError("Could not locate Gmail's Sent mailbox.")


def _plain_text(message: email.message.Message) -> str:
    parts: list[str] = []
    for part in message.walk():
        if part.get_content_type() != "text/plain":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(parts)


def _has_exact_attachments(message: email.message.Message) -> bool:
    expected = {f"DailyDuck_Image_{number}.png" for number in range(1, 4)}
    found: set[str] = set()
    for part in message.walk():
        filename = part.get_filename()
        if not filename:
            continue
        if part.get_content_type() != "image/png":
            return False
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes) or not payload:
            return False
        found.add(filename)
    return found == expected


def find_existing_design_email(
    imap: imaplib.IMAP4_SSL,
    *,
    sender: str,
    expected_recipients: set[str],
) -> dict[str, str] | None:
    """Return redacted evidence for a prior target email, if one exists."""

    mailbox = _sent_mailbox(imap)
    status, _ = imap.select(f'"{mailbox}"', readonly=True)
    if status != "OK":
        raise RuntimeError("Could not open Gmail's Sent mailbox.")
    status, data = imap.search(None, "SUBJECT", f'"{TARGET_ISSUE_DATE}"')
    if status != "OK" or not data:
        raise RuntimeError("Gmail Sent-mail search failed.")

    for msg_id in reversed(data[0].split()):
        status, payload = imap.fetch(
            msg_id,
            "(BODY.PEEK[])",
        )
        if status != "OK" or not payload or not isinstance(payload[0], tuple):
            continue
        message = email.message_from_bytes(payload[0][1])
        subject = _decode_mime(message.get("Subject"))
        from_address = parseaddr(_decode_mime(message.get("From")))[1].lower()
        if from_address != sender.lower():
            continue
        recipients = {
            address.lower()
            for _, address in getaddresses(message.get_all("To", []))
            if address
        }
        if recipients != {address.lower() for address in expected_recipients}:
            continue
        # Long RFC 2047 subjects can lose a cosmetic space where encoded and
        # ASCII header chunks meet.  Compare the fixed identity without
        # whitespace, then still require a structurally valid approval token.
        subject_identity = re.sub(r"\s+", "", subject)
        expected_identity = re.sub(
            r"\s+", "", DESIGN_SUBJECT_PREFIX + " — Approval Token "
        )
        if not subject_identity.startswith(expected_identity):
            continue
        token = extract_approval_token(subject)
        if token is None:
            continue
        body = _plain_text(message)
        required_body_markers = (
            BACKFILL_APPROVAL_ID,
            TARGET_TITLE_EN,
            TARGET_TITLE_JA,
            TARGET_SOURCE_URL,
            "IMAGE_NUMBER TITLE_NUMBER",
        )
        if any(marker not in body for marker in required_body_markers):
            continue
        if not _has_exact_attachments(message):
            continue
        return {
            "message_id": _decode_mime(message.get("Message-ID")),
            "date": _decode_mime(message.get("Date")),
            "subject_prefix": DESIGN_SUBJECT_PREFIX,
            "approval_token": token,
        }
    return None


def check_duplicate_via_gmail() -> dict[str, str] | None:
    gmail_address = design_email.required_env("GMAIL_ADDRESS")
    gmail_password = design_email.required_env("GMAIL_APP_PASSWORD")
    with imaplib.IMAP4_SSL("imap.gmail.com", 993) as imap:
        imap.login(gmail_address, gmail_password)
        return find_existing_design_email(
            imap,
            sender=gmail_address,
            expected_recipients=set(design_email.recipients_from_env()),
        )


def _final_selection_rule() -> dict[str, Any]:
    return {
        "image_numbers": [1, 2, 3],
        "title_numbers": [1, 2, 3],
        "format": "IMAGE_NUMBER TITLE_NUMBER",
        "example": "1 3",
        "next_3_command": "NEXT 3",
        "full_width_supported": True,
        "next_state": "READY_TO_PUBLISH",
    }


def _backfill_metadata() -> dict[str, Any]:
    return {
        "approval_id": BACKFILL_APPROVAL_ID,
        "target_issue_date": TARGET_ISSUE_DATE,
        "source_run_id": SOURCE_RUN_ID,
        "source_artifact_id": SOURCE_ARTIFACT_ID,
        "recovery_workflow_run_id": os.getenv("GITHUB_RUN_ID", ""),
        "recovery_repository": os.getenv("GITHUB_REPOSITORY", ""),
        "recovery_commit_sha": os.getenv("GITHUB_SHA", ""),
        "isolated": True,
        "publication_allowed": False,
    }


def verify_sent_email(options_path: Path = DEFAULT_OPTIONS_PATH) -> dict[str, str]:
    evidence = check_duplicate_via_gmail()
    if evidence is None:
        raise BackfillValidationError(
            "The 2026-10-26 design email was not found in Gmail Sent."
        )
    print("EMAIL_SENT_EVIDENCE: VERIFIED")
    print(f"Gmail Message-ID: {evidence['message_id'] or 'unavailable'}")
    print(f"Gmail Date: {evidence['date'] or 'unavailable'}")
    if options_path.exists():
        package = _load_json(options_path)
        _design_package_story(package)
        package["approval_token_digest"] = approval_token_digest(
            stage="DESIGN_SELECTION",
            issue_date=TARGET_ISSUE_DATE,
            batch=TARGET_BATCH,
            token=evidence["approval_token"],
        )
        package["final_email_subject_prefix"] = DESIGN_SUBJECT_PREFIX
        package["state"] = "WAITING_FINAL_SELECTION"
        package.setdefault("final_email_sent_at", datetime.now(timezone.utc).isoformat())
        package["final_selection_rule"] = _final_selection_rule()
        metadata = _backfill_metadata()
        metadata["gmail_message_id"] = evidence["message_id"]
        metadata["gmail_date"] = evidence["date"]
        package["backfill"] = metadata
        options_path.write_text(
            json.dumps(package, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return evidence


def _design_package_story(package: dict[str, Any]) -> dict[str, Any]:
    if _issue_date(package) != TARGET_ISSUE_DATE:
        raise BackfillValidationError("Design package has the wrong issue date.")
    if int(package.get("preview_batch_number", 0) or 0) != TARGET_BATCH:
        raise BackfillValidationError("Design package must be batch 1.")
    approved = package.get("approved_story")
    if not isinstance(approved, dict):
        raise BackfillValidationError("Design package has no approved story state.")
    return validate_approved_story(approved)


def build_backfill_email(
    package: dict[str, Any],
    approval_token: str,
) -> tuple[str, str, list[dict[str, Any]]]:
    selected = _design_package_story(package)
    _, _, previews = design_email.validate_package(package)
    subject, body = design_email.build_email(package, approval_token)
    story_section = f"""
==================================================
SELECTED STORY / 選択済み記事
==================================================

ENGLISH MASTER
{selected['title_en']}

JAPANESE TRANSLATION
{selected['title_ja']}

SOURCE
{selected['url']}

BACKFILL APPROVAL ID
{BACKFILL_APPROVAL_ID}
""".strip()
    marker = "Gate A is complete."
    if marker not in body:
        raise BackfillValidationError("Base design email marker is missing.")
    body = body.replace(marker, story_section + "\n\n\n" + marker, 1)
    return subject, body, previews


def dry_run_email(options_path: Path) -> None:
    package = _load_json(options_path)
    subject, body, previews = build_backfill_email(package, "A" * 32)
    if BACKFILL_APPROVAL_ID not in body or "IMAGE_NUMBER TITLE_NUMBER" not in body:
        raise BackfillValidationError("Dry-run email is missing required instructions.")
    print("DRY_RUN: PASS")
    print(f"Issue date: {TARGET_ISSUE_DATE}")
    print("Subject identity: target issue and batch verified")
    print(f"Selected story number: {TARGET_STORY_NUMBER}")
    print(f"Attached images: {len(previews)}")
    print("Concepts: 3")
    print("Titles: 3")
    print("Approval token: REDACTED")
    print(f"Rendered body characters: {len(body)}")
    print(f"Rendered subject characters: {len(subject)}")


def send_email(options_path: Path) -> str:
    duplicate = check_duplicate_via_gmail()
    if duplicate is not None:
        print("EMAIL_SENT: NO")
        print("EMAIL_ALREADY_EXISTS: YES")
        print(f"Existing Message-ID: {duplicate['message_id'] or 'unavailable'}")
        return "ALREADY_SENT"

    package = _load_json(options_path)
    approval_token = generate_approval_token()
    subject, body, previews = build_backfill_email(package, approval_token)
    package["approval_token_digest"] = approval_token_digest(
        stage="DESIGN_SELECTION",
        issue_date=TARGET_ISSUE_DATE,
        batch=TARGET_BATCH,
        token=approval_token,
    )
    package["final_email_subject_prefix"] = DESIGN_SUBJECT_PREFIX
    package.pop("final_email_subject", None)
    package.pop("email_subject", None)

    message = design_email.create_message(subject, body)
    design_email.attach_preview_images(message, previews)
    recipient_count = design_email.send_message(message)

    package["state"] = "WAITING_FINAL_SELECTION"
    package["final_email_sent_at"] = datetime.now(timezone.utc).isoformat()
    package["final_selection_rule"] = _final_selection_rule()
    package["backfill"] = _backfill_metadata()
    design_email.save_package(package)

    print("EMAIL_SENT: YES")
    print(f"Recipients: {recipient_count}")
    print("Attached images: 3")
    print("Concepts: 3")
    print("Titles: 3")
    print("Approval token: REDACTED")
    print("STATE: WAITING_FINAL_SELECTION")
    return "SENT"


def _append_output(path: Path | None, values: dict[str, str]) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "validate-approved",
            "check-duplicate",
            "dry-run-email",
            "send-email",
            "verify-sent",
        ),
    )
    parser.add_argument("--approved-path", type=Path, default=DEFAULT_APPROVED_PATH)
    parser.add_argument("--options-path", type=Path, default=DEFAULT_OPTIONS_PATH)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)

    if args.action == "validate-approved":
        validate_approved_story(_load_json(args.approved_path))
        print("APPROVED_STORY_VERIFIED: YES")
        print(f"TARGET_ISSUE_DATE: {TARGET_ISSUE_DATE}")
        print(f"SELECTED_STORY_NUMBER: {TARGET_STORY_NUMBER}")
        return 0
    if args.action == "check-duplicate":
        duplicate = check_duplicate_via_gmail()
        should_send = "false" if duplicate is not None else "true"
        _append_output(args.github_output, {"should_send": should_send})
        print(f"DESIGN_EMAIL_ALREADY_SENT: {'YES' if duplicate else 'NO'}")
        if duplicate:
            print(f"Existing Message-ID: {duplicate['message_id'] or 'unavailable'}")
        return 0
    if args.action == "dry-run-email":
        dry_run_email(args.options_path)
        return 0
    if args.action == "send-email":
        result = send_email(args.options_path)
        _append_output(args.github_output, {"send_result": result})
        return 0
    if args.action == "verify-sent":
        verify_sent_email(args.options_path)
        return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise

