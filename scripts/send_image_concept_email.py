#!/usr/bin/env python3
from __future__ import annotations

import json
import mimetypes
import os
import smtplib
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
    "The Daily Duck — Image Concept Selection"
)


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

    with path.open(
        "r",
        encoding="utf-8",
    ) as f:
        data = json.load(f)

    if not isinstance(
        data,
        dict,
    ):
        raise ValueError(
            f"{path} must contain a JSON object."
        )

    return data


# ============================================================
# Validation
# ============================================================

def validate_package(
    data: dict[str, Any],
) -> None:

    state = str(
        data.get("state", "")
    ).strip().upper()

    if state != "IMAGE_CONCEPT_REVIEW":
        raise ValueError(
            "Expected IMAGE_CONCEPT_REVIEW state, "
            f"got {state!r}."
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
                "Concept numbering must be exactly 1-5; "
                f"problem at {index}."
            )

        preview_path = str(
            concept.get(
                "preview_image_path",
                "",
            )
        ).strip()

        if not preview_path:
            raise ValueError(
                f"Concept {index} has no preview_image_path."
            )

        path = Path(
            preview_path
        )

        if not path.exists():
            raise FileNotFoundError(
                f"Concept {index} preview not found: {path}"
            )


# ============================================================
# Story helpers
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


def story_title_ja(
    data: dict[str, Any],
) -> str:

    story = get_story(
        data
    )

    return first_text(
        story.get("title_ja"),
        story.get("headline_ja"),
        story.get("title"),
        story.get("headline"),
        "承認済み Daily Duck 記事",
    )


def story_title_en(
    data: dict[str, Any],
) -> str:

    story = get_story(
        data
    )

    return first_text(
        story.get("title"),
        story.get("headline"),
    )


def story_reason_ja(
    data: dict[str, Any],
) -> str:

    story = get_story(
        data
    )

    return first_text(
        story.get("reason_ja"),
        story.get("why_this_story_ja"),
        story.get("reason"),
        "記事のポイント情報はありません。",
    )


def story_reason_en(
    data: dict[str, Any],
) -> str:

    story = get_story(
        data
    )

    return first_text(
        story.get("reason"),
        story.get("why_this_story"),
    )


# ============================================================
# Email body
# ============================================================

def build_body(
    data: dict[str, Any],
) -> str:

    concepts = data[
        "concepts"
    ]

    title_ja = story_title_ja(
        data
    )

    title_en = story_title_en(
        data
    )

    reason_ja = story_reason_ja(
        data
    )

    reason_en = story_reason_en(
        data
    )

    lines: list[str] = []

    # --------------------------------------------------------
    # Header
    # --------------------------------------------------------

    lines.extend(
        [
            "THE DAILY DUCK — 画像コンセプト選択",
            "",
            "記事の選択・承認が完了しました。",
            "",
            "忘れないように、今回選択した記事を最初に再掲します。",
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
    # Image concepts
    # --------------------------------------------------------

    lines.extend(
        [
            "",
            "",
            "==================================================",
            "IMAGE CONCEPTS / 画像コンセプト 1〜5",
            "==================================================",
            "",
            "この承認済み記事について、",
            "異なる画像コンセプトを5案作成しました。",
            "",
            "各コンセプトについて、実際のプレビュー画像も",
            "1枚ずつ生成してこのメールに添付しています。",
            "",
            "画像1〜5を見比べて、",
            "今後の最終画像に使用したいコンセプトを",
            "1つ選んでください。",
            "",
        ]
    )

    for concept in concepts:

        number = concept[
            "number"
        ]

        lines.extend(
            [
                "--------------------------------------------------",
                "",
                f"【{number}】"
                f"{concept.get('title_ja', '').strip()}",
                "",
                "コンセプト:",
                concept.get(
                    "concept_ja",
                    "",
                ).strip(),
                "",
                "構図:",
                concept.get(
                    "composition_ja",
                    "",
                ).strip(),
                "",
                f"添付画像: concept_{number}.png",
                "",
                "English:",
                concept.get(
                    "title_en",
                    "",
                ).strip(),
                "",
                concept.get(
                    "concept_en",
                    "",
                ).strip(),
                "",
            ]
        )

    # --------------------------------------------------------
    # Selection
    # --------------------------------------------------------

    lines.extend(
        [
            "==================================================",
            "IMAGE CONCEPT SELECTION / 画像コンセプト選択",
            "==================================================",
            "",
            "このメールに、使用したいコンセプトの",
            "番号だけを返信してください。",
            "",
            "1",
            "2",
            "3",
            "4",
            "5",
            "",
            "例:",
            "",
            "3",
            "",
            "IMPORTANT:",
            "",
            "- 返信は 1〜5 の数字1文字だけにしてください。",
            "- それ以外の返信では次の工程へ進みません。",
            "- 選択したコンセプト自体が今後の画像方針になります。",
            "- 次の工程では、選択した1コンセプトだけから実画像候補を5枚生成します。",
            "- その5枚から最終画像を1〜5で選択できます。",
            "- 気に入らない場合は NEXT 5 で、同じコンセプトのまま新しい5枚を生成します。",
            "- 最終画像確定までWebサイト/Xには公開しません。",
            "",
            "The Daily Duck",
            "One day. One story. One duck. 🐤",
        ]
    )

    return (
        "\n".join(lines).strip()
        + "\n"
    )


# ============================================================
# Recipients
# ============================================================

def parse_recipients(
    raw: str,
) -> list[str]:

    recipients = [
        x.strip()
        for x in raw.split(",")
        if x.strip()
    ]

    if not recipients:
        raise RuntimeError(
            "EMAIL_TO contains no valid recipients."
        )

    return recipients


# ============================================================
# Attachment
# ============================================================

def attach_image(
    msg: EmailMessage,
    image_path: Path,
) -> None:

    mime_type, _ = (
        mimetypes.guess_type(
            image_path.name
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
        filename=image_path.name,
    )


# ============================================================
# Main
# ============================================================

def main() -> None:

    data = load_json(
        CONCEPTS_PATH
    )

    validate_package(
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

    body = build_body(
        data
    )

    EMAIL_PREVIEW_PATH.write_text(
        body,
        encoding="utf-8",
    )

    issue_date = str(
        data.get(
            "issue_date",
            "",
        )
    ).strip()

    subject = (
        f"{SUBJECT_PREFIX}"
        + (
            f" — {issue_date}"
            if issue_date
            else ""
        )
    )

    msg = EmailMessage()

    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = ", ".join(
        recipients
    )

    msg.set_content(
        body
    )

    # Attach exactly five concept preview images.
    for concept in data[
        "concepts"
    ]:

        image_path = Path(
            str(
                concept[
                    "preview_image_path"
                ]
            )
        )

        attach_image(
            msg,
            image_path,
        )

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        timeout=60,
    ) as smtp:

        smtp.login(
            gmail_address,
            gmail_app_password,
        )

        smtp.send_message(
            msg
        )

    print(
        "Image concept selection email sent."
    )

    print(
        f"Recipients: {len(recipients)}"
    )

    print(
        "Selected story TITLE included."
    )

    print(
        "Selected story WHY THIS STORY included."
    )

    print(
        "Concept count: 5"
    )

    print(
        "Attached preview images: 5"
    )

    print(
        "Valid replies: 1 / 2 / 3 / 4 / 5"
    )

    print(
        f"Preview saved: {EMAIL_PREVIEW_PATH}"
    )


if __name__ == "__main__":
    main()
