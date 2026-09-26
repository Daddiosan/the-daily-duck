#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    # Running as `python scripts/send_email.py` (production/workflow
    # invocation): the repo root is not on sys.path, only scripts/ is.
    from scripts import llm_provider
except ImportError:
    # Running as a script directly: scripts/ itself is on sys.path, so
    # llm_provider is a plain sibling module (matches rank_news_with_ai.py's
    # existing import pattern for the same module).
    import llm_provider

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


RANKED_PATH = Path("ai_ranked_news.json")
PACKAGE_PATH = Path("gate_a_package.json")
EMAIL_TEXT_PATH = Path("daily_duck_email.txt")

TEXT_MODEL = os.getenv(
    "GEMINI_TEXT_MODEL",
    "gemini-3.6-flash",
)

JST = ZoneInfo("Asia/Tokyo")


# ============================================================
# Environment helpers
# ============================================================

def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


def get_issue_date() -> str:
    """
    Return the Daily Duck issue date.

    Manual rerun:
      DAILY_DUCK_TARGET_DATE=2026-08-21

    Normal scheduled run:
      current JST date

    The supplied date must use YYYY-MM-DD format.
    """

    target_date = os.getenv(
        "DAILY_DUCK_TARGET_DATE",
        "",
    ).strip()

    if target_date:

        try:
            parsed = datetime.strptime(
                target_date,
                "%Y-%m-%d",
            )

        except ValueError as exc:
            raise ValueError(
                "DAILY_DUCK_TARGET_DATE must "
                "use YYYY-MM-DD format. "
                f"Received: {target_date!r}"
            ) from exc

        issue_date = (
            parsed.date().isoformat()
        )

        print(
            "Using manually specified "
            f"Daily Duck issue date: {issue_date}"
        )

        return issue_date

    issue_date = (
        datetime.now(
            JST
        )
        .date()
        .isoformat()
    )

    print(
        "Using current JST "
        f"Daily Duck issue date: {issue_date}"
    )

    return issue_date


# ============================================================
# JSON helpers
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


def load_top_five(
    ranked: dict[str, Any],
) -> tuple[
    list[dict[str, Any]],
    str,
]:

    top_five = ranked.get(
        "top_five"
    )

    if (
        not isinstance(
            top_five,
            list,
        )
        or len(top_five) < 5
    ):
        raise ValueError(
            "ai_ranked_news.json must contain "
            "at least five items in 'top_five'."
        )

    normalized: list[
        dict[str, Any]
    ] = []

    for item in top_five[:5]:

        if not isinstance(
            item,
            dict,
        ):
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

    return (
        normalized,
        recommended_id,
    )


# ============================================================
# Editorial generation
#
# scripts/llm_provider.py is the sole retry/fallback owner for this
# operation (Gemini primary, OpenAI fallback, bounded attempts on each
# side; see PHASE_B_RETRY_NORMALIZATION_AND_EDITORIAL_FALLBACK_PLAN.md).
# This module must not retry on its own.
# ============================================================

REQUIRED_EDITORIAL_FIELDS = [
    "title_en",
    "reason_en",
    "en_copy",
    "duck_name",
    "duck_en",
    "x_en",
    "title_ja",
    "reason_ja",
    "jp_copy",
    "duck_jp",
    "x_jp",
]


def build_editorial_schema() -> dict[str, Any]:
    story_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            **{field: {"type": "string"} for field in REQUIRED_EDITORIAL_FIELDS},
        },
        "required": ["id", *REQUIRED_EDITORIAL_FIELDS],
    }

    return {
        "type": "object",
        "properties": {
            "stories": {
                "type": "array",
                "minItems": 5,
                "maxItems": 5,
                "items": story_schema,
            },
        },
        "required": ["stories"],
    }


def validate_editorial_result(
    generated: Any,
    expected_ids: list[str],
) -> None:
    """
    Provider-independent validation applied identically to both a
    Gemini-sourced and an OpenAI-sourced editorial response. Raises on
    any invalid/unacceptable result; llm_provider.generate_editorial()
    is the only caller.
    """

    if not isinstance(generated, dict):
        raise ValueError(
            "Editorial response must be a JSON object."
        )

    generated_stories = generated.get("stories")

    if (
        not isinstance(generated_stories, list)
        or len(generated_stories) != 5
    ):
        raise ValueError(
            "Editorial response must return exactly five stories."
        )

    for index, editorial in enumerate(generated_stories):

        if not isinstance(editorial, dict):
            raise ValueError(
                "Every generated story must be an object."
            )

        generated_id = str(editorial.get("id", "")).strip()
        expected_id = expected_ids[index]

        if generated_id != expected_id:
            raise ValueError(
                "Editorial response changed or reordered story IDs. "
                f"Expected {expected_id!r}, got {generated_id!r}."
            )

        for field in REQUIRED_EDITORIAL_FIELDS:

            value = editorial.get(field)

            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"Story {index + 1} is missing non-empty '{field}'."
                )


def combine_editorial_output(
    top_five: list[dict[str, Any]],
    generated_stories: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Apply the SAME combination logic regardless of which provider
    (Gemini or OpenAI) produced generated_stories -- both were already
    checked by validate_editorial_result() against the identical contract.
    """

    output: list[dict[str, Any]] = []

    for index, editorial in enumerate(generated_stories):

        original = dict(top_five[index])

        combined = {
            **original,

            "candidate_number": index + 1,

            # English-first canonical fields
            "title_en": editorial["title_en"].strip(),
            "reason_en": editorial["reason_en"].strip(),
            "en_copy": editorial["en_copy"].strip(),
            "duck_name": editorial["duck_name"].strip(),
            "duck_en": editorial["duck_en"].strip(),
            "x_en": editorial["x_en"].strip(),

            # Japanese translation fields
            "title_ja": editorial["title_ja"].strip(),
            "reason_ja": editorial["reason_ja"].strip(),
            "jp_copy": editorial["jp_copy"].strip(),
            "duck_jp": editorial["duck_jp"].strip(),
            "x_jp": editorial["x_jp"].strip(),
        }

        output.append(combined)

    return output


def generate_five_editorial_packages(
    ranked: dict[str, Any],
    top_five: list[
        dict[str, Any]
    ],
) -> list[dict[str, Any]]:
    """
    Generate a complete Daily Duck editorial package for all five
    stories via llm_provider.generate_editorial() (Gemini primary,
    OpenAI fallback, bounded attempts -- see module header).
    """

    source_stories: list[
        dict[str, Any]
    ] = []

    for story in top_five:

        source_stories.append(
            {
                "id":
                    story.get(
                        "id"
                    ),

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

                "title_en":
                    "polished English publication title",

                "reason_en":
                    "concise English reason / summary",

                "en_copy":
                    "canonical Daily Duck English editorial copy",

                "duck_name":
                    "short English duck/story nickname",

                "duck_en":
                    "short playful English duck line",

                "x_en":
                    "canonical English X post draft",

                "title_ja":
                    "自然な日本語タイトル（英語正本の翻訳）",

                "reason_ja":
                    "日本語での記事選定理由・要約（英語正本の翻訳）",

                "jp_copy":
                    "英語正本を自然な日本語に翻訳した記事本文",

                "duck_jp":
                    "duck_enを自然な日本語にした短いDuckコメント",

                "x_jp":
                    "x_enを自然な日本語にしたX投稿文",
            }
            for _ in range(5)
        ]
    }

    prompt = """
You are the editorial assistant for The Daily Duck.

The Daily Duck publishes one uplifting news story each day.

LANGUAGE POLICY — MANDATORY:

The Daily Duck is ENGLISH-FIRST.

English is the canonical/master language.
Japanese is a translation derived from the English master.

For EACH story, create the English publication content FIRST:

1. title_en
2. reason_en
3. en_copy
4. duck_name
5. duck_en
6. x_en

Only after the English master is complete, create the Japanese
translation fields:

7. title_ja
8. reason_ja
9. jp_copy
10. duck_jp
11. x_jp

The Japanese fields must faithfully preserve the meaning, tone,
and factual limits of the English master. They should still sound
natural to a Japanese reader and must not read like awkward literal
machine translation.

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

ENGLISH MASTER:

title_en must be a concise, publication-ready English title.
It may improve readability compared with the source headline,
but it must not change the facts or add unsupported information.

reason_en must clearly explain why the story is interesting
for The Daily Duck.

en_copy is the canonical Daily Duck article.
It should be natural, concise, friendly, publication-ready,
and leave the reader feeling positive.

duck_name is a short English duck/story nickname.

duck_en may be playful, but must not introduce unsupported facts.

x_en is the canonical English X post draft.
Keep it concise and natural for social media.

Do NOT include URLs in x_en.
The publication system adds the Daily Duck URL later.

JAPANESE TRANSLATION:

title_ja, reason_ja, jp_copy, duck_jp, and x_jp
must be derived from the completed English master fields.

Japanese should be natural and readable.
Do not add facts, details, nuance, claims, or interpretations
that are not present in the English master.

Do NOT include URLs in x_jp.

OUTPUT RULES:

- Return exactly five stories.
- Preserve every supplied story ID EXACTLY.
- Return the stories in the SAME ORDER as supplied.
- Every field must contain non-empty text.
- Double-check ALL eleven text fields for ALL five stories before responding.
- In particular, NEVER omit title_en, reason_en, x_en, title_ja, reason_ja, or x_jp.
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

    expected_ids = [
        str(
            story.get(
                "id",
                "",
            )
        ).strip()
        for story in top_five
    ]

    generated, provider = llm_provider.generate_editorial(
        prompt.strip(),
        build_editorial_schema(),
        validate=lambda result: validate_editorial_result(
            result,
            expected_ids,
        ),
        gemini_model=TEXT_MODEL,
    )

    print(
        f"Editorial generation provider: {provider}"
    )

    return combine_editorial_output(
        top_five,
        generated["stories"],
    )


# ============================================================
# Gate A package
# ============================================================

def build_package(
    ranked: dict[str, Any],
    story_options: list[
        dict[str, Any]
    ],
    recommended_id: str,
) -> dict[str, Any]:

    issue_date = get_issue_date()

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

        "language_policy": {
            "primary_language":
                "en",
            "canonical_language":
                "en",
            "translation_language":
                "ja",
            "translation_source":
                "english_master",
        },

        # Precise machine timestamp remains UTC.
        "gate_a_package_created_at":
            datetime.now(
                timezone.utc
            ).isoformat(),
    }


# ============================================================
# Email formatting
# ============================================================

def format_story_option(
    story: dict[str, Any],
    recommended_number: (
        int | None
    ),
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

    source_title = (
        story.get(
            "title",
            "",
        )
    )

    return f"""
==================================================
CANDIDATE {number} / 候補 {number}{recommendation_label}
==================================================

ENGLISH MASTER / 英語正本
--------------------------------------------------

TITLE

{story.get('title_en', '')}


WHY THIS STORY

{story.get('reason_en', '')}


EDITORIAL COPY

{story.get('en_copy', '')}


DUCK

Duck name:
{story.get('duck_name', '')}

Duck line:
{story.get('duck_en', '')}


X DRAFT

{story.get('x_en', '')}


==================================================
JAPANESE TRANSLATION / 日本語訳
==================================================

タイトル

{story.get('title_ja', '')}


記事のポイント

{story.get('reason_ja', '')}


記事案

{story.get('jp_copy', '')}


Duckコメント

{story.get('duck_jp', '')}


X投稿文

{story.get('x_jp', '')}


==================================================
SOURCE / 出典
==================================================

Original headline:
{source_title}

Source:
{story.get('source', '')}

URL:
{story.get('url', '')}
{score_line}
""".strip()


def build_email(
    package: dict[str, Any],
    approval_token: str,
) -> tuple[
    str,
    str,
]:

    subject_prefix = (
        "The Daily Duck — "
        "Choose Today's Story — "
        f"{package['issue_date']}"
    )
    subject = subject_with_approval_token(subject_prefix, approval_token)

    sections: list[
        str
    ] = []

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

The Daily Duck is now ENGLISH-FIRST.
English is the canonical/master copy.
Japanese is provided as a translation for review.

本日の候補ニュース5件について、
英語を正式原稿（Master）として記事案を作成しました。
日本語は英語正本を基にした確認用の翻訳です。

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
- 英語版がcanonical/master copyです。
- 日本語版は英語正本から生成された翻訳です。
- 記事選択後、その記事専用の画像コンセプトを3案作成します。
- 画像コンセプトは別メールで送信します。
- 画像コンセプト選択後、その1つのコンセプトから実画像を5枚生成します。
- 最終画像選択が終わるまでWebサイト/Xには公開しません。
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
        for x
        in required_env(
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


# ============================================================
# Main
# ============================================================

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

    print(
        "Gemini model: "
        f"{TEXT_MODEL}"
    )

    print(
        "Gemini max attempts: "
        f"{llm_provider.GEMINI_MAX_ATTEMPTS}"
    )

    print(
        "OpenAI fallback max attempts: "
        f"{llm_provider.OPENAI_MAX_ATTEMPTS}"
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

    approval_token = generate_approval_token()
    package["approval_token_digest"] = approval_token_digest(
        stage="GATE_A",
        issue_date=str(package["issue_date"]),
        batch=None,
        token=approval_token,
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
            package,
            approval_token,
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
        "Issue date: "
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

    print("Subject: tokenized Gate A approval subject generated")

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
