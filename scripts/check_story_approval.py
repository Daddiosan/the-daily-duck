#!/usr/bin/env python3
"""
The Daily Duck - Gate A approval checker

Gate A valid replies:
    1 OK
    2 OK
    3 OK
    4 OK
    5 OK

Plain "OK" is NOT valid.

A valid reply approves:
1. the story/copy
2. one of the five image concepts

Then writes:
    automation_state/approved_story.json

with:
    state = APPROVED_STORY
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


# ============================================================
# Paths
# ============================================================

PACKAGE_PATH = Path("gate_a_package.json")

STATE_DIR = Path("automation_state")

APPROVED_PATH = STATE_DIR / "approved_story.json"


# ============================================================
# Valid Gate A command
# ============================================================

VALID_APPROVAL_RE = re.compile(
    r"^([1-5])\s+OK$",
    re.IGNORECASE,
)


# ============================================================
# Environment
# ============================================================

def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


# ============================================================
# MIME helpers
# ============================================================

def decode_mime(value: str | None) -> str:
    if not value:
        return ""

    try:
        return str(
            make_header(
                decode_header(value)
            )
        )

    except Exception:
        return value


# ============================================================
# Extract plain text from email
# ============================================================

def message_text(
    msg: email.message.Message,
) -> str:

    parts: list[str] = []

    if msg.is_multipart():

        for part in msg.walk():

            content_type = part.get_content_type()

            disposition = str(
                part.get(
                    "Content-Disposition",
                    ""
                )
            ).lower()

            if "attachment" in disposition:
                continue

            if content_type != "text/plain":
                continue

            payload = part.get_payload(
                decode=True
            )

            if payload is None:
                continue

            charset = (
                part.get_content_charset()
                or "utf-8"
            )

            parts.append(
                payload.decode(
                    charset,
                    errors="replace",
                )
            )

    else:

        payload = msg.get_payload(
            decode=True
        )

        if payload is not None:

            charset = (
                msg.get_content_charset()
                or "utf-8"
            )

            parts.append(
                payload.decode(
                    charset,
                    errors="replace",
                )
            )

    return "\n".join(parts)


# ============================================================
# Remove quoted original email
# ============================================================

def strip_quoted_reply(text: str) -> str:

    kept: list[str] = []

    normalized_text = (
        text
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )

    for line in normalized_text.split("\n"):

        stripped = line.strip()

        # Gmail / common quote
        if stripped.startswith(">"):
            break

        # English Gmail
        if re.match(
            r"^On .+ wrote:$",
            stripped,
            flags=re.IGNORECASE,
        ):
            break

        # Japanese Gmail style
        if re.match(
            r"^\d{4}年\d{1,2}月\d{1,2}日.+<.+>:$",
            stripped,
        ):
            break

        # Original message separators
        if stripped in (
            "-----Original Message-----",
            "-----元のメッセージ-----",
        ):
            break

        # Mail headers from quoted message
        if re.match(
            r"^(From|Sent|To|Subject):\s",
            stripped,
            flags=re.IGNORECASE,
        ):
            break

        kept.append(line)

    return "\n".join(kept).strip()


# ============================================================
# Normalize reply
# ============================================================

def normalize_reply(text: str) -> str:

    fresh_reply = strip_quoted_reply(text)

    lines = [
        line.strip()
        for line in fresh_reply.splitlines()
        if line.strip()
    ]

    if not lines:
        return ""

    # All meaningful new-reply text is joined.
    #
    # Examples:
    #
    # "3 OK"     -> "3 OK"
    # "3   OK"   -> "3 OK"
    # "OK"       -> "OK"      (invalid)
    # "3 OK yes" -> invalid
    #

    normalized = " ".join(lines)

    normalized = re.sub(
        r"\s+",
        " ",
        normalized,
    )

    return normalized.strip()


# ============================================================
# Load Gate A package
# ============================================================

def load_package() -> dict[str, Any]:

    if not PACKAGE_PATH.exists():

        raise FileNotFoundError(
            f"Missing {PACKAGE_PATH}"
        )

    data = json.loads(
        PACKAGE_PATH.read_text(
            encoding="utf-8"
        )
    )

    if not isinstance(data, dict):

        raise ValueError(
            "gate_a_package.json "
            "must contain a JSON object."
        )

    concepts = data.get(
        "image_concepts"
    )

    if not isinstance(concepts, list):

        raise ValueError(
            "gate_a_package.json does not "
            "contain image_concepts."
        )

    if len(concepts) != 5:

        raise ValueError(
            "Gate A requires exactly "
            "five image concepts."
        )

    return data


# ============================================================
# Authorized reply addresses
# ============================================================

def authorized_senders() -> set[str]:

    email_to = required_env(
        "EMAIL_TO"
    )

    return {
        address.strip().lower()
        for address in email_to.split(",")
        if address.strip()
    }


# ============================================================
# Issue date
# ============================================================

def get_issue_date(
    package: dict[str, Any],
) -> str:

    issue_date = str(
        package.get("date")
        or package.get("issue_date")
        or ""
    ).strip()

    return issue_date


# ============================================================
# Check whether existing approval is for SAME issue
# ============================================================

def same_issue_already_approved(
    package: dict[str, Any],
) -> bool:

    if not APPROVED_PATH.exists():
        return False

    try:

        existing = json.loads(
            APPROVED_PATH.read_text(
                encoding="utf-8"
            )
        )

    except Exception:

        return False

    if existing.get("state") != "APPROVED_STORY":
        return False

    current_date = get_issue_date(
        package
    )

    existing_date = str(
        existing.get("date")
        or existing.get("issue_date")
        or ""
    ).strip()

    # --------------------------------------------------------
    # Important Phase 2 behavior
    #
    # Same day's approval:
    #   do not process again
    #
    # Old approval from another day:
    #   DO NOT block today's Gate A approval
    # --------------------------------------------------------

    if current_date and existing_date:

        return (
            current_date
            == existing_date
        )

    # If date is unavailable, compare a few stable story fields.
    current_story = package.get(
        "recommended_story"
    )

    existing_story = existing.get(
        "recommended_story"
    )

    if (
        current_story
        and existing_story
        and current_story == existing_story
    ):
        return True

    return False


# ============================================================
# Find exact Gate A approval
# ============================================================

def find_valid_approval(
    imap: imaplib.IMAP4_SSL,
    package: dict[str, Any],
) -> tuple[int, str, str] | None:

    issue_date = get_issue_date(
        package
    )

    expected_subject = (
        "The Daily Duck — Story Approval"
    )

    if issue_date:

        expected_subject += (
            f" — {issue_date}"
        )

    allowed_senders = (
        authorized_senders()
    )

    status, data = imap.search(
        None,
        "ALL",
    )

    if status != "OK":

        raise RuntimeError(
            "IMAP search failed."
        )

    message_ids = data[0].split()

    # Check newest messages first.
    #
    # Limit prevents scanning an enormous mailbox.
    for msg_id in reversed(
        message_ids[-250:]
    ):

        status, payload = imap.fetch(
            msg_id,
            "(RFC822)",
        )

        if status != "OK":
            continue

        if not payload:
            continue

        if not isinstance(
            payload[0],
            tuple,
        ):
            continue

        msg = email.message_from_bytes(
            payload[0][1]
        )

        subject = decode_mime(
            msg.get("Subject")
        )

        sender = parseaddr(
            decode_mime(
                msg.get("From")
            )
        )[1].lower()

        # ----------------------------------------------------
        # Subject must belong to current Gate A email.
        # ----------------------------------------------------

        if expected_subject not in subject:
            continue

        # ----------------------------------------------------
        # Only configured recipients may approve.
        # ----------------------------------------------------

        if (
            allowed_senders
            and sender
            not in allowed_senders
        ):
            continue

        body = message_text(msg)

        normalized = normalize_reply(
            body
        )

        print(
            "Gate A candidate reply "
            f"normalized as: {normalized!r}"
        )

        match = (
            VALID_APPROVAL_RE
            .fullmatch(normalized)
        )

        if not match:
            continue

        concept_number = int(
            match.group(1)
        )

        return (
            concept_number,
            sender,
            normalized,
        )

    return None


# ============================================================
# Save APPROVED_STORY
# ============================================================

def save_approved_story(
    package: dict[str, Any],
    concept_number: int,
    sender: str,
    normalized_reply: str,
) -> None:

    concepts = package[
        "image_concepts"
    ]

    selected_concept = concepts[
        concept_number - 1
    ]

    # Preserve the complete approved Gate A package.
    approved = dict(package)

    approved.update(
        {
            "state": "APPROVED_STORY",

            "approved_at":
                datetime.now(
                    timezone.utc
                ).isoformat(),

            "approval_reply":
                normalized_reply,

            "approval_sender":
                sender,

            "selected_image_concept_number":
                concept_number,

            "selected_image_concept":
                selected_concept,
        }
    )

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    APPROVED_PATH.write_text(
        json.dumps(
            approved,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


# ============================================================
# Main
# ============================================================

def main() -> int:

    package = load_package()

    # --------------------------------------------------------
    # Do not process the SAME issue twice.
    #
    # But an APPROVED_STORY from yesterday or another issue
    # must NOT block today's approval.
    # --------------------------------------------------------

    if same_issue_already_approved(
        package
    ):

        print(
            "This Gate A issue is "
            "already approved."
        )

        print(
            "STATE: APPROVED_STORY"
        )

        return 0

    gmail_address = required_env(
        "GMAIL_ADDRESS"
    )

    gmail_password = required_env(
        "GMAIL_APP_PASSWORD"
    )

    # --------------------------------------------------------
    # Gmail
    # --------------------------------------------------------

    with imaplib.IMAP4_SSL(
        "imap.gmail.com",
        993,
    ) as imap:

        imap.login(
            gmail_address,
            gmail_password,
        )

        print(
            "Gmail login successful."
        )

        imap.select(
            "INBOX"
        )

        found = find_valid_approval(
            imap,
            package,
        )

    # --------------------------------------------------------
    # No valid approval
    # --------------------------------------------------------

    if not found:

        print(
            "No exact Gate A "
            "approval found."
        )

        print(
            "Valid values are:"
        )

        print(
            "1 OK / 2 OK / 3 OK / "
            "4 OK / 5 OK"
        )

        print(
            "Plain OK is invalid."
        )

        print(
            "STATE: "
            "WAITING_STORY_APPROVAL"
        )

        return 0

    # --------------------------------------------------------
    # Valid approval
    # --------------------------------------------------------

    (
        concept_number,
        sender,
        normalized_reply,
    ) = found

    save_approved_story(
        package=package,
        concept_number=concept_number,
        sender=sender,
        normalized_reply=normalized_reply,
    )

    print(
        "EXACT GATE A APPROVAL FOUND: "
        f"{normalized_reply}"
    )

    print(
        "Selected image concept: "
        f"{concept_number}"
    )

    print(
        "Saved approved story to "
        f"{APPROVED_PATH}"
    )

    print(
        "STATE: APPROVED_STORY"
    )

    return 0


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":

    try:

        raise SystemExit(
            main()
        )

    except Exception as exc:

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        raise
