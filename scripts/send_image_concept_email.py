#!/usr/bin/env python3
from __future__ import annotations

import json
import mimetypes
import os
import smtplib
import sys

from email.message import EmailMessage
from pathlib import Path
from typing import Any


CONCEPTS_PATH = Path(
    "automation_state/image_concepts.json"
)

EMAIL_PREVIEW_PATH = Path(
    "image_concept_email.txt"
)

SUBJECT_PREFIX = (
    "The Daily Duck — Image Selection"
)

CONCEPT_COUNT = 3


# ============================================================
# Environment
# ============================================================

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


# ============================================================
# JSON
# ============================================================

def load_json(
    path: Path,
) -> dict[str, Any]:

    if not path.exists():
        raise FileNotFoundError(
            f"Required file not found: {path}"
        )

    data = json.loads(
        path.read_text(
            encoding="utf-8"
        )
    )

    if not isinstance(
        data,
        dict,
    ):
        raise ValueError(
            f"{path} must contain a JSON object."
        )

    return data


# ============================================================
# Text helper
# ============================================================

def first_text(
    *values: Any,
) -> str:

    for value in values:

        if (
            isinstance(value, str)
            and value.strip()
        ):
            return value.strip()

    return ""


# ============================================================
# Validate image package
# ============================================================

def validate_package(
    data: dict[str, Any],
) -> list[dict[str, Any]]:

    state = str(
        data.get(
            "state",
            "",
        )
    ).strip().upper()

    # X cards have already been generated at this state.
    if state != "IMAGE_CONCEPT_REVIEW":
        raise ValueError(
            "Expected IMAGE_CONCEPT_REVIEW, "
            f"got {state!r}."
        )

    concepts = data.get(
        "concepts"
    )

    if (
        not isinstance(concepts, list)
        or len(concepts) != CONCEPT_COUNT
    ):
        raise ValueError(
            "Exactly 3 image concepts are required."
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

        number = int(
            concept.get(
                "number",
                0,
            )
        )

        if number != index:
            raise ValueError(
                "Concept numbering must be exactly 1-3. "
                f"Problem at concept {index}."
            )

        web_image_path = first_text(
            concept.get(
                "web_image_path"
            )
        )

        x_image_path = first_text(
            concept.get(
                "x_image_path"
            )
        )

        if not web_image_path:
            raise ValueError(
                f"Concept {index} has no web_image_path."
            )

        if not x_image_path:
            raise ValueError(
                f"Concept {index} has no x_image_path."
            )

        web_path = Path(
            web_image_path
        )

        x_path = Path(
            x_image_path
        )

        if not web_path.exists():
            raise FileNotFoundError(
                f"Concept {index} WEB image not found: {web_path}"
            )

        if not x_path.exists():
            raise FileNotFoundError(
                f"Concept {index} X image not found: {x_path}"
            )

        web_status = str(
            concept.get(
                "web_image_status",
                "",
            )
        ).strip().upper()

        x_status = str(
            concept.get(
                "x_image_status",
                "",
            )
        ).strip().upper()

        if web_status != "GENERATED":
            raise ValueError(
                f"Concept {index} WEB image status "
                f"is {web_status!r}, not GENERATED."
            )

        if x_status != "GENERATED":
            raise ValueError(
                f"Concept {index} X image status "
                f"is {x_status!r}, not GENERATED."
            )

    return concepts


# ============================================================
# Selected story
# ============================================================

def get_story(
    data: dict[str, Any],
) -> dict[str, Any]:

    story = data.get(
        "story"
    )

    if isinstance(
        story,
        dict,
    ):
        return story

    return {}


# ============================================================
# Email body
# ============================================================

def build_body(
    data: dict[str, Any],
    concepts: list[dict[str, Any]],
) -> str:

    story = get_story(
        data
    )

    title_ja = first_text(
        story.get(
            "title_ja"
        ),
        story.get(
            "title"
        ),
        "承認済み Daily Duck 記事",
    )

    title_en = first_text(
        story.get(
            "title"
        )
    )

    reason_ja = first_text(
        story.get(
            "reason_ja"
        ),
        story.get(
            "reason"
        ),
        "記事のポイント情報はありません。",
    )

    reason_en = first_text(
        story.get(
            "reason"
        )
    )

    lines: list[str] = []

    # --------------------------------------------------------
    # Header / selected story
    # --------------------------------------------------------

    lines.extend(
        [
            "THE DAILY DUCK — IMAGE SELECTION",
            "",
            "本日のWEB用画像とX用画像を3案作成しました。",
            "",
            "1つの番号を選ぶと、",
            "同じ番号のWEB画像とX画像をセットで正式採用します。",
            "",
            "==================================================",
            "SELECTED STORY / 選択した記事",
            "==================================================",
            "",
            "TITLE / タイトル",
            "",
            title_ja,
        ]
    )

    if title_en:
        lines.extend(
            [
                "",
                "EN:",
                title_en,
            ]
        )

    lines.extend(
        [
            "",
            "",
            "WHY THIS STORY / 記事のポイント",
            "",
            reason_ja,
        ]
    )

    if reason_en:
        lines.extend(
            [
                "",
                "English:",
                reason_en,
            ]
        )

    # --------------------------------------------------------
    # 3 image sets
    # --------------------------------------------------------

    lines.extend(
        [
            "",
            "",
            "==================================================",
            "IMAGE SETS / 画像案 1〜3",
            "==================================================",
            "",
            "各案には2枚の画像があります。",
            "",
            "WEB：The Daily Duck ホームページ用",
            "X：X投稿用 Daily Duck ブランドカード",
            "",
        ]
    )

    for concept in concepts:

        number = int(
            concept["number"]
        )

        lines.extend(
            [
                "--------------------------------------------------",
                "",
                f"【案 {number}】",
                "",
                first_text(
                    concept.get(
                        "title_ja"
                    )
                ),
                "",
                "コンセプト:",
                first_text(
                    concept.get(
                        "concept_ja"
                    )
                ),
                "",
                "構図:",
                first_text(
                    concept.get(
                        "composition_ja"
                    )
                ),
                "",
                f"WEB画像:",
                f"concept_{number}_web.png",
                "",
                f"X画像:",
                f"concept_{number}_x.png",
                "",
            ]
        )

    # --------------------------------------------------------
    # Selection instructions
    # --------------------------------------------------------

    lines.extend(
        [
            "==================================================",
            "IMAGE SELECTION / 画像選択",
            "==================================================",
            "",
            "使用したい画像セットの番号だけ返信してください。",
            "",
            "1",
            "2",
            "3",
            "",
            "例:",
            "",
            "2",
            "",
            "IMPORTANT:",
            "",
            "- 返信は半角数字 1 / 2 / 3 のどれか1文字だけにしてください。",
            "- 同じ番号のWEB画像とX画像をセットで採用します。",
            "- 選択したWEB画像をThe Daily Duckサイトへ使用します。",
            "- 選択したXブランドカードをX投稿へ使用します。",
            "- 選択後はREADY_TO_PUBLISHへ進みます。",
            "- HP公開に成功した場合だけX投稿へ進みます。",
            "",
            "The Daily Duck",
            "One day. One story. One duck. 🐤",
        ]
    )

    return (
        "\n".join(
            lines
        ).strip()
        + "\n"
    )


# ============================================================
# Recipients
# ============================================================

def parse_recipients(
    raw: str,
) -> list[str]:

    recipients = [
        item.strip()
        for item in raw.split(",")
        if item.strip()
    ]

    if not recipients:
        raise RuntimeError(
            "EMAIL_TO contains no valid recipients."
        )

    return recipients


# ============================================================
# Attach image
# ============================================================

def attach_image(
    msg: EmailMessage,
    image_path: Path,
    filename: str,
) -> None:

    mime_type, _ = (
        mimetypes.guess_type(
            filename
        )
    )

    if mime_type:

        maintype, subtype = (
            mime_type.split(
                "/",
                1,
            )
        )

    else:

        maintype = "image"
        subtype = "png"

    msg.add_attachment(
        image_path.read_bytes(),
        maintype=maintype,
        subtype=subtype,
        filename=filename,
    )


# ============================================================
# Main
# ============================================================

def main() -> int:

    data = load_json(
        CONCEPTS_PATH
    )

    concepts = validate_package(
        data
    )

    gmail_address = required_env(
        "GMAIL_ADDRESS"
    )

    gmail_app_password = required_env(
        "GMAIL_APP_PASSWORD"
    )

    recipients = parse_recipients(
        required_env(
            "EMAIL_TO"
        )
    )

    issue_date = first_text(
        data.get(
            "issue_date"
        ),
        data.get(
            "date"
        ),
    )

    body = build_body(
        data,
        concepts,
    )

    EMAIL_PREVIEW_PATH.write_text(
        body,
        encoding="utf-8",
    )

    subject = (
        SUBJECT_PREFIX
        + (
            f" — {issue_date}"
            if issue_date
            else ""
        )
    )

    msg = EmailMessage()

    msg[
        "Subject"
    ] = subject

    msg[
        "From"
    ] = gmail_address

    msg[
        "To"
    ] = ", ".join(
        recipients
    )

    msg.set_content(
        body
    )

    # --------------------------------------------------------
    # Attach:
    #
    # concept_1_web.png
    # concept_1_x.png
    # concept_2_web.png
    # concept_2_x.png
    # concept_3_web.png
    # concept_3_x.png
    # --------------------------------------------------------

    attachment_count = 0

    for concept in concepts:

        number = int(
            concept[
                "number"
            ]
        )

        web_path = Path(
            concept[
                "web_image_path"
            ]
        )

        x_path = Path(
            concept[
                "x_image_path"
            ]
        )

        attach_image(
            msg,
            web_path,
            f"concept_{number}_web.png",
        )

        attachment_count += 1

        attach_image(
            msg,
            x_path,
            f"concept_{number}_x.png",
        )

        attachment_count += 1

    if attachment_count != 6:
        raise RuntimeError(
            f"Expected 6 attachments, got {attachment_count}."
        )

    # --------------------------------------------------------
    # Send
    # --------------------------------------------------------

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        timeout=90,
    ) as smtp:

        smtp.login(
            gmail_address,
            gmail_app_password,
        )

        smtp.send_message(
            msg
        )

    print(
        "Image selection email sent."
    )

    print(
        f"Recipients: {len(recipients)}"
    )

    print(
        "Image sets: 3"
    )

    print(
        "WEB images attached: 3"
    )

    print(
        "X brand cards attached: 3"
    )

    print(
        "Total attachments: 6"
    )

    print(
        "Valid replies: 1 / 2 / 3"
    )

    print(
        f"Email preview saved: {EMAIL_PREVIEW_PATH}"
    )

    print(
        "STATE: IMAGE_CONCEPT_REVIEW"
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
