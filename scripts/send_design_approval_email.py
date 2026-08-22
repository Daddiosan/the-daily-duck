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


OPTIONS_PATH = Path(
    "automation_state/design_options.json"
)

EXPECTED_CONCEPT_COUNT = 3
EXPECTED_TITLE_COUNT = 3
EXPECTED_PREVIEW_COUNT = 3


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


def issue_date_from(
    package: dict[str, Any],
) -> str:

    issue_date = first_text(
        package.get(
            "issue_date"
        ),
        package.get(
            "date"
        ),
    )

    if not issue_date:

        raise ValueError(
            "design_options.json "
            "is missing issue_date."
        )

    return issue_date


# ============================================================
# Shared validation
# ============================================================

def validate_three_concepts(
    package: dict[str, Any],
) -> list[dict[str, Any]]:

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
        ) != EXPECTED_CONCEPT_COUNT
    ):

        raise ValueError(
            "design_options.json must contain "
            f"exactly {EXPECTED_CONCEPT_COUNT} "
            "image concepts."
        )

    normalized: list[
        dict[str, Any]
    ] = []

    for index, item in enumerate(
        concepts,
        start=1,
    ):

        if not isinstance(
            item,
            dict,
        ):

            raise ValueError(
                f"Image concept {index} "
                "must be an object."
            )

        if int(
            item.get(
                "number",
                0,
            )
            or 0
        ) != index:

            raise ValueError(
                f"Image concept {index} "
                "has an invalid number."
            )

        normalized.append(
            item
        )

    return normalized


def validate_three_titles(
    package: dict[str, Any],
) -> list[dict[str, Any]]:

    titles = package.get(
        "title_ideas"
    )

    if (
        not isinstance(
            titles,
            list,
        )
        or len(
            titles
        ) != EXPECTED_TITLE_COUNT
    ):

        raise ValueError(
            "design_options.json must contain "
            f"exactly {EXPECTED_TITLE_COUNT} "
            "title ideas."
        )

    normalized: list[
        dict[str, Any]
    ] = []

    for index, item in enumerate(
        titles,
        start=1,
    ):

        if not isinstance(
            item,
            dict,
        ):

            raise ValueError(
                f"Title idea {index} "
                "must be an object."
            )

        if int(
            item.get(
                "number",
                0,
            )
            or 0
        ) != index:

            raise ValueError(
                f"Title idea {index} "
                "has an invalid number."
            )

        normalized.append(
            item
        )

    return normalized


def validate_three_previews(
    package: dict[str, Any],
) -> list[dict[str, Any]]:

    previews = package.get(
        "design_previews"
    )

    if (
        not isinstance(
            previews,
            list,
        )
        or len(
            previews
        ) != EXPECTED_PREVIEW_COUNT
    ):

        raise ValueError(
            "design_options.json must contain "
            f"exactly {EXPECTED_PREVIEW_COUNT} "
            "real image previews."
        )

    normalized: list[
        dict[str, Any]
    ] = []

    for index, item in enumerate(
        previews,
        start=1,
    ):

        if not isinstance(
            item,
            dict,
        ):

            raise ValueError(
                f"Preview {index} "
                "must be an object."
            )

        if int(
            item.get(
                "number",
                0,
            )
            or 0
        ) != index:

            raise ValueError(
                f"Preview {index} "
                "has an invalid number."
            )

        image_path = Path(
            first_text(
                item.get(
                    "image_path"
                )
            )
        )

        if not image_path.exists():

            raise FileNotFoundError(
                "Preview image does not exist: "
                f"{image_path}"
            )

        normalized.append(
            item
        )

    return normalized


# ============================================================
# Concept formatting
# ============================================================

def format_concept(
    concept: dict[str, Any],
) -> str:

    number = concept.get(
        "number"
    )

    return f"""
==================================================
IMAGE CONCEPT {number}
画像コンセプト {number}
==================================================

ENGLISH MASTER
--------------------------------------------------

TITLE

{first_text(concept.get('title_en'))}


CONCEPT

{first_text(concept.get('concept_en'))}


COMPOSITION

{first_text(concept.get('composition_en'))}


JAPANESE REVIEW TRANSLATION
--------------------------------------------------

コンセプト名

{first_text(concept.get('title_ja'))}


コンセプト

{first_text(concept.get('concept_ja'))}


構図

{first_text(concept.get('composition_ja'))}
""".strip()


def format_title(
    title: dict[str, Any],
) -> str:

    number = title.get(
        "number"
    )

    return f"""
TITLE {number}

{first_text(title.get('title'))}

日本語での意味・ニュアンス:
{first_text(title.get('meaning_ja'))}
""".strip()


# ============================================================
# Stage 1:
# Concept selection email
# ============================================================

def build_concept_email(
    package: dict[str, Any],
) -> tuple[
    str,
    str,
]:

    issue_date = issue_date_from(
        package
    )

    concepts = validate_three_concepts(
        package
    )

    titles = validate_three_titles(
        package
    )

    concept_sections = (
        "\n\n\n".join(
            format_concept(
                concept
            )
            for concept in concepts
        )
    )

    title_sections = (
        "\n\n".join(
            format_title(
                title
            )
            for title in titles
        )
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


==================================================
IMAGE CONCEPTS — CHOOSE ONE
画像コンセプト — 1案選択
==================================================

{concept_sections}


==================================================
TITLE IDEAS
タイトル候補
==================================================

タイトル候補は3案です。

この段階では確認用です。
タイトルの最終選択は、
実画像3枚を確認した後に行います。

{title_sections}


==================================================
HOW TO REPLY
返信方法
==================================================

採用する画像コンセプトの番号を
1つだけ返信してください。


有効:

1
2
3

全角もOK:

１
２
３


例:

2

または

２


==================================================
NEXT
==================================================

コンセプトを1案選択
        ↓
そのコンセプトをLOCK
        ↓
同じコンセプトから
実画像3枚を生成
        ↓
画像3枚をメール添付
        ↓
画像1〜3 + タイトル1〜3
を最終選択

この段階では、
Webサイト/Xへの公開は行いません。
""".strip()

    return (
        subject,
        body,
    )


# ============================================================
# Stage 2:
# Final image + title selection email
# ============================================================

def build_final_email(
    package: dict[str, Any],
) -> tuple[
    str,
    str,
]:

    issue_date = issue_date_from(
        package
    )

    previews = validate_three_previews(
        package
    )

    titles = validate_three_titles(
        package
    )

    selected_concept_number = (
        package.get(
            "selected_image_concept_number"
        )
    )

    selected_concept = package.get(
        "selected_image_concept"
    )

    if not isinstance(
        selected_concept,
        dict,
    ):

        raise ValueError(
            "selected_image_concept "
            "is missing."
        )

    batch_number = int(
        package.get(
            "preview_batch_number",
            1,
        )
        or 1
    )

    title_sections = (
        "\n\n".join(
            format_title(
                title
            )
            for title in titles
        )
    )

    image_lines = []

    for preview in previews:

        number = preview.get(
            "number"
        )

        image_lines.append(
            f"""
IMAGE {number}
添付画像: preview_{number}.png
""".strip()
        )

    image_text = (
        "\n\n".join(
            image_lines
        )
    )

    subject = (
        "The Daily Duck — "
        "Final Image & Title Selection — "
        f"{issue_date} — "
        f"Batch {batch_number}"
    )

    body = f"""
The Daily Duck — Final Design Selection

Issue date:
{issue_date}

Selected concept:
{selected_concept_number}

{first_text(
    selected_concept.get("title_en"),
    selected_concept.get("title_ja"),
)}

Preview batch:
{batch_number}


==================================================
REAL IMAGES
実画像3案
==================================================

このメールに、
同じ選択済みコンセプトから生成した
実画像3枚を添付しています。

{image_text}


==================================================
TITLE OPTIONS
タイトル3案
==================================================

{title_sections}


==================================================
FINAL REPLY
最終選択
==================================================

以下の形式で返信してください。

画像番号 + 半角スペース + タイトル番号


例:

2 1


全角数字でもOKです:

２ １


画像番号:

1
2
3


タイトル番号:

1
2
3


==================================================
NEED ANOTHER IMAGE BATCH?
再生成
==================================================

画像3枚が気に入らない場合は、

NEXT 3

と返信してください。


全角でもOK:

ＮＥＸＴ ３


NEXT 3では、

選択済みコンセプトは変更しません。

同じコンセプトをLOCKしたまま、
新しい実画像3枚を生成します。


==================================================
IMPORTANT
==================================================

最終的に、

画像番号 + タイトル番号

が選択されるまで、
READY_TO_PUBLISHには進みません。

Webサイト/Xへの公開も行いません。
""".strip()

    return (
        subject,
        body,
    )


# ============================================================
# Gmail
# ============================================================

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
            "EMAIL_TO contains "
            "no valid recipients."
        )

    return recipients


def create_message(
    subject: str,
    body: str,
) -> EmailMessage:

    gmail_address = required_env(
        "GMAIL_ADDRESS"
    )

    recipients = recipients_from_env()

    message = EmailMessage()

    message[
        "From"
    ] = gmail_address

    message[
        "To"
    ] = ", ".join(
        recipients
    )

    message[
        "Subject"
    ] = subject

    message.set_content(
        body
    )

    return message


def attach_preview_images(
    message: EmailMessage,
    previews: list[
        dict[str, Any]
    ],
) -> None:

    for preview in previews:

        number = int(
            preview.get(
                "number",
                0,
            )
            or 0
        )

        path = Path(
            first_text(
                preview.get(
                    "image_path"
                )
            )
        )

        if not path.exists():

            raise FileNotFoundError(
                f"Preview image missing: {path}"
            )

        mime_type, _ = (
            mimetypes.guess_type(
                path.name
            )
        )

        if (
            mime_type
            and "/" in mime_type
        ):

            maintype, subtype = (
                mime_type.split(
                    "/",
                    1,
                )
            )

        else:

            maintype = "image"
            subtype = "png"

        data = path.read_bytes()

        message.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=(
                f"DailyDuck_"
                f"Image_{number}.png"
            ),
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

    return len(
        recipients
    )


# ============================================================
# State transitions
# ============================================================

def mark_waiting_concept(
    package: dict[str, Any],
    subject: str,
) -> None:

    package[
        "state"
    ] = (
        "WAITING_CONCEPT_SELECTION"
    )

    package[
        "concept_email_subject"
    ] = subject

    package[
        "email_subject"
    ] = subject

    package[
        "concept_email_sent_at"
    ] = datetime.now(
        timezone.utc
    ).isoformat()

    package[
        "concept_selection_rule"
    ] = {
        "valid_replies": [
            "1",
            "2",
            "3",
        ],
        "full_width_supported":
            True,
        "next_state":
            "APPROVED_IMAGE_CONCEPT",
    }

    save_package(
        package
    )


def mark_waiting_final(
    package: dict[str, Any],
    subject: str,
) -> None:

    package[
        "state"
    ] = (
        "WAITING_FINAL_SELECTION"
    )

    package[
        "final_email_subject"
    ] = subject

    package[
        "email_subject"
    ] = subject

    package[
        "final_email_sent_at"
    ] = datetime.now(
        timezone.utc
    ).isoformat()

    package[
        "final_selection_rule"
    ] = {

        "image_numbers": [
            1,
            2,
            3,
        ],

        "title_numbers": [
            1,
            2,
            3,
        ],

        "format":
            "IMAGE_NUMBER TITLE_NUMBER",

        "example":
            "2 1",

        "next_3_command":
            "NEXT 3",

        "full_width_supported":
            True,

        "next_state":
            "READY_TO_PUBLISH",
    }

    save_package(
        package
    )


# ============================================================
# Stage runners
# ============================================================

def send_concept_selection(
    package: dict[str, Any],
) -> int:

    subject, body = (
        build_concept_email(
            package
        )
    )

    message = create_message(
        subject,
        body,
    )

    recipient_count = send_message(
        message
    )

    # State changes only AFTER successful email send.
    mark_waiting_concept(
        package,
        subject,
    )

    print(
        "Concept selection email sent."
    )

    print(
        "Image concepts: 3"
    )

    print(
        "Titles: 3"
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
        f"Recipients: "
        f"{recipient_count}"
    )

    print(
        f"Subject: "
        f"{subject}"
    )

    print(
        "STATE: "
        "WAITING_CONCEPT_SELECTION"
    )

    return 0


def send_final_selection(
    package: dict[str, Any],
) -> int:

    subject, body = (
        build_final_email(
            package
        )
    )

    previews = validate_three_previews(
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

    # State changes only AFTER successful email send.
    mark_waiting_final(
        package,
        subject,
    )

    print(
        "Final image + title "
        "selection email sent."
    )

    print(
        "Attached real images: 3"
    )

    print(
        "Title choices: 3"
    )

    print(
        "Final format:"
    )

    print(
        "IMAGE_NUMBER TITLE_NUMBER"
    )

    print(
        "Example:"
    )

    print(
        "2 1"
    )

    print(
        "Full-width example:"
    )

    print(
        "２ １"
    )

    print(
        "Regeneration:"
    )

    print(
        "NEXT 3"
    )

    print(
        "Full-width regeneration:"
    )

    print(
        "ＮＥＸＴ ３"
    )

    print(
        f"Recipients: "
        f"{recipient_count}"
    )

    print(
        f"Subject: "
        f"{subject}"
    )

    print(
        "STATE: "
        "WAITING_FINAL_SELECTION"
    )

    return 0


# ============================================================
# Main
# ============================================================

def main() -> int:

    package = load_json(
        OPTIONS_PATH
    )

    state = first_text(
        package.get(
            "state"
        )
    ).upper()

    print(
        f"Current design state: "
        f"{state}"
    )

    # --------------------------------------------------------
    # Stage 1
    # --------------------------------------------------------

    if state in (
        "CONCEPTS_READY",
        "WAITING_CONCEPT_SELECTION",
    ):

        return send_concept_selection(
            package
        )

    # --------------------------------------------------------
    # Stage 2
    # --------------------------------------------------------

    if state in (
        "DESIGN_PREVIEWS_READY",
        "WAITING_FINAL_SELECTION",
    ):

        return send_final_selection(
            package
        )

    raise ValueError(
        "send_design_approval_email.py "
        "cannot run in state "
        f"{state!r}. "
        "Expected CONCEPTS_READY, "
        "WAITING_CONCEPT_SELECTION, "
        "DESIGN_PREVIEWS_READY, or "
        "WAITING_FINAL_SELECTION."
    )


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
