#!/usr/bin/env python3
"""
The Daily Duck - Image Concept Selection Checker

Valid replies:
    1
    2
    3
    4
    5

Only an exact single digit is valid.

A valid reply selects ONE of the five image concepts and writes:

    automation_state/approved_image_concept.json

with:

    state = APPROVED_IMAGE_CONCEPT

The selected concept then becomes the ONLY visual direction used
for final image candidate generation.
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


CONCEPTS_PATH = Path(
    "automation_state/image_concepts.json"
)

STATE_DIR = Path(
    "automation_state"
)

APPROVED_CONCEPT_PATH = (
    STATE_DIR / "approved_image_concept.json"
)

VALID_SELECTION_RE = re.compile(
    r"^[1-5]$"
)


def required_env(name: str) -> str:
    value = os.getenv(
        name,
        "",
    ).strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


def decode_mime(
    value: str | None,
) -> str:

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


def message_text(
    msg: email.message.Message,
) -> str:

    parts: list[str] = []

    if msg.is_multipart():

        for part in msg.walk():

            content_type = (
                part.get_content_type()
            )

            disposition = str(
                part.get(
                    "Content-Disposition",
                    "",
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


def strip_quoted_reply(
    text: str,
) -> str:

    kept: list[str] = []

    normalized = (
        text
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )

    for line in normalized.split("\n"):

        stripped = line.strip()

        if stripped.startswith(">"):
            break

        if re.match(
            r"^On .+ wrote:$",
            stripped,
            flags=re.IGNORECASE,
        ):
            break

        if re.match(
            r"^\d{4}年\d{1,2}月\d{1,2}日.+<.+>:$",
            stripped,
        ):
            break

        if stripped in (
            "-----Original Message-----",
            "-----元のメッセージ-----",
        ):
            break

        if re.match(
            r"^(From|Sent|To|Subject):\s",
            stripped,
            flags=re.IGNORECASE,
        ):
            break

        kept.append(line)

    return "\n".join(
        kept
    ).strip()


def normalize_reply(
    text: str,
) -> str:

    fresh = strip_quoted_reply(
        text
    )

    lines = [
        line.strip()
        for line in fresh.splitlines()
        if line.strip()
    ]

    if not lines:
        return ""

    normalized = " ".join(
        lines
    )

    normalized = re.sub(
        r"\s+",
        " ",
        normalized,
    )

    return normalized.strip()


def load_concepts() -> dict[str, Any]:

    if not CONCEPTS_PATH.exists():

        raise FileNotFoundError(
            f"Missing {CONCEPTS_PATH}"
        )

    data = json.loads(
        CONCEPTS_PATH.read_text(
            encoding="utf-8"
        )
    )

    if not isinstance(
        data,
        dict,
    ):
        raise ValueError(
            "image_concepts.json must contain a JSON object."
        )

    state = str(
        data.get("state", "")
    ).strip().upper()

    if state != "IMAGE_CONCEPT_REVIEW":
        raise ValueError(
            "Image concept selection requires "
            f"IMAGE_CONCEPT_REVIEW, got {state!r}."
        )

    concepts = data.get(
        "concepts"
    )

    if (
        not isinstance(concepts, list)
        or len(concepts) != 5
    ):
        raise ValueError(
            "Exactly five image concepts are required."
        )

    for index, concept in enumerate(
        concepts,
        start=1,
    ):

        if not isinstance(
            concept,
            dict,
        ):
            raise ValueError(
                f"Concept {index} must be an object."
            )

        if int(
            concept.get("number", 0)
        ) != index:
            raise ValueError(
                "Concept numbering must be exactly 1-5."
            )

    return data


def authorized_senders() -> set[str]:

    raw = required_env(
        "EMAIL_TO"
    )

    return {
        address.strip().lower()
        for address in raw.split(",")
        if address.strip()
    }


def get_issue_date(
    data: dict[str, Any],
) -> str:

    return str(
        data.get("issue_date")
        or data.get("date")
        or ""
    ).strip()


def same_issue_already_selected(
    data: dict[str, Any],
) -> bool:

    if not APPROVED_CONCEPT_PATH.exists():
        return False

    try:
        existing = json.loads(
            APPROVED_CONCEPT_PATH.read_text(
                encoding="utf-8"
            )
        )

    except Exception:
        return False

    if (
        existing.get("state")
        != "APPROVED_IMAGE_CONCEPT"
    ):
        return False

    current_date = get_issue_date(
        data
    )

    existing_date = str(
        existing.get("issue_date")
        or existing.get("date")
        or ""
    ).strip()

    if current_date and existing_date:
        return current_date == existing_date

    return False


def find_valid_selection(
    imap: imaplib.IMAP4_SSL,
    data: dict[str, Any],
) -> tuple[int, str, str] | None:

    issue_date = get_issue_date(
        data
    )

    expected_subject = (
        "The Daily Duck — Image Concept Selection"
    )

    if issue_date:
        expected_subject += (
            f" — {issue_date}"
        )

    allowed_senders = (
        authorized_senders()
    )

    status, result = imap.search(
        None,
        "ALL",
    )

    if status != "OK":
        raise RuntimeError(
            "IMAP search failed."
        )

    message_ids = result[0].split()

    for msg_id in reversed(
        message_ids[-250:]
    ):

        status, payload = imap.fetch(
            msg_id,
            "(RFC822)",
        )

        if status != "OK":
            continue

        if (
            not payload
            or not isinstance(
                payload[0],
                tuple,
            )
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

        if expected_subject not in subject:
            continue

        if (
            allowed_senders
            and sender
            not in allowed_senders
        ):
            continue

        normalized = normalize_reply(
            message_text(msg)
        )

        print(
            "Image concept candidate reply "
            f"normalized as: {normalized!r}"
        )

        if not VALID_SELECTION_RE.fullmatch(
            normalized
        ):
            continue

        return (
            int(normalized),
            sender,
            normalized,
        )

    return None


def save_selected_concept(
    data: dict[str, Any],
    concept_number: int,
    sender: str,
    normalized_reply: str,
) -> None:

    concepts = data[
        "concepts"
    ]

    selected_concept = dict(
        concepts[
            concept_number - 1
        ]
    )

    issue_date = get_issue_date(
        data
    )

    result: dict[str, Any] = {
        "date":
            issue_date,

        "issue_date":
            issue_date,

        "state":
            "APPROVED_IMAGE_CONCEPT",

        "selected_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "selection_reply":
            normalized_reply,

        "selection_sender":
            sender,

        "selected_image_concept_number":
            concept_number,

        "selected_image_concept":
            selected_concept,

        "story":
            data.get("story", {}),

        "source_image_concepts_path":
            str(CONCEPTS_PATH),

        "final_image_generation_rule":
            (
                "Generate exactly five final image candidates "
                "from ONLY this selected concept."
            ),

        "regeneration_rule":
            (
                "NEXT 5 generates five new final images "
                "while preserving this same selected concept."
            ),
    }

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    APPROVED_CONCEPT_PATH.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:

    data = load_concepts()

    if same_issue_already_selected(
        data
    ):
        print(
            "This image concept issue is already selected."
        )

        print(
            "STATE: APPROVED_IMAGE_CONCEPT"
        )

        return 0

    gmail_address = required_env(
        "GMAIL_ADDRESS"
    )

    gmail_password = required_env(
        "GMAIL_APP_PASSWORD"
    )

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

        found = find_valid_selection(
            imap,
            data,
        )

    if not found:

        print(
            "No exact image concept selection found."
        )

        print(
            "Valid values: 1 / 2 / 3 / 4 / 5"
        )

        print(
            "STATE: IMAGE_CONCEPT_REVIEW"
        )

        return 0

    (
        concept_number,
        sender,
        normalized_reply,
    ) = found

    save_selected_concept(
        data=data,
        concept_number=concept_number,
        sender=sender,
        normalized_reply=normalized_reply,
    )

    print(
        "EXACT IMAGE CONCEPT SELECTION FOUND: "
        f"{normalized_reply}"
    )

    print(
        "Selected image concept: "
        f"{concept_number}"
    )

    print(
        "Saved selected concept to "
        f"{APPROVED_CONCEPT_PATH}"
    )

    print(
        "STATE: APPROVED_IMAGE_CONCEPT"
    )

    return 0


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
