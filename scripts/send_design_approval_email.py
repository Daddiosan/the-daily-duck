#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path
from typing import Any


OPTIONS_PATH = Path(
    "automation_state/design_options.json"
)

EXPECTED_CONCEPT_COUNT = 3
EXPECTED_TITLE_COUNT = 3


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
# Validation
# ============================================================

def validate_package(
    package: dict[str, Any],
) -> None:

    state = first_text(
        package.get("state")
    ).upper()

    if state != "CONCEPTS_READY":
        raise ValueError(
            "Design approval email requires "
            "state CONCEPTS_READY; "
            f"got {state!r}."
        )

    concepts = package.get(
        "image_concepts"
    )

    if (
        not isinstance(concepts, list)
        or len(concepts)
        != EXPECTED_CONCEPT_COUNT
    ):
        raise ValueError(
            "design_options.json must contain "
            f"exactly {EXPECTED_CONCEPT_COUNT} "
            "image concepts."
        )

    titles = package.get(
        "title_ideas"
    )

    if (
        not isinstance(titles, list)
        or len(titles)
        != EXPECTED_TITLE_COUNT
    ):
        raise ValueError(
            "design_options.json must contain "
            f"exactly {EXPECTED_TITLE_COUNT} "
            "title ideas."
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
                f"Image concept {index} "
                "must be an object."
            )

        number = concept.get(
            "number"
        )

        if number != index:
            raise ValueError(
                f"Image concept {index} "
                "has an invalid number."
            )

    for index, title in enumerate(
        titles,
        start=1,
    ):
        if not isinstance(
            title,
            dict,
        ):
            raise ValueError(
                f"Title idea {index} "
                "must be an object."
            )

        number = title.get(
            "number"
        )

        if number != index:
            raise ValueError(
                f"Title idea {index} "
                "has an invalid number."
            )


# ============================================================
# Formatting
# ============================================================

def format_concept(
    concept: dict[str, Any],
) -> str:

    number = concept.get(
        "number"
    )

    title_en = first_text(
        concept.get("title_en")
    )

    concept_en = first_text(
        concept.get("concept_en")
    )

    composition_en = first_text(
        concept.get("composition_en")
    )

    title_ja = first_text(
        concept.get("title_ja")
    )

    concept_ja = first_text(
        concept.get("concept_ja")
    )

    composition_ja = first_text(
        concept.get("composition_ja")
    )

    return f"""
==================================================
IMAGE CONCEPT {number}
画像コンセプト {number}
==================================================

ENGLISH MASTER
--------------------------------------------------

TITLE

{title_en}


CONCEPT

{concept_en}


COMPOSITION

{composition_en}


JAPANESE REVIEW TRANSLATION
--------------------------------------------------

コンセプト名

{title_ja}


コンセプト

{concept_ja}


構図

{composition_ja}
""".strip()


def format_title(
    title: dict[str, Any],
) -> str:

    number = title.get(
        "number"
    )

    title_text = first_text(
        title.get("title")
    )

    meaning_ja = first_text(
        title.get("meaning_ja")
    )

    return f"""
TITLE {number}

{title_text}

日本語での意味・ニュアンス:
{meaning_ja}
""".strip()


# ============================================================
# Email
# ============================================================

def build_email(
    package: dict[str, Any],
) -> tuple[str, str]:

    issue_date = first_text(
        package.get("issue_date"),
        package.get("date"),
    )

    if not issue_date:
        raise ValueError(
            "Design options package "
            "is missing issue_date."
        )

    concepts = package[
        "image_concepts"
    ]

    titles = package[
        "title_ideas"
    ]

    concept_sections = "\n\n\n".join(
        format_concept(
            concept
        )
        for concept in concepts
    )

    title_sections = "\n\n".join(
        format_title(
            title
        )
        for title in titles
    )

    subject = (
        "The Daily Duck — "
        "Choose Image Concept — "
        f"{issue_date}"
    )

    body = f"""
The Daily Duck — Design Selection

Issue date:
{issue_date}

The story has passed Gate A.

記事選択は完了しています。

次に、採用する画像コンセプトを
3案の中から1つ選んでください。


==================================================
IMAGE CONCEPTS — CHOOSE ONE
画像コンセプト — 1案選択
==================================================

{concept_sections}


==================================================
TITLE IDEAS
タイトル候補
==================================================

以下のタイトル3案は、
この段階では確認用です。

最終的なタイトル選択は、
実画像5枚が生成された後に行います。

{title_sections}


==================================================
HOW TO REPLY
返信方法
==================================================

採用する画像コンセプトの番号を
1つだけ返信してください。

半角数字・全角数字の
どちらでも受け付けます。


有効な返信:

1
2
3

または

１
２
３


例:

2

または

２


==================================================
IMPORTANT
==================================================

この段階では、

「画像コンセプト」

だけを選択します。

タイトル番号はまだ返信しないでください。


返信後の流れ:

画像コンセプト3案
        ↓
あなたが1案を選択
        ↓
選択したその1つのコンセプトを固定
        ↓
同じコンセプトから実画像を5枚生成
        ↓
実画像5枚をメール送信
        ↓
画像番号 1〜5
+
タイトル番号 1〜3
を最終選択
        ↓
READY_TO_PUBLISH
        ↓
Website
        ↓
X


最終選択でも、
半角数字・全角数字の
どちらでも使用できます。

例:

4 1

または

４ １


再生成コマンド:

NEXT 5

全角入力:

ＮＥＸＴ ５

にも対応します。


No website or X publication occurs
until the final image and title
have been selected.
""".strip()

    return (
        subject,
        body,
    )


# ============================================================
# Gmail
# ============================================================

def send_email(
    subject: str,
    body: str,
) -> int:

    gmail_address = required_env(
        "GMAIL_ADDRESS"
    )

    gmail_password = required_env(
        "GMAIL_APP_PASSWORD"
    )

    recipients = [
        item.strip()
        for item in required_env(
            "EMAIL_TO"
        ).split(",")
        if item.strip()
    ]

    if not recipients:
        raise RuntimeError(
            "EMAIL_TO contains "
            "no valid recipients."
        )

    message = EmailMessage()

    message["From"] = (
        gmail_address
    )

    message["To"] = (
        ", ".join(recipients)
    )

    message["Subject"] = (
        subject
    )

    message.set_content(
        body
    )

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

    return len(
        recipients
    )


# ============================================================
# Main
# ============================================================

def main() -> int:

    package = load_json(
        OPTIONS_PATH
    )

    validate_package(
        package
    )

    subject, body = build_email(
        package
    )

    recipient_count = send_email(
        subject,
        body,
    )

    print(
        "Design selection email sent."
    )

    print(
        "Issue date:",
        package.get("issue_date"),
    )

    print(
        "Image concepts:",
        EXPECTED_CONCEPT_COUNT,
    )

    print(
        "Title ideas:",
        EXPECTED_TITLE_COUNT,
    )

    print(
        "Valid concept replies:"
    )

    print(
        "1 / 2 / 3"
    )

    print(
        "Full-width replies also supported:"
    )

    print(
        "１ / ２ / ３"
    )

    print(
        "Recipients:",
        recipient_count,
    )

    print(
        f"Subject: {subject}"
    )

    print(
        "STATE: CONCEPTS_READY"
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
