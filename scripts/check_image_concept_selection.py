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
    "The Daily Duck — Image Selection"
)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing environment variable: {name}"
        )

    return value


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(
        path.read_text(encoding="utf-8")
    )


def first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def validate(data: dict[str, Any]) -> None:
    if str(
        data.get("state", "")
    ).upper() != "IMAGE_CONCEPT_REVIEW":
        raise ValueError(
            "Expected IMAGE_CONCEPT_REVIEW."
        )

    concepts = data.get("concepts")

    if (
        not isinstance(concepts, list)
        or len(concepts) != 3
    ):
        raise ValueError(
            "Exactly three concepts required."
        )

    for number, concept in enumerate(
        concepts,
        start=1,
    ):
        if int(
            concept.get("number", 0)
        ) != number:
            raise ValueError(
                "Concept numbering error."
            )

        for field in (
            "web_image_path",
            "x_image_path",
        ):
            path = Path(
                str(concept.get(field, ""))
            )

            if not path.exists():
                raise FileNotFoundError(path)


def build_body(data: dict[str, Any]) -> str:
    story = data.get("story")

    if not isinstance(story, dict):
        story = {}

    title_ja = first_text(
        story.get("title_ja"),
        story.get("title"),
    )

    title_en = first_text(
        story.get("title")
    )

    reason_ja = first_text(
        story.get("reason_ja"),
        story.get("reason"),
    )

    lines = [
        "THE DAILY DUCK — IMAGE SELECTION",
        "",
        "==================================================",
        "SELECTED STORY / 選択した記事",
        "==================================================",
        "",
        "TITLE / タイトル",
        "",
        title_ja,
        "",
    ]

    if title_en:
        lines.extend(
            [
                "EN:",
                title_en,
                "",
            ]
        )

    lines.extend(
        [
            "WHY THIS STORY / 記事のポイント",
            "",
            reason_ja,
            "",
            "==================================================",
            "IMAGE SETS / 画像案 1〜3",
            "==================================================",
            "",
            "各案には2枚あります。",
            "",
            "WEB：The Daily Duckサイト用",
            "X：X投稿用のDaily Duckブランドカード",
            "",
        ]
    )

    for concept in data["concepts"]:
        number = concept["number"]

        lines.extend(
            [
                f"【案 {number}】",
                "",
                concept.get(
                    "title_ja",
                    "",
                ),
                "",
                "コンセプト:",
                concept.get(
                    "concept_ja",
                    "",
                ),
                "",
                "構図:",
                concept.get(
                    "composition_ja",
                    "",
                ),
                "",
                f"WEB画像: concept_{number}_web.png",
                f"X画像:   concept_{number}_x.png",
                "",
                "--------------------------------------------------",
                "",
            ]
        )

    lines.extend(
        [
            "選択方法",
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
            "番号を選ぶと、同じ番号の",
            "WEB画像とX画像をセットで正式採用します。",
            "",
            "選択後は自動でHPを更新し、",
            "HP公開成功後にXへ投稿します。",
            "",
            "The Daily Duck",
            "One day. One story. One duck.",
        ]
    )

    return "\n".join(lines) + "\n"


def attach(
    msg: EmailMessage,
    path: Path,
) -> None:

    mime, _ = mimetypes.guess_type(
        path.name
    )

    if mime:
        maintype, subtype = mime.split("/", 1)
    else:
        maintype, subtype = "image", "png"

    msg.add_attachment(
        path.read_bytes(),
        maintype=maintype,
        subtype=subtype,
        filename=path.name,
    )


def main() -> None:
    data = load_json(
        CONCEPTS_PATH
    )

    validate(data)

    recipients = [
        x.strip()
        for x in required_env(
            "EMAIL_TO"
        ).split(",")
        if x.strip()
    ]

    body = build_body(data)

    EMAIL_PREVIEW_PATH.write_text(
        body,
        encoding="utf-8",
    )

    issue_date = first_text(
        data.get("issue_date")
    )

    msg = EmailMessage()

    msg["From"] = required_env(
        "GMAIL_ADDRESS"
    )

    msg["To"] = ", ".join(
        recipients
    )

    msg["Subject"] = (
        f"{SUBJECT_PREFIX} — {issue_date}"
    )

    msg.set_content(body)

    for concept in data["concepts"]:
        attach(
            msg,
            Path(concept["web_image_path"]),
        )

        attach(
            msg,
            Path(concept["x_image_path"]),
        )

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        timeout=90,
    ) as smtp:

        smtp.login(
            required_env("GMAIL_ADDRESS"),
            required_env("GMAIL_APP_PASSWORD"),
        )

        smtp.send_message(msg)

    print(
        "Sent 3 WEB/X image sets."
    )

    print(
        "Attachments: 6"
    )

    print(
        "Valid replies: 1 / 2 / 3"
    )


if __name__ == "__main__":
    main()
