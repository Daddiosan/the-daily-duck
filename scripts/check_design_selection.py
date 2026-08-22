#!/usr/bin/env python3
from __future__ import annotations

import email
import imaplib
import json
import os
import re
import shutil
import unicodedata
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path
from typing import Any


STATE_DIR = Path("automation_state")

OPTIONS_PATH = (
    STATE_DIR
    / "design_options.json"
)

READY_PATH = (
    STATE_DIR
    / "ready_to_publish.json"
)

RESULT_PATH = (
    STATE_DIR
    / "design_selection_result.json"
)

CANONICAL_DIR = (
    Path("automation_images")
    / "canonical"
)


# ============================================================
# Current Daily Duck selection rules
#
# Image concepts:
#   1 / 2 / 3
#
# Final image:
#   1 / 2 / 3
#
# Final title:
#   1 / 2 / 3
#
# Regeneration:
#   NEXT 3
#
# Full-width input is normalized before validation:
#
#   ２       -> 2
#   ３ １     -> 3 1
#   ３　１    -> 3 1
#   ＮＥＸＴ ３ -> NEXT 3
# ============================================================

CONCEPT_RE = re.compile(
    r"^([1-3])$"
)

FINAL_RE = re.compile(
    r"^([1-3])\s+([1-3])$"
)

NEXT_3_RE = re.compile(
    r"^NEXT\s+3$",
    flags=re.IGNORECASE,
)


# ============================================================
# Helpers
# ============================================================

def required_env(
    name: str,
) -> str:

    value = os.getenv(
        name,
        "",
    ).strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


def first_text(
    *values: Any,
) -> str:

    for value in values:

        if (
            isinstance(
                value,
                str,
            )
            and value.strip()
        ):
            return value.strip()

    return ""


def normalize_command(
    value: str,
) -> str:
    """
    Normalize full-width Japanese/Unicode input
    before command validation.

    Examples:

      １       -> 1
      ２       -> 2
      ３ １     -> 3 1
      ３　１    -> 3 1
      ＮＥＸＴ ３ -> NEXT 3
    """

    normalized = unicodedata.normalize(
        "NFKC",
        value,
    )

    normalized = re.sub(
        r"[\u00a0\t]+",
        " ",
        normalized,
    )

    normalized = re.sub(
        r"\s+",
        " ",
        normalized,
    )

    return normalized.strip()


# ============================================================
# Email parsing
# ============================================================

def decode_mime(
    value: str | None,
) -> str:

    if not value:
        return ""

    try:
        return str(
            make_header(
                decode_header(
                    value
                )
            )
        )

    except Exception:
        return value


def message_text(
    msg: email.message.Message,
) -> str:

    chunks: list[str] = []

    parts = (
        msg.walk()
        if msg.is_multipart()
        else [msg]
    )

    for part in parts:

        if (
            part.get_content_type()
            != "text/plain"
        ):
            continue

        if "attachment" in str(
            part.get(
                "Content-Disposition",
                "",
            )
        ).lower():
            continue

        payload = part.get_payload(
            decode=True
        )

        if payload is not None:

            chunks.append(
                payload.decode(
                    part.get_content_charset()
                    or "utf-8",
                    errors="replace",
                )
            )

    return "\n".join(
        chunks
    )


def fresh_reply_lines(
    text: str,
) -> list[str]:

    fresh: list[str] = []

    for line in (
        text.replace(
            "\r\n",
            "\n",
        )
        .replace(
            "\r",
            "\n",
        )
        .splitlines()
    ):

        stripped = line.strip()

        if stripped.startswith(">"):
            break

        if re.match(
            r"^On .+ wrote:$",
            stripped,
            flags=re.I,
        ):
            break

        if re.match(
            r"^.+ wrote:$",
            stripped,
            flags=re.I,
        ):
            break

        if stripped in (
            "-----Original Message-----",
            "-----元のメッセージ-----",
            "----- 引用元メッセージ -----",
            "---------- Forwarded message ---------",
        ):
            break

        if re.match(
            r"^(From|Sent|To|Subject):\s",
            stripped,
            flags=re.I,
        ):
            break

        fresh.append(
            stripped
        )

    return fresh


# ============================================================
# Concept selection
# ============================================================

def extract_concept_selection(
    text: str,
) -> tuple[
    int,
    str,
] | None:

    matches: list[
        tuple[
            int,
            str,
        ]
    ] = []

    for line in fresh_reply_lines(
        text
    ):

        candidate = normalize_command(
            line
        )

        match = CONCEPT_RE.fullmatch(
            candidate
        )

        if match:

            matches.append(
                (
                    int(
                        match.group(1)
                    ),
                    candidate,
                )
            )

    if not matches:
        return None

    unique = {
        number
        for number, _ in matches
    }

    if len(unique) != 1:
        return None

    return matches[0]


# ============================================================
# Final image/title selection + NEXT 3
# ============================================================

def extract_final_command(
    text: str,
) -> tuple[
    str,
    int | None,
    int | None,
    str,
] | None:

    commands: list[
        tuple[
            str,
            int | None,
            int | None,
            str,
        ]
    ] = []

    for line in fresh_reply_lines(
        text
    ):

        candidate = normalize_command(
            line
        )

        final_match = FINAL_RE.fullmatch(
            candidate
        )

        if final_match:

            commands.append(
                (
                    "FINAL",
                    int(
                        final_match.group(1)
                    ),
                    int(
                        final_match.group(2)
                    ),
                    candidate,
                )
            )

            continue

        if NEXT_3_RE.fullmatch(
            candidate
        ):

            commands.append(
                (
                    "NEXT_3",
                    None,
                    None,
                    "NEXT 3",
                )
            )

    if not commands:
        return None

    unique = {
        (
            command_type,
            image_number,
            title_number,
        )
        for (
            command_type,
            image_number,
            title_number,
            _,
        ) in commands
    }

    if len(unique) != 1:
        return None

    return commands[0]


# ============================================================
# Result state
# ============================================================

def save_result(
    action: str,
    **extra: Any,
) -> None:

    payload = {
        "action":
            action,

        "checked_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        **extra,
    }

    RESULT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    RESULT_PATH.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


# ============================================================
# Allowed senders
# ============================================================

def allowed_senders() -> set[str]:

    return {
        x.strip().lower()
        for x in required_env(
            "EMAIL_TO"
        ).split(",")
        if x.strip()
    }


# ============================================================
# Find reply
# ============================================================

def find_reply(
    subject: str,
    mode: str,
) -> tuple[
    Any,
    ...,
] | None:

    allowed = allowed_senders()

    with imaplib.IMAP4_SSL(
        "imap.gmail.com",
        993,
    ) as imap:

        imap.login(
            required_env(
                "GMAIL_ADDRESS"
            ),
            required_env(
                "GMAIL_APP_PASSWORD"
            ),
        )

        imap.select(
            "INBOX"
        )

        status, data = imap.search(
            None,
            "ALL",
        )

        if status != "OK":

            raise RuntimeError(
                "IMAP search failed."
            )

        for msg_id in reversed(
            data[0].split()[-250:]
        ):

            status, payload = imap.fetch(
                msg_id,
                "(RFC822)",
            )

            if (
                status != "OK"
                or not payload
                or not isinstance(
                    payload[0],
                    tuple,
                )
            ):
                continue

            msg = email.message_from_bytes(
                payload[0][1]
            )

            msg_subject = decode_mime(
                msg.get(
                    "Subject"
                )
            )

            sender = parseaddr(
                decode_mime(
                    msg.get(
                        "From"
                    )
                )
            )[1].lower()

            if subject not in msg_subject:
                continue

            if (
                allowed
                and sender not in allowed
            ):
                continue

            body = message_text(
                msg
            )

            if mode == "CONCEPT":

                selection = (
                    extract_concept_selection(
                        body
                    )
                )

                if selection:

                    (
                        number,
                        normalized,
                    ) = selection

                    return (
                        number,
                        sender,
                        normalized,
                    )

            elif mode == "FINAL":

                command = (
                    extract_final_command(
                        body
                    )
                )

                if command:

                    (
                        command_type,
                        image_number,
                        title_number,
                        normalized,
                    ) = command

                    return (
                        command_type,
                        image_number,
                        title_number,
                        sender,
                        normalized,
                    )

            else:

                raise ValueError(
                    f"Unknown reply mode: {mode}"
                )

    return None


# ============================================================
# Select image concept 1-3
# ============================================================

def select_concept(
    package: dict[str, Any],
) -> int:

    subject = first_text(
        package.get(
            "concept_email_subject"
        ),
        package.get(
            "email_subject"
        ),
    )

    if not subject:

        raise ValueError(
            "design_options.json is missing "
            "concept_email_subject."
        )

    found = find_reply(
        subject,
        "CONCEPT",
    )

    if found is None:

        save_result(
            "WAIT",
            stage="CONCEPT_SELECTION",
            issue_date=package.get(
                "issue_date"
            ),
        )

        print(
            "No valid concept reply found."
        )

        print(
            "Valid replies:"
        )

        print(
            "1 / 2 / 3"
        )

        print(
            "Full-width:"
        )

        print(
            "１ / ２ / ３"
        )

        print(
            "STATE: "
            "WAITING_CONCEPT_SELECTION"
        )

        return 0

    (
        concept_number,
        sender,
        normalized,
    ) = found

    concepts = package.get(
        "image_concepts"
    )

    if (
        not isinstance(
            concepts,
            list,
        )
        or len(
            concepts
        ) != 3
    ):

        raise ValueError(
            "Exactly 3 image concepts "
            "are required."
        )

    selected_concept = next(
        (
            item
            for item in concepts
            if isinstance(
                item,
                dict,
            )
            and int(
                item.get(
                    "number",
                    0,
                )
            )
            == concept_number
        ),
        None,
    )

    if not isinstance(
        selected_concept,
        dict,
    ):

        raise ValueError(
            f"Selected concept "
            f"{concept_number} "
            "was not found."
        )

    package[
        "selected_image_concept_number"
    ] = concept_number

    package[
        "selected_image_concept"
    ] = selected_concept

    package[
        "concept_approval_reply"
    ] = normalized

    package[
        "concept_approval_sender"
    ] = sender

    package[
        "concept_selected_at"
    ] = datetime.now(
        timezone.utc
    ).isoformat()

    package[
        "preview_batch_number"
    ] = 0

    package[
        "design_previews"
    ] = []

    package[
        "state"
    ] = "APPROVED_IMAGE_CONCEPT"

    OPTIONS_PATH.write_text(
        json.dumps(
            package,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    save_result(
        "APPROVED_IMAGE_CONCEPT",
        issue_date=package.get(
            "issue_date"
        ),
        concept_number=concept_number,
        concept_title=first_text(
            selected_concept.get(
                "title_en"
            ),
            selected_concept.get(
                "title_ja"
            ),
        ),
        sender=sender,
        normalized_reply=normalized,
    )

    print(
        "SELECTED CONCEPT: "
        f"{concept_number}"
    )

    print(
        "NORMALIZED REPLY: "
        f"{normalized}"
    )

    print(
        "STATE: "
        "APPROVED_IMAGE_CONCEPT"
    )

    return 0


# ============================================================
# NEXT 3
# ============================================================

def request_next_three(
    package: dict[str, Any],
    sender: str,
    normalized: str,
) -> int:

    selected_concept = package.get(
        "selected_image_concept"
    )

    if not isinstance(
        selected_concept,
        dict,
    ):

        raise ValueError(
            "NEXT 3 cannot run because "
            "selected_image_concept "
            "is missing."
        )

    package[
        "previous_design_previews"
    ] = package.get(
        "design_previews",
        [],
    )

    package[
        "design_previews"
    ] = []

    package[
        "next_3_requested_at"
    ] = datetime.now(
        timezone.utc
    ).isoformat()

    package[
        "next_3_reply"
    ] = normalized

    package[
        "next_3_sender"
    ] = sender

    package[
        "state"
    ] = "APPROVED_IMAGE_CONCEPT"

    OPTIONS_PATH.write_text(
        json.dumps(
            package,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    save_result(
        "NEXT_3_REQUESTED",
        issue_date=package.get(
            "issue_date"
        ),
        selected_concept_number=package.get(
            "selected_image_concept_number"
        ),
        current_batch=package.get(
            "preview_batch_number",
            0,
        ),
        sender=sender,
        normalized_reply=normalized,
    )

    print(
        "NEXT 3 REQUESTED."
    )

    print(
        "Selected concept "
        "remains LOCKED."
    )

    print(
        "STATE: "
        "APPROVED_IMAGE_CONCEPT"
    )

    return 0


# ============================================================
# Final selection
#
# image: 1-3
# title: 1-3
# ============================================================

def finalize_selection(
    package: dict[str, Any],
    image_number: int,
    title_number: int,
    sender: str,
    normalized: str,
) -> int:

    titles = package.get(
        "title_ideas"
    )

    previews = package.get(
        "design_previews"
    )

    selected_concept = package.get(
        "selected_image_concept"
    )

    if (
        not isinstance(
            titles,
            list,
        )
        or len(titles) != 3
    ):

        raise ValueError(
            "Exactly 3 title ideas "
            "are required."
        )

    if (
        not isinstance(
            previews,
            list,
        )
        or len(previews) != 3
    ):

        raise ValueError(
            "Exactly 3 real image "
            "previews are required."
        )

    if not isinstance(
        selected_concept,
        dict,
    ):

        raise ValueError(
            "selected_image_concept "
            "is missing."
        )

    selected_title = next(
        (
            item
            for item in titles
            if isinstance(
                item,
                dict,
            )
            and int(
                item.get(
                    "number",
                    0,
                )
            )
            == title_number
        ),
        None,
    )

    selected_preview = next(
        (
            item
            for item in previews
            if isinstance(
                item,
                dict,
            )
            and int(
                item.get(
                    "number",
                    0,
                )
            )
            == image_number
        ),
        None,
    )

    if not isinstance(
        selected_title,
        dict,
    ):

        raise ValueError(
            f"Title {title_number} "
            "was not found."
        )

    if not isinstance(
        selected_preview,
        dict,
    ):

        raise ValueError(
            f"Image {image_number} "
            "was not found."
        )

    source_image = Path(
        first_text(
            selected_preview.get(
                "image_path"
            )
        )
    )

    if not source_image.exists():

        raise FileNotFoundError(
            "Selected preview image "
            "does not exist: "
            f"{source_image}"
        )

    issue_date = first_text(
        package.get(
            "issue_date"
        )
    )

    if not issue_date:

        raise ValueError(
            "issue_date is missing."
        )

    CANONICAL_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    suffix = (
        source_image.suffix.lower()
        or ".png"
    )

    canonical_path = (
        CANONICAL_DIR
        / f"{issue_date}{suffix}"
    )

    shutil.copy2(
        source_image,
        canonical_path,
    )

    approved = package.get(
        "approved_story"
    )

    if not isinstance(
        approved,
        dict,
    ):

        raise ValueError(
            "approved_story is missing."
        )

    approved = dict(
        approved
    )

    approved[
        "selected_image_concept_number"
    ] = package.get(
        "selected_image_concept_number"
    )

    approved[
        "selected_image_concept"
    ] = selected_concept

    approved[
        "selected_title_number"
    ] = title_number

    approved[
        "selected_title"
    ] = first_text(
        selected_title.get(
            "title"
        )
    )

    ready_payload = {

        "state":
            "READY_TO_PUBLISH",

        "issue_date":
            issue_date,

        "ready_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "gate_a_approved_story":
            approved,

        "selected_image_concept_number":
            package.get(
                "selected_image_concept_number"
            ),

        "selected_image_concept":
            selected_concept,

        "selected_image_number":
            image_number,

        "selected_title_number":
            title_number,

        "selected_title":
            first_text(
                selected_title.get(
                    "title"
                )
            ),

        "selected_title_detail":
            selected_title,

        "selected_preview":
            selected_preview,

        "preview_batch_number":
            package.get(
                "preview_batch_number",
                1,
            ),

        "canonical_image_path":
            canonical_path.as_posix(),

        "design_approval_reply":
            normalized,

        "design_approval_sender":
            sender,

        "publish_started":
            False,

        "language_policy": {

            "primary_language":
                "en",

            "canonical_language":
                "en",

            "translation_language":
                "ja",
        },
    }

    READY_PATH.write_text(
        json.dumps(
            ready_payload,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    package[
        "state"
    ] = (
        "DESIGN_SELECTED_READY_TO_PUBLISH"
    )

    package[
        "selected_image_number"
    ] = image_number

    package[
        "selected_title_number"
    ] = title_number

    package[
        "selected_title"
    ] = first_text(
        selected_title.get(
            "title"
        )
    )

    package[
        "final_selection_reply"
    ] = normalized

    package[
        "final_selection_sender"
    ] = sender

    package[
        "final_selected_at"
    ] = datetime.now(
        timezone.utc
    ).isoformat()

    OPTIONS_PATH.write_text(
        json.dumps(
            package,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    save_result(
        "READY_TO_PUBLISH",
        issue_date=issue_date,
        image_number=image_number,
        title_number=title_number,
        selected_title=first_text(
            selected_title.get(
                "title"
            )
        ),
        canonical_image_path=(
            canonical_path.as_posix()
        ),
        preview_batch_number=package.get(
            "preview_batch_number",
            1,
        ),
        normalized_reply=normalized,
    )

    print(
        "FINAL IMAGE: "
        f"preview {image_number}"
    )

    print(
        "FINAL TITLE: "
        f"title {title_number} — "
        f"{selected_title['title']}"
    )

    print(
        "NORMALIZED REPLY: "
        f"{normalized}"
    )

    print(
        f"CANONICAL: "
        f"{canonical_path}"
    )

    print(
        "STATE: "
        "READY_TO_PUBLISH"
    )

    return 0


# ============================================================
# Check final selection
# ============================================================

def check_final_selection(
    package: dict[str, Any],
) -> int:

    subject = first_text(
        package.get(
            "final_email_subject"
        ),
        package.get(
            "email_subject"
        ),
    )

    if not subject:

        raise ValueError(
            "design_options.json "
            "is missing "
            "final_email_subject."
        )

    found = find_reply(
        subject,
        "FINAL",
    )

    if found is None:

        save_result(
            "WAIT",
            stage="FINAL_SELECTION",
            issue_date=package.get(
                "issue_date"
            ),
        )

        print(
            "No valid final reply "
            "or NEXT 3 command found."
        )

        print(
            "Valid examples:"
        )

        print(
            "2 1"
        )

        print(
            "２ １"
        )

        print(
            "NEXT 3"
        )

        print(
            "ＮＥＸＴ ３"
        )

        print(
            "STATE: "
            "WAITING_FINAL_SELECTION"
        )

        return 0

    (
        command_type,
        image_number,
        title_number,
        sender,
        normalized,
    ) = found

    if (
        command_type
        == "NEXT_3"
    ):

        return request_next_three(
            package,
            sender,
            normalized,
        )

    assert (
        image_number
        is not None
    )

    assert (
        title_number
        is not None
    )

    return finalize_selection(
        package,
        image_number,
        title_number,
        sender,
        normalized,
    )


# ============================================================
# Main
# ============================================================

def main() -> int:

    if not OPTIONS_PATH.exists():

        save_result(
            "WAIT",
            reason=(
                "design_options.json missing"
            ),
        )

        print(
            "No design_options.json yet."
        )

        print(
            "STATE: WAIT"
        )

        return 0

    package = json.loads(
        OPTIONS_PATH.read_text(
            encoding="utf-8"
        )
    )

    if not isinstance(
        package,
        dict,
    ):

        raise ValueError(
            "design_options.json "
            "must contain an object."
        )

    state = first_text(
        package.get(
            "state"
        )
    ).upper()

    # --------------------------------------------------------
    # Concept-selection reply expected
    # --------------------------------------------------------

    if (
        state
        == "WAITING_CONCEPT_SELECTION"
    ):

        return select_concept(
            package
        )

    # --------------------------------------------------------
    # Final image + title reply expected
    # --------------------------------------------------------

    if (
        state
        == "WAITING_FINAL_SELECTION"
    ):

        return check_final_selection(
            package
        )

    # --------------------------------------------------------
    # Workflow processing states
    # --------------------------------------------------------

    if state in (
        "CONCEPTS_READY",
        "APPROVED_IMAGE_CONCEPT",
        "DESIGN_PREVIEWS_READY",
    ):

        save_result(
            "WAIT",
            stage=state,
            issue_date=package.get(
                "issue_date"
            ),
        )

        print(
            "No email check required "
            f"in state {state}."
        )

        print(
            "STATE: WAIT"
        )

        return 0

    # --------------------------------------------------------
    # Already completed
    # --------------------------------------------------------

    if (
        state
        == "DESIGN_SELECTED_READY_TO_PUBLISH"
    ):

        save_result(
            "ALREADY_SELECTED",
            state=state,
            issue_date=package.get(
                "issue_date"
            ),
        )

        print(
            "Final design selection "
            "already completed."
        )

        print(
            "STATE: ALREADY_SELECTED"
        )

        return 0

    # --------------------------------------------------------
    # Unknown / inactive state
    # --------------------------------------------------------

    save_result(
        "WAIT",
        state=state,
        issue_date=package.get(
            "issue_date"
        ),
    )

    print(
        f"No action for state "
        f"{state!r}."
    )

    print(
        "STATE: WAIT"
    )

    return 0


if __name__ == "__main__":

    raise SystemExit(
        main()
    )
