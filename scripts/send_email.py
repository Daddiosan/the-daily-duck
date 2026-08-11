#!/usr/bin/env python3
"""
The Daily Duck - Gate A editorial email (Phase 2 compatible)

Correct Phase 1 -> Phase 2 flow:

news_candidates.json
    ->
ai_ranked_news.json
    ->
THIS SCRIPT creates gate_a_package.json
    ->
generates exactly five IMAGE CONCEPTS
    ->
sends Gate A email
    ->
user replies exactly:
    1 OK / 2 OK / 3 OK / 4 OK / 5 OK

Important:
- gate_a_package.json does NOT need to exist before this script runs.
- The script loads ai_ranked_news.json and creates gate_a_package.json itself.
- Plain "OK" is no longer a valid Gate A approval.

Environment:
- GEMINI_API_KEY
- GMAIL_ADDRESS
- GMAIL_APP_PASSWORD
- EMAIL_TO

Optional:
- GEMINI_TEXT_MODEL (default: gemini-3.6-flash)
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from google import genai


# ============================================================
# Paths / configuration
# ============================================================

RANKED_PATH = Path("ai_ranked_news.json")
PACKAGE_PATH = Path("gate_a_package.json")
EMAIL_TEXT_PATH = Path("daily_duck_email.txt")

TEXT_MODEL = os.getenv(
    "GEMINI_TEXT_MODEL",
    "gemini-3.6-flash",
)


# ============================================================
# Basic helpers
# ============================================================

def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


def load_json(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(
            f"Required file not found: {path}"
        )

    with path.open(
        "r",
        encoding="utf-8",
    ) as f:
        return json.load(f)


def first_value(
    data: dict[str, Any],
    *keys: str,
    default: str = "",
) -> str:
    for key in keys:
        value = data.get(key)

        if value is not None:
            text = str(value).strip()

            if text:
                return text

    return default


def first_any(
    data: dict[str, Any],
    keys: list[str],
) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


# ============================================================
# Normalize ranked-news input
# ============================================================

def extract_ranked_list(
    ranked: Any,
) -> list[Any]:
    """
    Supports several possible Phase 1 JSON shapes.

    Examples:
      {"top5": [...]}
      {"top_5": [...]}
      {"ranked_news": [...]}
      {"candidates": [...]}
      [...]
    """

    if isinstance(ranked, list):
        return ranked

    if not isinstance(ranked, dict):
        raise ValueError(
            "ai_ranked_news.json must be a JSON object or array."
        )

    possible = first_any(
        ranked,
        [
            "top5",
            "top_5",
            "top5_stories",
            "top_stories",
            "ranked_news",
            "ranked",
            "stories",
            "candidates",
            "results",
        ],
    )

    if isinstance(possible, list):
        return possible

    # Sometimes recommendation + shortlist are nested.
    for value in ranked.values():
        if isinstance(value, dict):
            nested = first_any(
                value,
                [
                    "top5",
                    "top_5",
                    "top_stories",
                    "ranked_news",
                    "stories",
                    "candidates",
                ],
            )
            if isinstance(nested, list):
                return nested

    return []


def extract_recommended_story(
    ranked: Any,
    shortlist: list[Any],
) -> Any:
    if isinstance(ranked, dict):
        recommended = first_any(
            ranked,
            [
                "recommended_story",
                "recommendation",
                "recommended",
                "winner",
                "selected_story",
                "top_pick",
                "best_story",
            ],
        )

        if recommended is not None:
            # Some schemas store an index / rank instead of object.
            if isinstance(recommended, int):
                idx = recommended - 1

                if 0 <= idx < len(shortlist):
                    return shortlist[idx]

            return recommended

    if shortlist:
        return shortlist[0]

    return {}


def normalize_story(
    story: Any,
) -> dict[str, Any]:
    if isinstance(story, dict):
        return dict(story)

    return {
        "title": str(story),
    }


# ============================================================
# Build Gate A package from Phase 1 output
# ============================================================

def build_gate_a_package(
    ranked: Any,
) -> dict[str, Any]:
    if isinstance(ranked, dict):
        package = dict(ranked)
    else:
        package = {
            "ranked_news": ranked,
        }

    shortlist_raw = extract_ranked_list(
        ranked
    )

    shortlist = [
        normalize_story(item)
        for item in shortlist_raw[:5]
    ]

    recommended_raw = extract_recommended_story(
        ranked,
        shortlist_raw,
    )

    recommended = normalize_story(
        recommended_raw
    )

    # Preserve/standardize fields used later by Gate A + Phase 2.
    package["recommended_story"] = recommended
    package["top5"] = shortlist

    issue_date = first_value(
        package,
        "date",
        "issue_date",
        "publication_date",
        default=datetime.now().date().isoformat(),
    )

    package["date"] = issue_date
    package["issue_date"] = issue_date

    # Copy likely editorial fields from recommendation to top-level
    # only when they are not already present.
    aliases: dict[str, list[str]] = {
        "title": [
            "title",
            "headline",
            "name",
        ],
        "summary": [
            "summary",
            "description",
            "reason",
            "why_selected",
        ],
        "source": [
            "source",
            "source_name",
            "publisher",
        ],
        "source_url": [
            "source_url",
            "url",
            "link",
        ],
        "jp_copy": [
            "jp_copy",
            "story_jp",
            "copy_jp",
            "japanese_copy",
            "jp",
        ],
        "en_copy": [
            "en_copy",
            "story_en",
            "copy_en",
            "english_copy",
            "en",
        ],
        "duck_name": [
            "duck_name",
            "name",
        ],
        "duck_jp": [
            "duck_jp",
            "duck_copy_jp",
            "duck_comment_jp",
        ],
        "duck_en": [
            "duck_en",
            "duck_copy_en",
            "duck_comment_en",
        ],
        "x_jp": [
            "x_jp",
            "x_copy_jp",
            "twitter_jp",
        ],
        "x_en": [
            "x_en",
            "x_copy_en",
            "twitter_en",
        ],
        "image_concept": [
            "image_concept",
            "visual_concept",
        ],
    }

    for target, source_keys in aliases.items():
        if str(package.get(target, "")).strip():
            continue

        value = first_value(
            recommended,
            *source_keys,
        )

        if value:
            package[target] = value

    package["phase"] = 2
    package["gate_a_approval_format"] = "<1-5> OK"
    package["gate_a_package_created_at"] = (
        datetime.now(timezone.utc).isoformat()
    )

    return package


# ============================================================
# Story context for concept generation
# ============================================================

def story_context(
    pkg: dict[str, Any],
) -> str:
    recommended = pkg.get(
        "recommended_story"
    )

    if not isinstance(
        recommended,
        dict,
    ):
        recommended = {}

    title = first_value(
        recommended,
        "title",
        "headline",
        "name",
        default=first_value(
            pkg,
            "title",
            "headline",
        ),
    )

    summary = first_value(
        recommended,
        "summary",
        "description",
        "reason",
        "why_selected",
        default=first_value(
            pkg,
            "summary",
            "description",
            "jp_copy",
        ),
    )

    source = first_value(
        recommended,
        "source",
        "source_name",
        "publisher",
        default=first_value(
            pkg,
            "source",
            "source_name",
        ),
    )

    url = first_value(
        recommended,
        "url",
        "source_url",
        "link",
        default=first_value(
            pkg,
            "source_url",
            "url",
            "link",
        ),
    )

    existing_concept = first_value(
        pkg,
        "image_concept",
        "visual_concept",
    )

    return "\n".join(
        [
            f"TITLE: {title}",
            f"SUMMARY: {summary}",
            f"SOURCE: {source}",
            f"URL: {url}",
            f"JP COPY: {first_value(pkg, 'jp_copy', 'story_jp', 'jp')}",
            f"EN COPY: {first_value(pkg, 'en_copy', 'story_en', 'en')}",
            f"EXISTING IMAGE IDEA: {existing_concept}",
        ]
    )


# ============================================================
# Parse Gemini JSON safely
# ============================================================

def extract_json_array(
    text: str,
) -> list[Any]:
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

    start = cleaned.find("[")
    end = cleaned.rfind("]")

    if (
        start == -1
        or end == -1
        or end <= start
    ):
        raise ValueError(
            "Gemini did not return a JSON array."
        )

    result = json.loads(
        cleaned[start : end + 1]
    )

    if not isinstance(
        result,
        list,
    ):
        raise ValueError(
            "Gemini image concepts were not a list."
        )

    return result


def normalize_concepts(
    raw: list[Any],
) -> list[dict[str, Any]]:
    if len(raw) != 5:
        raise ValueError(
            "Exactly five image concepts are required."
        )

    concepts: list[dict[str, Any]] = []

    for idx, item in enumerate(
        raw,
        start=1,
    ):
        if not isinstance(
            item,
            dict,
        ):
            raise ValueError(
                f"Image concept {idx} is not a JSON object."
            )

        title = str(
            item.get(
                "title",
                "",
            )
        ).strip()

        concept = str(
            item.get(
                "concept",
                item.get(
                    "description",
                    "",
                ),
            )
        ).strip()

        visual_direction = str(
            item.get(
                "visual_direction",
                item.get(
                    "visual",
                    "",
                ),
            )
        ).strip()

        if not title:
            title = f"Concept {idx}"

        if not concept:
            raise ValueError(
                f"Image concept {idx} has no concept description."
            )

        concepts.append(
            {
                "number": idx,
                "title": title,
                "concept": concept,
                "visual_direction": visual_direction,
            }
        )

    return concepts


# ============================================================
# Generate exactly five image concepts
# ============================================================

def generate_image_concepts(
    pkg: dict[str, Any],
) -> list[dict[str, Any]]:
    # Reuse them if an upstream stage already supplied exactly five.
    existing = pkg.get(
        "image_concepts"
    )

    if (
        isinstance(existing, list)
        and len(existing) == 5
    ):
        return normalize_concepts(
            existing
        )

    api_key = required_env(
        "GEMINI_API_KEY"
    )

    client = genai.Client(
        api_key=api_key
    )

    prompt = f"""
You are the visual editor for The Daily Duck.

The Daily Duck selects category-neutral uplifting news:
stories that make people feel happier, hopeful, amused,
inspired, warm, or positively curious.

For the story below, create EXACTLY five IMAGE CONCEPTS.

IMPORTANT:
These are concept directions only.
Do NOT generate actual images now.
The human editor will choose one concept using:
1 OK / 2 OK / 3 OK / 4 OK / 5 OK

Later, Phase 2 will generate five actual images from the selected concept.

Permanent mascot identity:
- recognizable yellow duck
- orange beak
- large dark glossy eyes
- small feather tuft
- friendly expression
- story-specific clothing and props are allowed

Brand direction:
- simple and clean
- modern editorial feeling
- warm and charming
- avoid an overly vintage look
- suitable for both The Daily Duck website hero and X post
- no long article text inside the image
- do not invent factual claims not supported by the story

The five concepts must be meaningfully different.
Vary composition, setting, action, viewpoint, props,
scale, visual metaphor, or storytelling approach.

Return ONLY valid JSON.
Return an array of EXACTLY five objects:

[
  {{
    "title": "short title",
    "concept": "2-4 sentence scene description",
    "visual_direction": "short composition/style direction"
  }},
  {{
    "title": "short title",
    "concept": "2-4 sentence scene description",
    "visual_direction": "short composition/style direction"
  }},
  {{
    "title": "short title",
    "concept": "2-4 sentence scene description",
    "visual_direction": "short composition/style direction"
  }},
  {{
    "title": "short title",
    "concept": "2-4 sentence scene description",
    "visual_direction": "short composition/style direction"
  }},
  {{
    "title": "short title",
    "concept": "2-4 sentence scene description",
    "visual_direction": "short composition/style direction"
  }}
]

STORY:
{story_context(pkg)}
""".strip()

    response = client.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
    )

    response_text = getattr(
        response,
        "text",
        None,
    )

    if not response_text:
        raise RuntimeError(
            "Gemini returned no text while generating image concepts."
        )

    raw = extract_json_array(
        response_text
    )

    return normalize_concepts(
        raw
    )


# ============================================================
# Shortlist formatting
# ============================================================

def shortlist_text(
    pkg: dict[str, Any],
) -> str:
    shortlist = (
        pkg.get("top5")
        or pkg.get("top_5")
        or pkg.get("shortlist")
        or pkg.get("candidates")
        or []
    )

    if not isinstance(
        shortlist,
        list,
    ):
        return str(shortlist)

    lines: list[str] = []

    for idx, item in enumerate(
        shortlist[:5],
        start=1,
    ):
        if isinstance(
            item,
            dict,
        ):
            title = first_value(
                item,
                "title",
                "headline",
                "name",
                default=f"Candidate {idx}",
            )

            reason = first_value(
                item,
                "reason",
                "summary",
                "description",
                "why_selected",
            )

            source = first_value(
                item,
                "source",
                "source_name",
                "publisher",
            )

            url = first_value(
                item,
                "url",
                "source_url",
                "link",
            )

            lines.append(
                f"{idx}. {title}"
            )

            if reason:
                lines.append(
                    f"   {reason}"
                )

            if source or url:
                lines.append(
                    f"   Source: {source} {url}".rstrip()
                )

        else:
            lines.append(
                f"{idx}. {item}"
            )

    return "\n".join(lines)


# ============================================================
# Email
# ============================================================

def build_email(
    pkg: dict[str, Any],
    concepts: list[dict[str, Any]],
) -> tuple[str, str]:
    issue_date = first_value(
        pkg,
        "date",
        "issue_date",
        default=datetime.now().date().isoformat(),
    )

    subject = (
        f"The Daily Duck — Story Approval — {issue_date}"
    )

    recommended = pkg.get(
        "recommended_story"
    )

    if not isinstance(
        recommended,
        dict,
    ):
        recommended = {}

    rec_title = first_value(
        recommended,
        "title",
        "headline",
        "name",
        default=first_value(
            pkg,
            "title",
            "headline",
        ),
    )

    rec_summary = first_value(
        recommended,
        "summary",
        "description",
        "reason",
        "why_selected",
        default=first_value(
            pkg,
            "summary",
            "description",
        ),
    )

    rec_source = first_value(
        recommended,
        "source",
        "source_name",
        "publisher",
        default=first_value(
            pkg,
            "source",
            "source_name",
        ),
    )

    rec_url = first_value(
        recommended,
        "url",
        "source_url",
        "link",
        default=first_value(
            pkg,
            "source_url",
            "url",
            "link",
        ),
    )

    concept_lines: list[str] = []

    for concept in concepts:
        concept_lines.extend(
            [
                f"[{concept['number']}] {concept['title']}",
                str(concept["concept"]),
                (
                    "Visual: "
                    + str(
                        concept.get(
                            "visual_direction",
                            "",
                        )
                    )
                ),
                "",
            ]
        )

    body = f"""The Daily Duck — Gate A

今日の記事・コピーと、
使用する画像コンセプトを同時に承認してください。

==================================================
RECOMMENDED STORY
==================================================

{rec_title}

{rec_summary}

Source:
{rec_source}

{rec_url}

==================================================
TOP 5 SHORTLIST
==================================================

{shortlist_text(pkg)}

==================================================
EDITORIAL COPY
==================================================

JP:
{first_value(pkg, "jp_copy", "story_jp", "jp")}

EN:
{first_value(pkg, "en_copy", "story_en", "en")}

Duck name:
{first_value(pkg, "duck_name", "name")}

Duck JP:
{first_value(pkg, "duck_jp", "duck_copy_jp")}

Duck EN:
{first_value(pkg, "duck_en", "duck_copy_en")}

X JP:
{first_value(pkg, "x_jp", "x_copy_jp")}

X EN:
{first_value(pkg, "x_en", "x_copy_en")}

==================================================
IMAGE CONCEPTS — CHOOSE ONE
==================================================

{chr(10).join(concept_lines).rstrip()}

==================================================
GATE A APPROVAL
==================================================

使用したい画像コンセプト番号 + OK だけを返信してください。

1 OK
2 OK
3 OK
4 OK
5 OK

例:

3 OK

IMPORTANT:

- 「OK」だけでは承認されません。
- 1〜5のコンセプト番号が必要です。
- 上記5つ以外の返信では APPROVED_STORY に進みません。
- この段階では実画像は生成しません。
- Gate B完了前にWebサイト/Xへ公開しません。
"""

    return (
        subject,
        body,
    )


# ============================================================
# Main
# ============================================================

def main() -> int:
    # --------------------------------------------------------
    # Phase 1 output
    # --------------------------------------------------------

    ranked = load_json(
        RANKED_PATH
    )

    # --------------------------------------------------------
    # THIS SCRIPT creates Gate A package.
    # gate_a_package.json is NOT an input requirement.
    # --------------------------------------------------------

    package = build_gate_a_package(
        ranked
    )

    # --------------------------------------------------------
    # Create exactly five image concept choices.
    # --------------------------------------------------------

    concepts = generate_image_concepts(
        package
    )

    package["image_concepts"] = (
        concepts
    )

    package[
        "gate_a_approval_format"
    ] = "<1-5> OK"

    package[
        "gate_a_updated_at"
    ] = datetime.now(
        timezone.utc
    ).isoformat()

    # --------------------------------------------------------
    # Save package BEFORE artifact upload.
    # --------------------------------------------------------

    PACKAGE_PATH.write_text(
        json.dumps(
            package,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # --------------------------------------------------------
    # Build email.
    # --------------------------------------------------------

    subject, body = build_email(
        package,
        concepts,
    )

    EMAIL_TEXT_PATH.write_text(
        body,
        encoding="utf-8",
    )

    # --------------------------------------------------------
    # Gmail SMTP.
    # --------------------------------------------------------

    gmail_address = required_env(
        "GMAIL_ADDRESS"
    )

    gmail_password = required_env(
        "GMAIL_APP_PASSWORD"
    )

    recipients = [
        value.strip()
        for value in required_env(
            "EMAIL_TO"
        ).split(",")
        if value.strip()
    ]

    if not recipients:
        raise RuntimeError(
            "EMAIL_TO contains no valid recipients."
        )

    msg = EmailMessage()

    msg["From"] = (
        gmail_address
    )

    msg["To"] = ", ".join(
        recipients
    )

    msg["Subject"] = (
        subject
    )

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

    # --------------------------------------------------------
    # Logs
    # --------------------------------------------------------

    print(
        f"Gate A package created: {PACKAGE_PATH}"
    )

    print(
        "Exactly five image concepts created."
    )

    print(
        f"Gate A email sent to {len(recipients)} recipient(s)."
    )

    print(
        f"Subject: {subject}"
    )

    print(
        "Valid Gate A replies:"
    )

    print(
        "1 OK / 2 OK / 3 OK / 4 OK / 5 OK"
    )

    print(
        "STATE: WAITING_STORY_APPROVAL"
    )

    return 0


# ============================================================
# Entry point
# ============================================================

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
