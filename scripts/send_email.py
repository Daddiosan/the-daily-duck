#!/usr/bin/env python3
from __future__ import annotations
from gemini_retry import call_with_retry

import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from google import genai


RANKED_PATH = Path("ai_ranked_news.json")
PACKAGE_PATH = Path("gate_a_package.json")
EMAIL_TEXT_PATH = Path("daily_duck_email.txt")

TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-3.6-flash")

JST = ZoneInfo("Asia/Tokyo")


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

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(
            f"{path} must contain a JSON object."
        )

    return data


def load_top_five(
    ranked: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:

    top_five = ranked.get("top_five")

    if not isinstance(top_five, list) or len(top_five) < 5:
        raise ValueError(
            "ai_ranked_news.json must contain "
            "at least five items in 'top_five'."
        )

    normalized: list[dict[str, Any]] = []

    for item in top_five[:5]:
        if not isinstance(item, dict):
            raise ValueError(
                "Every item in 'top_five' "
                "must be a JSON object."
            )

        normalized.append(
            dict(item)
        )

    recommended_id = str(
        ranked.get(
            "recommended_id",
            "",
        )
    ).strip()

    return normalized, recommended_id


def clean_json_text(
    text: str,
) -> str:

    cleaned = text.strip()

    cleaned = re.sub(
        r"^```(?:json)?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    cleaned = re.sub(
        r"\s*```$",
        "",
        cleaned,
    )

    return cleaned.strip()


def generate_five_editorial_packages(
    ranked: dict[str, Any],
    top_five: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Generate a complete Daily Duck editorial package
    for ALL five stories.

    One Gemini request is used so that:
    - exactly five candidates are returned
    - relative tone/quality can stay consistent
    - API usage stays simpler than five independent requests
    """

    client = genai.Client(
        api_key=required_env(
            "GEMINI_API_KEY"
        )
    )

    source_stories: list[
        dict[str, Any]
    ] = []

    for story in top_five:
        source_stories.append(
            {
                "id":
                    story.get("id"),

                "title":
                    story.get(
                        "title",
                        "",
                    ),

                "source":
                    story.get(
                        "source",
                        "",
                    ),

                "url":
                    story.get(
                        "url",
                        "",
                    ),

                "reason":
                    story.get(
                        "reason",
                        "",
                    ),

                "total_score":
                    story.get(
                        "total_score"
                    ),
            }
        )

    output_example = {
        "stories": [
            {
                "id":
                    "exact original story id",

                "title_ja":
                    "自然な日本語タイトル",

                "reason_ja":
                    "日本語での記事選定理由・要約",

                "jp_copy":
                    "Daily Duck用の日本語記事本文",

                "en_copy":
                    "Daily Duck English editorial copy",

                "duck_name":
                    "short English duck/story nickname",

                "duck_jp":
                    "短い遊び心のある日本語Duckコメント",

                "duck_en":
                    "short playful English duck line",

                "x_jp":
                    "日本語X投稿文",

                "x_en":
                    "English X post draft",
            }
            for _ in range(5)
        ]
    }

    prompt = """
You are the editorial assistant for The Daily Duck.

The Daily Duck publishes one uplifting news story each day.

Editorial philosophy:

- category-neutral
- happy
- hopeful
- warm
- amusing
- inspiring
- surprising
- positively curious

Do NOT favor science, technology, nature, space,
or any particular category.

You are given exactly five candidate news stories.

IMPORTANT:
Create a COMPLETE editorial package for EACH
of the five stories.

The human editor will receive all five finished
editorial proposals and choose ONE by replying
with the number 1, 2, 3, 4, or 5.

STRICT FACTUAL RULE:

Use ONLY factual information present in the
supplied story data.

Do not invent:

- names
- people
- dates
- numbers
- locations
- quotations
- scientific claims
- study results
- organizations
- background facts

If the source information is limited, keep the
article general rather than adding facts.

JAPANESE:

Japanese must sound natural and readable.
Do not make it sound like a literal machine
translation.

ENGLISH:

English must be natural, concise and friendly.

EDITORIAL COPY:

jp_copy and en_copy should each be a short
Daily Duck article suitable for publication.

They should explain why the story is interesting
and leave the reader feeling positive.

DUCK COPY:

duck_jp and duck_en may be playful, but must
not introduce unsupported facts.

X COPY:

x_jp and x_en should be concise social-media copy.
Do NOT include URLs in the generated X fields.
The publication system adds the Daily Duck URL later.

OUTPUT RULES:

- Return exactly five stories.
- Preserve every supplied story ID EXACTLY.
- Return the stories in the SAME ORDER as supplied.
- Every field must contain non-empty text.
- Return ONLY valid JSON.
- Do not use Markdown fences.

Return exactly this JSON structure:

""" + json.dumps(
        output_example,
        ensure_ascii=False,
        indent=2,
    ) + """

FIVE SOURCE STORIES:

""" + json.dumps(
        source_stories,
        ensure_ascii=False,
        indent=2,
    )

    response = call_with_retry(
        lambda: client.models.generate_content(
            model=TEXT_MODEL,
            contents=prompt.strip(),
        ),
        label="Gemini generate_content",
    )

    response_text = getattr(
        response,
        "text",
        None,
    )

    if not response_text:
        raise RuntimeError(
            "Gemini returned no editorial text."
        )

    try:
        generated = json.loads(
            clean_json_text(
                response_text
            )
        )

    except json.JSONDecodeError as exc:
        raise ValueError(
            "Gemini did not return valid JSON: "
            f"{exc}"
        ) from exc

    if not isinstance(
        generated,
        dict,
    ):
        raise ValueError(
            "Gemini editorial response "
            "must be a JSON object."
        )

    generated_stories = (
        generated.get(
            "stories"
        )
    )

    if (
        not isinstance(
            generated_stories,
            list,
        )
        or len(
            generated_stories
        ) != 5
    ):
        raise ValueError(
            "Gemini must return exactly "
            "five editorial stories."
        )

    expected_ids = [
        str(
            story.get(
                "id",
                "",
            )
        ).strip()
        for story in top_five
    ]

    output: list[
        dict[str, Any]
    ] = []

    required_fields = [
        "title_ja",
        "reason_ja",
        "jp_copy",
        "en_copy",
        "duck_name",
        "duck_jp",
        "duck_en",
        "x_jp",
        "x_en",
    ]

    for index, editorial in enumerate(
        generated_stories
    ):

        if not isinstance(
            editorial,
            dict,
        ):
            raise ValueError(
                "Every generated story "
                "must be an object."
            )

        generated_id = str(
            editorial.get(
                "id",
                "",
            )
        ).strip()

        expected_id = (
            expected_ids[index]
        )

        if (
            generated_id
            != expected_id
        ):
            raise ValueError(
                "Gemini changed or reordered "
                "story IDs. "
                f"Expected {expected_id!r}, "
                f"got {generated_id!r}."
            )

        for field in required_fields:

            value = editorial.get(
                field
            )

            if (
                not isinstance(
                    value,
                    str,
                )
                or not value.strip()
            ):
                raise ValueError(
                    f"Story {index + 1} "
                    f"is missing non-empty "
                    f"'{field}'."
                )

        original = dict(
            top_five[index]
        )

        combined = {
            **original,

            "candidate_number":
                index + 1,

            "title_ja":
                editorial[
                    "title_ja"
                ].strip(),

            "reason_ja":
                editorial[
                    "reason_ja"
                ].strip(),

            "jp_copy":
                editorial[
                    "jp_copy"
                ].strip(),

            "en_copy":
                editorial[
                    "en_copy"
                ].strip(),

            "duck_name":
                editorial[
                    "duck_name"
                ].strip(),

            "duck_jp":
                editorial[
                    "duck_jp"
                ].strip(),

            "duck_en":
                editorial[
                    "duck_en"
                ].strip(),

            "x_jp":
                editorial[
                    "x_jp"
                ].strip(),

            "x_en":
                editorial[
                    "x_en"
                ].strip(),
        }

        output.append(
            combined
        )

    return output


def build_package(
    ranked: dict[str, Any],
    story_options: list[
        dict[str, Any]
    ],
    recommended_id: str,
) -> dict[str, Any]:

    # Daily Duck issue dates are based on JST,
    # regardless of the GitHub Actions runner timezone.
    issue_date = (
        datetime.now(JST)
        .date()
        .isoformat()
    )

    recommended_number = None

    for item in story_options:

        if (
            str(
                item.get(
                    "id",
                    "",
                )
            )
            == recommended_id
        ):

            recommended_number = (
                item[
                    "candidate_number"
                ]
            )

            break

    return {
        "date":
            issue_date,

        "issue_date":
            issue_date,

        "phase":
            2,

        "state":
            "WAITING_STORY_SELECTION",

        "recommended_id":
            recommended_id,

        "recommended_number":
            recommended_number,

        "recommended_reason":
            ranked.get(
                "recommended_reason",
                "",
            ),

        "story_options":
            story_options,

        # Compatibility aliases
        "top_five":
            story_options,

        "top5":
            story_options,

        "gate_a_approval_format":
            "1-5",

        "gate_a_valid_replies":
            [
                "1",
                "2",
                "3",
                "4",
                "5",
            ],

        # Precise machine timestamp remains UTC.
        "gate_a_package_created_at":
            datetime.now(
                timezone.utc
            ).isoformat(),
    }


def format_story_option(
    story: dict[str, Any],
    recommended_number: int | None,
) -> str:

    number = story[
        "candidate_number"
    ]

    recommendation_label = ""

    if (
        number
        == recommended_number
    ):

        recommendation_label = (
            "\n"
            "★ AI RECOMMENDED / "
            "AIおすすめ"
        )

    score_line = ""

    if (
        story.get(
            "total_score"
        )
        is not None
    ):

        score_line = (
            "\nScore: "
            f"{story['total_score']}"
        )

    return f"""
==================================================
候補 {number}{recommendation_label}
==================================================

TITLE / タイトル

{story.get('title_ja', '')}

EN:
{story.get('title', '')}


WHY THIS STORY / 記事のポイント

{story.get('reason_ja', '')}


SOURCE

{story.get('source', '')}
{story.get('url', '')}
{score_line}


--------------------------------------------------
EDITORIAL COPY / 記事案
--------------------------------------------------

JP:

{story.get('jp_copy', '')}


EN:

{story.get('en_copy', '')}


--------------------------------------------------
DUCK
--------------------------------------------------

Duck name:
{story.get('duck_name', '')}

Duck JP:
{story.get('duck_jp', '')}

Duck EN:
{story.get('duck_en', '')}


--------------------------------------------------
X DRAFT
--------------------------------------------------

X JP:

{story.get('x_jp', '')}


X EN:

{story.get('x_en', '')}
""".strip()


def build_email(
    package: dict[str, Any],
) -> tuple[str, str]:

    subject = (
        "The Daily Duck — "
        "Choose Today's Story — "
        f"{package['issue_date']}"
    )

    sections: list[str] = []

    for story in package[
        "story_options"
    ]:

        sections.append(
            format_story_option(
                story,
                package.get(
                    "recommended_number"
                ),
            )
        )

    candidates_text = (
        "\n\n\n".join(
            sections
        )
    )

    recommended_text = ""

    if package.get(
        "recommended_number"
    ):

        recommended_text = (
            "\n"
            "AIおすすめ: "
            "候補 "
            f"{package['recommended_number']}"
            "\n"
        )

    body = f"""
The Daily Duck — Gate A

本日の候補ニュース5件について、
それぞれ完成した記事案を作成しました。

5件を比較して、
本日採用する記事を1つ選んでください。

{recommended_text}

{candidates_text}


==================================================
GATE A — STORY SELECTION / 本日の記事を選択
==================================================

採用する記事の番号だけを返信してください。

1
2
3
4
5


例:

3


IMPORTANT:

- 「1」「2」「3」「4」「5」のいずれか1文字だけを返信してください。
- それ以外の返信では承認されません。
- 選択した1件だけが本日の正式記事になります。
- 記事選択後、その記事専用の画像コンセプトを5案作成します。
- 画像コンセプトは別メールで送信します。
- 画像コンセプト選択後、その1つのコンセプトから実画像を5枚生成します。
- 最終画像選択が終わるまでWebサイト/Xには公開しません。
""".strip()

    return subject, body


def send_email(
    subject: str,
    body: str,
) -> int:

    gmail_address = (
        required_env(
            "GMAIL_ADDRESS"
        )
    )

    gmail_password = (
        required_env(
            "GMAIL_APP_PASSWORD"
        )
    )

    recipients = [
        x.strip()
        for x in required_env(
            "EMAIL_TO"
        ).split(",")
        if x.strip()
    ]

    if not recipients:
        raise RuntimeError(
            "EMAIL_TO contains "
            "no valid recipients."
        )

    msg = EmailMessage()

    msg[
        "From"
    ] = gmail_address

    msg[
        "To"
    ] = ", ".join(
        recipients
    )

    msg[
        "Subject"
    ] = subject

    msg.set_content(
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
            msg
        )

    return len(
        recipients
    )


def main() -> int:

    ranked = load_json(
        RANKED_PATH
    )

    (
        top_five,
        recommended_id,
    ) = load_top_five(
        ranked
    )

    print(
        "Generating complete editorial copy "
        "for all five stories..."
    )

    story_options = (
        generate_five_editorial_packages(
            ranked,
            top_five,
        )
    )

    package = build_package(
        ranked,
        story_options,
        recommended_id,
    )

    PACKAGE_PATH.write_text(
        json.dumps(
            package,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    subject, body = (
        build_email(
            package
        )
    )

    EMAIL_TEXT_PATH.write_text(
        body,
        encoding="utf-8",
    )

    recipient_count = (
        send_email(
            subject,
            body,
        )
    )

    print(
        "Gate A package created: "
        f"{PACKAGE_PATH}"
    )

    print(
        "Issue date (JST): "
        f"{package['issue_date']}"
    )

    print(
        "Story candidates: 5"
    )

    print(
        "AI recommended candidate: "
        f"{package.get('recommended_number')}"
    )

    print(
        "Gate A email sent to "
        f"{recipient_count} "
        "recipient(s)."
    )

    print(
        f"Subject: {subject}"
    )

    print(
        "Valid replies: "
        "1 / 2 / 3 / 4 / 5"
    )

    print(
        "STATE: "
        "WAITING_STORY_SELECTION"
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
