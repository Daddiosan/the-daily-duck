#!/usr/bin/env python3
"""
The Daily Duck - Gate A story selection checker

Gate A valid replies:
    1
    2
    3
    4
    5

Only an exact single number is valid.

A valid reply:
1. selects ONE of the five completed editorial story proposals
2. saves that story as APPROVED_STORY
3. preserves the selected story's JP/EN/Duck/X copy
4. does NOT select an image concept
5. does NOT publish anything

Image concepts are generated only AFTER this Gate A selection.
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

APPROVED_PATH = (
    STATE_DIR / "approved_story.json"
)


# Exact single number only.
VALID_APPROVAL_RE = re.compile(
    r"^[1-5]$"
)


def required_env(name: str) -> str:

    value = os.getenv(
        name,
        "",
    ).strip()

    if not value:

        raise RuntimeError(
            "Missing required environment "
            f"variable: {name}"
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

    normalized_text = (
        text
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )

    for line in normalized_text.split("\n"):

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
            r"^\d{4}年\d{1,2}月\d{1,2}日"
            r".+<.+>:$",
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

    fresh_reply = strip_quoted_reply(
        text
    )

    lines = [
        line.strip()
        for line in fresh_reply.splitlines()
        if line.strip()
    ]

    if not lines:
        return ""

    # Joining all meaningful text intentionally
    # makes anything other than one exact digit invalid.
    #
    # "3"       -> valid
    # "3 OK"    -> invalid
    # "3 yes"   -> invalid
    # "OK"      -> invalid

    normalized = " ".join(lines)

    normalized = re.sub(
        r"\s+",
        " ",
        normalized,
    )

    return normalized.strip()


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

    if not isinstance(
        data,
        dict,
    ):

        raise ValueError(
            "gate_a_package.json "
            "must contain a JSON object."
        )

    story_options = data.get(
        "story_options"
    )

    if not isinstance(
        story_options,
        list,
    ):

        raise ValueError(
            "gate_a_package.json does not "
            "contain story_options."
        )

    if len(story_options) != 5:

        raise ValueError(
            "Gate A requires exactly "
            "five story options."
        )

    for index, story in enumerate(
        story_options,
        start=1,
    ):

        if not isinstance(
            story,
            dict,
        ):

            raise ValueError(
                f"Story option {index} "
                "must be an object."
            )

        candidate_number = (
            story.get(
                "candidate_number"
            )
        )

        if candidate_number != index:

            raise ValueError(
                "Story candidate numbering "
                "must be exactly 1-5."
            )

    return data


def authorized_senders() -> set[str]:

    email_to = required_env(
        "EMAIL_TO"
    )

    return {
        address.strip().lower()
        for address
        in email_to.split(",")
        if address.strip()
    }


def get_issue_date(
    package: dict[str, Any],
) -> str:

    return str(
        package.get("issue_date")
        or package.get("date")
        or ""
    ).strip()


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

    if (
        existing.get("state")
        != "APPROVED_STORY"
    ):

        return False

    current_date = get_issue_date(
        package
    )

    existing_date = str(
        existing.get("issue_date")
        or existing.get("date")
        or ""
    ).strip()

    if (
        current_date
        and existing_date
    ):

        return (
            current_date
            == existing_date
        )

    current_options = package.get(
        "story_options"
    )

    selected_story = existing.get(
        "selected_story"
    )

    if (
        isinstance(
            current_options,
            list,
        )
        and isinstance(
            selected_story,
            dict,
        )
    ):

        selected_id = str(
            selected_story.get(
                "id",
                "",
            )
        )

        for story in current_options:

            if (
                isinstance(story, dict)
                and str(
                    story.get(
                        "id",
                        "",
                    )
                )
                == selected_id
            ):

                return True

    return False


def find_valid_approval(
    imap: imaplib.IMAP4_SSL,
    package: dict[str, Any],
) -> tuple[int, str, str] | None:

    issue_date = get_issue_date(
        package
    )

    expected_subject = (
        "The Daily Duck — "
        "Choose Today's Story"
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

    message_ids = (
        data[0].split()
    )

    for msg_id in reversed(
        message_ids[-250:]
    ):

        status, payload = (
            imap.fetch(
                msg_id,
                "(RFC822)",
            )
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

        msg = (
            email.message_from_bytes(
                payload[0][1]
            )
        )

        subject = decode_mime(
            msg.get("Subject")
        )

        sender = parseaddr(
            decode_mime(
                msg.get("From")
            )
        )[1].lower()

        if (
            expected_subject
            not in subject
        ):
            continue

        if (
            allowed_senders
            and sender
            not in allowed_senders
        ):
            continue

        body = message_text(
            msg
        )

        normalized = normalize_reply(
            body
        )

        print(
            "Gate A candidate reply "
            f"normalized as: "
            f"{normalized!r}"
        )

        if not (
            VALID_APPROVAL_RE
            .fullmatch(
                normalized
            )
        ):

            continue

        story_number = int(
            normalized
        )

        return (
            story_number,
            sender,
            normalized,
        )

    return None


def save_approved_story(
    package: dict[str, Any],
    story_number: int,
    sender: str,
    normalized_reply: str,
) -> None:

    story_options = package[
        "story_options"
    ]

    selected_story = dict(
        story_options[
            story_number - 1
        ]
    )

    issue_date = get_issue_date(
        package
    )

    # Store the selected story in both a clear
    # selected_story field and compatibility fields
    # used by downstream Daily Duck scripts.

    approved: dict[str, Any] = {
        "date": issue_date,
        "issue_date": issue_date,

        "phase": 2,

        "state":
            "APPROVED_STORY",

        "approved_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "approval_reply":
            normalized_reply,

        "approval_sender":
            sender,

        "selected_story_number":
            story_number,

        "selected_story":
            selected_story,

        "recommended_story":
            selected_story,

        "recommended_id":
            selected_story.get(
                "id"
            ),

        "jp_copy":
            selected_story.get(
                "jp_copy",
                "",
            ),

        "en_copy":
            selected_story.get(
                "en_copy",
                "",
            ),

        "duck_name":
            selected_story.get(
                "duck_name",
                "",
            ),

        "duck_jp":
            selected_story.get(
                "duck_jp",
                "",
            ),

        "duck_en":
            selected_story.get(
                "duck_en",
                "",
            ),

        "x_jp":
            selected_story.get(
                "x_jp",
                "",
            ),

        "x_en":
            selected_story.get(
                "x_en",
                "",
            ),

        "source":
            selected_story.get(
                "source",
                "",
            ),

        "source_url":
            selected_story.get(
                "url",
                "",
            ),

        # Preserve original Gate A information.
        "gate_a_package":
            package,

        # Image concepts intentionally do not
        # exist yet. They are generated next.
        "image_concept_status":
            "NOT_GENERATED",
    }

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


def main() -> int:

    package = load_package()

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

    if not found:

        print(
            "No exact Gate A "
            "story selection found."
        )

        print(
            "Valid values are:"
        )

        print(
            "1 / 2 / 3 / 4 / 5"
        )

        print(
            "Anything else is invalid."
        )

        print(
            "STATE: "
            "WAITING_STORY_SELECTION"
        )

        return 0

    (
        story_number,
        sender,
        normalized_reply,
    ) = found

    save_approved_story(
        package=package,
        story_number=story_number,
        sender=sender,
        normalized_reply=normalized_reply,
    )

    print(
        "EXACT GATE A "
        "STORY SELECTION FOUND: "
        f"{normalized_reply}"
    )

    print(
        "Selected story: "
        f"{story_number}"
    )

    print(
        "Saved approved story to "
        f"{APPROVED_PATH}"
    )

    print(
        "Next stage: "
        "generate five image concepts."
    )

    print(
        "STATE: APPROVED_STORY"
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
