#!/usr/bin/env python3
from __future__ import annotations

import base64
import email
import hashlib
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

from openai import OpenAI


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

PREVIEW_ROOT = Path(
    "automation_images/design_previews"
)

FINAL_RE = re.compile(
    r"^([1-3])\s+([1-3])$"
)

NEXT_3_RE = re.compile(
    r"^NEXT\s+3$",
    flags=re.IGNORECASE,
)

OPENAI_IMAGE_MODEL = (
    os.getenv("OPENAI_IMAGE_MODEL") or ""
).strip() or "gpt-image-2"

OPENAI_IMAGE_SIZE = (
    os.getenv("OPENAI_IMAGE_SIZE") or ""
).strip() or "1536x1024"

OPENAI_IMAGE_QUALITY = (
    os.getenv("OPENAI_IMAGE_QUALITY") or ""
).strip() or "medium"

MAX_DUPLICATE_RETRIES = 2


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


def first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def normalize_command(
    value: str,
) -> str:
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

    return "\n".join(chunks)


def fresh_reply_lines(
    text: str,
) -> list[str]:
    fresh: list[str] = []

    for line in (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
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

        fresh.append(stripped)

    return fresh


def extract_command(
    text: str,
) -> tuple[
    str,
    int | None,
    int | None,
    str,
] | None:
    commands = []

    for line in fresh_reply_lines(text):
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
                    int(final_match.group(1)),
                    int(final_match.group(2)),
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


def save_result(
    action: str,
    **extra: Any,
) -> None:
    RESULT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    RESULT_PATH.write_text(
        json.dumps(
            {
                "action": action,
                "checked_at": datetime.now(
                    timezone.utc
                ).isoformat(),
                **extra,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def allowed_senders() -> set[str]:
    return {
        x.strip().lower()
        for x in required_env(
            "EMAIL_TO"
        ).split(",")
        if x.strip()
    }


def find_reply(
    subject: str,
) -> tuple[
    str,
    int | None,
    int | None,
    str,
    str,
] | None:
    allowed = allowed_senders()

    with imaplib.IMAP4_SSL(
        "imap.gmail.com",
        993,
    ) as imap:
        imap.login(
            required_env("GMAIL_ADDRESS"),
            required_env("GMAIL_APP_PASSWORD"),
        )

        imap.select("INBOX")

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
                msg.get("Subject")
            )

            sender = parseaddr(
                decode_mime(
                    msg.get("From")
                )
            )[1].lower()

            if subject not in msg_subject:
                continue

            if (
                allowed
                and sender not in allowed
            ):
                continue

            command = extract_command(
                message_text(msg)
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

    return None


def build_next_image_prompt(
    package: dict[str, Any],
    concept: dict[str, Any],
    number: int,
    batch_number: int,
    retry: int,
) -> str:
    approved_story = package.get(
        "approved_story_compact"
    )

    if not isinstance(
        approved_story,
        dict,
    ):
        raise ValueError(
            "approved_story_compact is missing."
        )

    retry_note = ""

    if retry > 0:
        retry_note = f"""
RETRY {retry}:
Make this new rendition clearly different from the previous batch,
while preserving this exact concept.
""".strip()

    return f"""
Create ONE fresh publishable landscape hero image
for The Daily Duck.

This is regenerated batch {batch_number},
concept {number} of 3.

APPROVED STORY:
{first_text(
    approved_story.get("title_en"),
    approved_story.get("title"),
)}

CONCEPT TITLE:
{first_text(concept.get("title_en"))}

CONCEPT:
{first_text(concept.get("concept_en"))}

COMPOSITION:
{first_text(concept.get("composition_en"))}

PRODUCTION DIRECTION:
{first_text(concept.get("generation_prompt_en"))}

{retry_note}

Preserve the exact concept, but produce a fresh execution.
Keep the friendly yellow Daily Duck mascot consistent.
No readable text, numbers, logos, watermarks or UI.
Do not invent unsupported facts.
""".strip()


def generate_next_three(
    package: dict[str, Any],
) -> int:
    concepts = package.get(
        "image_concepts"
    )

    if (
        not isinstance(concepts, list)
        or len(concepts) != 3
    ):
        raise ValueError(
            "Exactly 3 image concepts are required for NEXT 3."
        )

    issue_date = first_text(
        package.get("issue_date")
    )

    if not issue_date:
        raise ValueError(
            "issue_date is missing."
        )

    previous_batch = int(
        package.get(
            "preview_batch_number",
            1,
        )
        or 1
    )

    batch_number = previous_batch + 1

    out_dir = (
        PREVIEW_ROOT
        / issue_date
        / f"batch_{batch_number:02d}"
    )

    out_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    client = OpenAI(
        api_key=required_env(
            "OPENAI_API_KEY"
        )
    )

    old_previews = package.get(
        "design_previews",
        [],
    )

    old_hashes = {
        str(item.get("sha256"))
        for item in old_previews
        if isinstance(item, dict)
        and item.get("sha256")
    }

    new_hashes: set[str] = set()
    previews = []

    for concept in concepts:
        number = int(
            concept.get("number", 0)
            or 0
        )

        chosen_bytes = None
        chosen_hash = ""
        chosen_prompt = ""

        for retry in range(
            0,
            MAX_DUPLICATE_RETRIES + 1,
        ):
            prompt = build_next_image_prompt(
                package,
                concept,
                number,
                batch_number,
                retry,
            )

            result = client.images.generate(
                model=OPENAI_IMAGE_MODEL,
                prompt=prompt,
                n=1,
                size=OPENAI_IMAGE_SIZE,
                quality=OPENAI_IMAGE_QUALITY,
                output_format="png",
            )

            if (
                not result.data
                or not result.data[0].b64_json
            ):
                raise RuntimeError(
                    f"No image returned for concept {number}."
                )

            image_bytes = base64.b64decode(
                result.data[0].b64_json
            )

            digest = hashlib.sha256(
                image_bytes
            ).hexdigest()

            if (
                digest not in old_hashes
                and digest not in new_hashes
            ):
                chosen_bytes = image_bytes
                chosen_hash = digest
                chosen_prompt = prompt
                break

        if chosen_bytes is None:
            raise RuntimeError(
                f"Could not generate a fresh unique image "
                f"for concept {number}."
            )

        new_hashes.add(
            chosen_hash
        )

        path = (
            out_dir
            / f"preview_{number}.png"
        )

        path.write_bytes(
            chosen_bytes
        )

        previews.append(
            {
                "number": number,
                "concept_number": number,
                "concept_title_en": first_text(
                    concept.get("title_en")
                ),
                "concept_title_ja": first_text(
                    concept.get("title_ja")
                ),
                "image_path": path.as_posix(),
                "mime_type": "image/png",
                "provider": "OpenAI",
                "model": OPENAI_IMAGE_MODEL,
                "size": OPENAI_IMAGE_SIZE,
                "quality": OPENAI_IMAGE_QUALITY,
                "sha256": chosen_hash,
                "generation_prompt": chosen_prompt,
                "alt_en": first_text(
                    concept.get("alt_en")
                ),
                "alt_ja": first_text(
                    concept.get("alt_ja")
                ),
            }
        )

    package[
        "previous_design_previews"
    ] = old_previews

    package[
        "design_previews"
    ] = previews

    package[
        "preview_batch_number"
    ] = batch_number

    package[
        "preview_batch_path"
    ] = out_dir.as_posix()

    package[
        "state"
    ] = "DESIGN_OPTIONS_READY"

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
        "NEXT_3_GENERATED",
        issue_date=issue_date,
        preview_batch_number=batch_number,
    )

    print(
        "NEXT 3 generated."
    )
    print(
        "Three concepts preserved."
    )
    print(
        "One fresh image regenerated for each concept."
    )
    print(
        "STATE: DESIGN_OPTIONS_READY"
    )

    return 0


def finalize_selection(
    package: dict[str, Any],
    image_number: int,
    title_number: int,
    sender: str,
    normalized: str,
) -> int:
    concepts = package.get(
        "image_concepts"
    )

    titles = package.get(
        "title_ideas"
    )

    previews = package.get(
        "design_previews"
    )

    if (
        not isinstance(concepts, list)
        or len(concepts) != 3
    ):
        raise ValueError(
            "Exactly 3 image concepts are required."
        )

    if (
        not isinstance(titles, list)
        or len(titles) != 3
    ):
        raise ValueError(
            "Exactly 3 title ideas are required."
        )

    if (
        not isinstance(previews, list)
        or len(previews) != 3
    ):
        raise ValueError(
            "Exactly 3 real image previews are required."
        )

    selected_concept = next(
        (
            item
            for item in concepts
            if isinstance(item, dict)
            and int(
                item.get("number", 0)
                or 0
            ) == image_number
        ),
        None,
    )

    selected_preview = next(
        (
            item
            for item in previews
            if isinstance(item, dict)
            and int(
                item.get("number", 0)
                or 0
            ) == image_number
        ),
        None,
    )

    selected_title = next(
        (
            item
            for item in titles
            if isinstance(item, dict)
            and int(
                item.get("number", 0)
                or 0
            ) == title_number
        ),
        None,
    )

    if not isinstance(
        selected_concept,
        dict,
    ):
        raise ValueError(
            f"Concept {image_number} not found."
        )

    if not isinstance(
        selected_preview,
        dict,
    ):
        raise ValueError(
            f"Image {image_number} not found."
        )

    if not isinstance(
        selected_title,
        dict,
    ):
        raise ValueError(
            f"Title {title_number} not found."
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
            f"Selected image not found: {source_image}"
        )

    issue_date = first_text(
        package.get("issue_date")
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

    approved = dict(approved)

    approved[
        "selected_image_concept_number"
    ] = image_number

    approved[
        "selected_image_concept"
    ] = selected_concept

    approved[
        "selected_title_number"
    ] = title_number

    approved[
        "selected_title"
    ] = first_text(
        selected_title.get("title")
    )

    READY_PATH.write_text(
        json.dumps(
            {
                "state": "READY_TO_PUBLISH",
                "issue_date": issue_date,
                "ready_at": datetime.now(
                    timezone.utc
                ).isoformat(),

                "gate_a_approved_story": approved,

                "selected_image_concept_number":
                    image_number,

                "selected_image_concept":
                    selected_concept,

                "selected_image_number":
                    image_number,

                "selected_title_number":
                    title_number,

                "selected_title":
                    first_text(
                        selected_title.get("title")
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
                    "primary_language": "en",
                    "canonical_language": "en",
                    "translation_language": "ja",
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    package[
        "state"
    ] = "DESIGN_SELECTED_READY_TO_PUBLISH"

    package[
        "selected_image_concept_number"
    ] = image_number

    package[
        "selected_image_concept"
    ] = selected_concept

    package[
        "selected_image_number"
    ] = image_number

    package[
        "selected_title_number"
    ] = title_number

    package[
        "selected_title"
    ] = first_text(
        selected_title.get("title")
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
        concept_number=image_number,
        title_number=title_number,
        canonical_image_path=(
            canonical_path.as_posix()
        ),
        normalized_reply=normalized,
    )

    print(
        f"FINAL IMAGE / CONCEPT: {image_number}"
    )
    print(
        f"FINAL TITLE: {title_number}"
    )
    print(
        f"CANONICAL: {canonical_path}"
    )
    print(
        "STATE: READY_TO_PUBLISH"
    )

    return 0


def main() -> int:
    if not OPTIONS_PATH.exists():
        save_result(
            "WAIT",
            reason="design_options.json missing",
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
            "design_options.json must contain an object."
        )

    state = first_text(
        package.get("state")
    ).upper()

    if state == "WAITING_FINAL_SELECTION":
        subject = first_text(
            package.get("final_email_subject"),
            package.get("email_subject"),
        )

        if not subject:
            raise ValueError(
                "final_email_subject is missing."
            )

        found = find_reply(
            subject
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
                "No valid final reply found."
            )
            print(
                "Valid: 1 3 / １ ３ / NEXT 3 / ＮＥＸＴ ３"
            )
            print(
                "STATE: WAITING_FINAL_SELECTION"
            )

            return 0

        (
            command_type,
            image_number,
            title_number,
            sender,
            normalized,
        ) = found

        if command_type == "NEXT_3":
            return generate_next_three(
                package
            )

        assert image_number is not None
        assert title_number is not None

        return finalize_selection(
            package,
            image_number,
            title_number,
            sender,
            normalized,
        )

    if state == "DESIGN_OPTIONS_READY":
        save_result(
            "SEND_DESIGN_EMAIL",
            issue_date=package.get(
                "issue_date"
            ),
        )

        print(
            "Design images exist but approval email "
            "has not been marked waiting yet."
        )
        print(
            "STATE: DESIGN_OPTIONS_READY"
        )

        return 0

    if (
        state
        == "DESIGN_SELECTED_READY_TO_PUBLISH"
    ):
        save_result(
            "ALREADY_SELECTED",
            issue_date=package.get(
                "issue_date"
            ),
        )

        print(
            "STATE: ALREADY_SELECTED"
        )

        return 0

    save_result(
        "WAIT",
        state=state,
        issue_date=package.get(
            "issue_date"
        ),
    )

    print(
        f"No action for state {state!r}."
    )
    print(
        "STATE: WAIT"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
