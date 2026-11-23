#!/usr/bin/env python3
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

try:
    from scripts.approval_token import (
        approval_token_digest,
        generate_approval_token,
        subject_with_approval_token,
    )
except ImportError:
    from approval_token import (  # type: ignore[no-redef]
        approval_token_digest,
        generate_approval_token,
        subject_with_approval_token,
    )


OPTIONS_PATH = Path(
    "automation_state/design_options.json"
)

EXPECTED_CONCEPT_COUNT = 3
EXPECTED_TITLE_COUNT = 3
EXPECTED_PREVIEW_COUNT = 3


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Required file not found: {path}"
        )

    data = json.loads(
        path.read_text(encoding="utf-8")
    )

    if not isinstance(data, dict):
        raise ValueError(
            f"{path} must contain a JSON object."
        )

    return data


def save_package(
    package: dict[str, Any],
) -> None:
    OPTIONS_PATH.write_text(
        json.dumps(
            package,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def issue_date_from(
    package: dict[str, Any],
) -> str:
    issue_date = first_text(
        package.get("issue_date"),
        package.get("date"),
    )

    if not issue_date:
        raise ValueError(
            "design_options.json is missing issue_date."
        )

    return issue_date


def validate_package(
    package: dict[str, Any],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    state = first_text(
        package.get("state")
    ).upper()

    if state not in (
        "DESIGN_OPTIONS_READY",
        "WAITING_FINAL_SELECTION",
    ):
        raise ValueError(
            "Expected DESIGN_OPTIONS_READY or "
            f"WAITING_FINAL_SELECTION; got {state!r}."
        )

    concepts = package.get("image_concepts")
    titles = package.get("title_ideas")
    previews = package.get("design_previews")

    if (
        not isinstance(concepts, list)
        or len(concepts) != EXPECTED_CONCEPT_COUNT
    ):
        raise ValueError(
            "Exactly 3 image concepts are required."
        )

    if (
        not isinstance(titles, list)
        or len(titles) != EXPECTED_TITLE_COUNT
    ):
        raise ValueError(
            "Exactly 3 title ideas are required."
        )

    if (
        not isinstance(previews, list)
        or len(previews) != EXPECTED_PREVIEW_COUNT
    ):
        raise ValueError(
            "Exactly 3 real image previews are required."
        )

    for index in range(3):
        expected = index + 1

        concept = concepts[index]
        preview = previews[index]
        title = titles[index]

        if int(concept.get("number", 0) or 0) != expected:
            raise ValueError(
                f"Concept {expected} has invalid number."
            )

        if int(preview.get("number", 0) or 0) != expected:
            raise ValueError(
                f"Preview {expected} has invalid number."
            )

        if int(title.get("number", 0) or 0) != expected:
            raise ValueError(
                f"Title {expected} has invalid number."
            )

        image_path = Path(
            first_text(
                preview.get("image_path")
            )
        )

        if not image_path.exists():
            raise FileNotFoundError(
                f"Preview image missing: {image_path}"
            )

    return concepts, titles, previews


def format_concept(
    concept: dict[str, Any],
) -> str:
    number = concept["number"]

    return f"""
IMAGE / CONCEPT {number}
画像 / コンセプト {number}

ENGLISH MASTER
----------------------------------------

{first_text(concept.get("title_en"))}

{first_text(concept.get("concept_en"))}

Composition:
{first_text(concept.get("composition_en"))}


JAPANESE REVIEW
----------------------------------------

{first_text(concept.get("title_ja"))}

{first_text(concept.get("concept_ja"))}

構図:
{first_text(concept.get("composition_ja"))}
""".strip()


def format_title(
    title: dict[str, Any],
) -> str:
    return f"""
TITLE {title["number"]}

{first_text(title.get("title"))}

日本語での意味・ニュアンス:
{first_text(title.get("meaning_ja"))}
""".strip()


def build_email(
    package: dict[str, Any],
    approval_token: str,
    *,
    legacy_reply: bool = False,
) -> tuple[str, str]:
    issue_date = issue_date_from(
        package
    )

    concepts, titles, previews = (
        validate_package(package)
    )

    options = "\n\n".join(
        f"{format_concept(concept)}\n\n"
        f"{format_title(title)}\n"
        f"添付：DailyDuck_Image_{preview['number']}.png"
        for concept, title, preview in zip(concepts, titles, previews)
    )

    batch_number = int(
        package.get("preview_batch_number", 1)
        or 1
    )

    subject_prefix = (
        "The Daily Duck — "
        "Choose Image + Title — "
        f"{issue_date} — Batch {batch_number}"
    )
    subject = subject_with_approval_token(subject_prefix, approval_token)

    story = package.get("approved_story_compact")
    if not isinstance(story, dict):
        approved = package.get("approved_story")
        story = approved.get("selected_story", approved) if isinstance(approved, dict) else {}
    if not isinstance(story, dict):
        story = {}
    title_ja = first_text(story.get("title_ja"))
    title_en = first_text(story.get("title_en"), story.get("title"))
    story_titles = "\n".join(dict.fromkeys(title for title in (title_ja, title_en) if title))
    if not story_titles and not legacy_reply:
        raise ValueError("The approved story title is missing.")

    story_section = f"選んだ記事\n{story_titles}\n\n" if not legacy_reply else ""
    reply = (
        "画像番号 + スペース + タイトル番号（IMAGE_NUMBER TITLE_NUMBER）\n例：1 3"
        if legacy_reply else
        "希望の番号（1〜3）だけ返信してください。例：2（全角の２でも可）\n"
        "同じ番号の画像と掲載タイトルが一緒に決まります。"
    )
    body = (
        f"The Daily Duck — 画像と掲載タイトルの選択 — {issue_date}\n\n"
        f"{story_section}Gate A is complete.\n\n"
        f"{options}\n\n"
        f"{reply}\n"
        "3枚とも選ばない場合は NEXT 3（全角のＮＥＸＴ ３でも可）で画像を作り直せます。"
    )

    return subject, body


def recipients_from_env() -> list[str]:
    recipients = [
        item.strip()
        for item in required_env(
            "EMAIL_TO"
        ).split(",")
        if item.strip()
    ]

    if not recipients:
        raise RuntimeError(
            "EMAIL_TO contains no valid recipients."
        )

    return recipients


def create_message(
    subject: str,
    body: str,
) -> EmailMessage:
    message = EmailMessage()

    message["From"] = required_env(
        "GMAIL_ADDRESS"
    )

    message["To"] = ", ".join(
        recipients_from_env()
    )

    message["Subject"] = subject

    message.set_content(body)

    return message


def attach_preview_images(
    message: EmailMessage,
    previews: list[dict[str, Any]],
) -> None:
    for preview in previews:
        number = int(
            preview.get("number", 0)
            or 0
        )

        path = Path(
            first_text(
                preview.get("image_path")
            )
        )

        mime_type, _ = mimetypes.guess_type(
            path.name
        )

        if mime_type and "/" in mime_type:
            maintype, subtype = mime_type.split(
                "/",
                1,
            )
        else:
            maintype = "image"
            subtype = "png"

        message.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=f"DailyDuck_Image_{number}.png",
        )


def send_message(
    message: EmailMessage,
) -> int:
    gmail_address = required_env(
        "GMAIL_ADDRESS"
    )

    gmail_password = required_env(
        "GMAIL_APP_PASSWORD"
    )

    recipients = recipients_from_env()

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        timeout=30,
    ) as smtp:
        smtp.login(
            gmail_address,
            gmail_password,
        )

        smtp.send_message(
            message
        )

    return len(recipients)


def main() -> int:
    package = load_json(
        OPTIONS_PATH
    )

    issue_date = issue_date_from(package)
    batch_number = int(package.get("preview_batch_number", 1) or 1)
    approval_token = generate_approval_token()
    subject_prefix = (
        "The Daily Duck — Choose Image + Title — "
        f"{issue_date} — Batch {batch_number}"
    )
    package["approval_token_digest"] = approval_token_digest(
        stage="DESIGN_SELECTION",
        issue_date=issue_date,
        batch=batch_number,
        token=approval_token,
    )
    package["final_email_subject_prefix"] = subject_prefix
    package.pop("final_email_subject", None)
    package.pop("email_subject", None)

    subject, body = build_email(
        package,
        approval_token,
    )

    _, _, previews = validate_package(
        package
    )

    message = create_message(
        subject,
        body,
    )

    attach_preview_images(
        message,
        previews,
    )

    recipient_count = send_message(
        message
    )

    package["state"] = "WAITING_FINAL_SELECTION"
    package["final_email_sent_at"] = datetime.now(
        timezone.utc
    ).isoformat()

    package["final_selection_rule"] = {
        "image_numbers": [1, 2, 3],
        "title_numbers": [1, 2, 3],
        "format": "OPTION_NUMBER",
        "example": "2",
        "next_3_command": "NEXT 3",
        "full_width_supported": True,
        "next_state": "READY_TO_PUBLISH",
    }

    save_package(
        package
    )

    print(
        "Design selection email sent."
    )
    print(
        "Attached images: 3"
    )
    print(
        "Concepts: 3"
    )
    print(
        "Titles: 3"
    )
    print(
        "Reply example: 2"
    )
    print(
        "Full-width: ２"
    )
    print(
        "Regeneration: NEXT 3 / ＮＥＸＴ ３"
    )
    print(
        f"Recipients: {recipient_count}"
    )
    print("Subject: tokenized design approval subject generated")
    print(
        "STATE: WAITING_FINAL_SELECTION"
    )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )
        raise
