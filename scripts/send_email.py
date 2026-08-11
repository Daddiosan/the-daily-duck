#!/usr/bin/env python3
"""
The Daily Duck - Gate A email generator (Phase 2)

Input:
    ai_ranked_news.json

Actual Phase 1 input schema:
    {
      "recommended_id": 28,
      "recommended_reason": "...",
      "top_five": [
        {"id": 28, "title": "...", "source": "...", "url": "...", ...},
        ...
      ]
    }

Output:
    gate_a_package.json
    daily_duck_email.txt
    Gate A email via Gmail SMTP

Gate A reply format:
    1 OK
    2 OK
    3 OK
    4 OK
    5 OK

Plain "OK" is invalid in Phase 2.

This script:
1. Resolves recommended_id against top_five.
2. Uses Gemini to create the editorial package:
   JP/EN copy, Duck JP/EN, X JP/EN, and exactly five image concepts.
3. Preserves source/title/url from ai_ranked_news.json as authoritative.
4. Saves gate_a_package.json before the workflow uploads the artifact.
5. Sends the Gate A email.
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


RANKED_PATH = Path("ai_ranked_news.json")
PACKAGE_PATH = Path("gate_a_package.json")
EMAIL_TEXT_PATH = Path("daily_duck_email.txt")

# This is the model that is working in the current GitHub/API environment.
TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-3.6-flash")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Required file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")

    return data


def resolve_recommended_story(
    ranked: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    top_five = ranked.get("top_five")

    if not isinstance(top_five, list) or not top_five:
        raise ValueError(
            "ai_ranked_news.json must contain a non-empty 'top_five' array."
        )

    normalized: list[dict[str, Any]] = []
    for item in top_five[:5]:
        if not isinstance(item, dict):
            raise ValueError("Every item in 'top_five' must be a JSON object.")
        normalized.append(dict(item))

    recommended_id = ranked.get("recommended_id")
    recommended: dict[str, Any] | None = None

    for item in normalized:
        if str(item.get("id")) == str(recommended_id):
            recommended = dict(item)
            break

    if recommended is None:
        raise ValueError(
            f"recommended_id={recommended_id!r} was not found in top_five."
        )

    recommended["recommended_reason"] = str(
        ranked.get("recommended_reason", "")
    ).strip()

    return recommended, normalized


def clean_json_text(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def generate_editorial_package(
    recommended: dict[str, Any],
    top_five: list[dict[str, Any]],
) -> dict[str, Any]:
    api_key = required_env("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    source_story = {
        "id": recommended.get("id"),
        "title": recommended.get("title", ""),
        "source": recommended.get("source", ""),
        "url": recommended.get("url", ""),
        "reason": recommended.get("reason", ""),
        "recommended_reason": recommended.get("recommended_reason", ""),
    }

    prompt = f"""
You are the editorial assistant for The Daily Duck.

The Daily Duck publishes category-neutral uplifting news.
The goal is to make readers feel happier, hopeful, amused, inspired,
warm, or positively curious.

Create the Gate A editorial package for the selected story below.

STRICT FACTUAL RULE:
Use ONLY the facts present in SOURCE STORY.
Do not invent names, numbers, dates, places, quotations, study details,
scientific findings, or other factual claims that are not present there.
If the source information is sparse, keep the copy general and faithful
rather than filling gaps.

STYLE:
- Warm, simple, intelligent, concise.
- Not clickbait.
- JP should sound natural in Japanese.
- EN should sound natural in English.
- Duck lines can be playful but must not add unsupported facts.
- X drafts should be concise and suitable for a social post.
- Do not include hashtags unless they genuinely help.
- Do not put URLs into the generated copy fields.

IMAGE CONCEPT RULES:
Create EXACTLY five meaningfully different image concepts for THIS story.
Every concept must clearly relate to the selected story.
Permanent mascot identity:
- recognizable yellow duck
- orange beak
- large dark glossy eyes
- small feather tuft
- friendly expression
Story-specific clothing and props are allowed.
Visual direction:
- simple and clean
- modern editorial feeling
- warm and charming
- not overly vintage
- suitable for both The Daily Duck website hero and X
- no long article text embedded in the image
- no unsupported factual details

Return ONLY one valid JSON object with exactly these keys:

{{
  "jp_copy": "Japanese editorial copy",
  "en_copy": "English editorial copy",
  "duck_name": "short English mascot/story nickname",
  "duck_jp": "short playful Japanese duck line",
  "duck_en": "short playful English duck line",
  "x_jp": "Japanese X draft",
  "x_en": "English X draft",
  "top_five_ja": [
    {"id": "same story id", "title_ja": "natural Japanese title", "reason_ja": "natural Japanese summary/reason"},
    {"id": "same story id", "title_ja": "natural Japanese title", "reason_ja": "natural Japanese summary/reason"},
    {"id": "same story id", "title_ja": "natural Japanese title", "reason_ja": "natural Japanese summary/reason"},
    {"id": "same story id", "title_ja": "natural Japanese title", "reason_ja": "natural Japanese summary/reason"},
    {"id": "same story id", "title_ja": "natural Japanese title", "reason_ja": "natural Japanese summary/reason"}
  ],
  "image_concepts": [
    {{
      "number": 1,
      "title": "short English title",
      "title_ja": "natural Japanese title",
      "concept": "2-4 sentence English scene description",
      "concept_ja": "natural Japanese translation of the scene description",
      "visual_direction": "short English composition/style direction",
      "visual_direction_ja": "natural Japanese translation of the visual direction"
    }},
    {{
      "number": 2,
      "title": "short English title",
      "title_ja": "natural Japanese title",
      "concept": "2-4 sentence English scene description",
      "concept_ja": "natural Japanese translation of the scene description",
      "visual_direction": "short English composition/style direction",
      "visual_direction_ja": "natural Japanese translation of the visual direction"
    }},
    {{
      "number": 3,
      "title": "short English title",
      "title_ja": "natural Japanese title",
      "concept": "2-4 sentence English scene description",
      "concept_ja": "natural Japanese translation of the scene description",
      "visual_direction": "short English composition/style direction",
      "visual_direction_ja": "natural Japanese translation of the visual direction"
    }},
    {{
      "number": 4,
      "title": "short English title",
      "title_ja": "natural Japanese title",
      "concept": "2-4 sentence English scene description",
      "concept_ja": "natural Japanese translation of the scene description",
      "visual_direction": "short English composition/style direction",
      "visual_direction_ja": "natural Japanese translation of the visual direction"
    }},
    {{
      "number": 5,
      "title": "short English title",
      "title_ja": "natural Japanese title",
      "concept": "2-4 sentence English scene description",
      "concept_ja": "natural Japanese translation of the scene description",
      "visual_direction": "short English composition/style direction",
      "visual_direction_ja": "natural Japanese translation of the visual direction"
    }}
  ]
}}

SOURCE STORY:
{json.dumps(source_story, ensure_ascii=False, indent=2)}

TOP FIVE STORIES TO TRANSLATE:
{json.dumps(top_five, ensure_ascii=False, indent=2)}
""".strip()

    response = client.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
    )

    response_text = getattr(response, "text", None)
    if not response_text:
        raise RuntimeError("Gemini returned no editorial text.")

    try:
        generated = json.loads(clean_json_text(response_text))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Gemini did not return valid JSON: {exc}"
        ) from exc

    if not isinstance(generated, dict):
        raise ValueError("Gemini editorial response must be a JSON object.")

    required_text_fields = [
        "jp_copy",
        "en_copy",
        "duck_name",
        "duck_jp",
        "duck_en",
        "x_jp",
        "x_en",
    ]

    for field in required_text_fields:
        value = generated.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Gemini response is missing non-empty '{field}'.")

    top_five_ja = generated.get("top_five_ja")
    if not isinstance(top_five_ja, list) or len(top_five_ja) != 5:
        raise ValueError("Gemini must return exactly five top_five_ja entries.")

    ja_by_id = {}
    for item in top_five_ja:
        if not isinstance(item, dict):
            raise ValueError("Every top_five_ja entry must be an object.")
        story_id = str(item.get("id", ""))
        title_ja = str(item.get("title_ja", "")).strip()
        reason_ja = str(item.get("reason_ja", "")).strip()
        if not story_id or not title_ja or not reason_ja:
            raise ValueError("Each top_five_ja entry requires id, title_ja and reason_ja.")
        ja_by_id[story_id] = {
            "title_ja": title_ja,
            "reason_ja": reason_ja,
        }

    for story in top_five:
        translation = ja_by_id.get(str(story.get("id")))
        if translation is None:
            raise ValueError(
                f"Japanese translation missing for story id={story.get('id')!r}."
            )
        story["title_ja"] = translation["title_ja"]
        story["reason_ja"] = translation["reason_ja"]

    concepts = generated.get("image_concepts")
    if not isinstance(concepts, list) or len(concepts) != 5:
        raise ValueError("Gemini must return exactly five image_concepts.")

    normalized_concepts: list[dict[str, Any]] = []
    for number, item in enumerate(concepts, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Image concept {number} must be an object.")

        title = str(item.get("title", "")).strip()
        title_ja = str(item.get("title_ja", "")).strip()
        concept = str(item.get("concept", "")).strip()
        concept_ja = str(item.get("concept_ja", "")).strip()
        visual = str(item.get("visual_direction", "")).strip()
        visual_ja = str(item.get("visual_direction_ja", "")).strip()

        if not all([title, title_ja, concept, concept_ja, visual, visual_ja]):
            raise ValueError(
                f"Image concept {number} must contain complete English/Japanese fields."
            )

        normalized_concepts.append(
            {
                "number": number,
                "title": title,
                "title_ja": title_ja,
                "concept": concept,
                "concept_ja": concept_ja,
                "visual_direction": visual,
                "visual_direction_ja": visual_ja,
            }
        )

    generated["image_concepts"] = normalized_concepts
    return generated


def build_package(
    ranked: dict[str, Any],
    recommended: dict[str, Any],
    top_five: list[dict[str, Any]],
    editorial: dict[str, Any],
) -> dict[str, Any]:
    issue_date = datetime.now().date().isoformat()

    package: dict[str, Any] = {
        "date": issue_date,
        "issue_date": issue_date,
        "phase": 2,
        "state": "WAITING_STORY_APPROVAL",
        "recommended_id": ranked.get("recommended_id"),
        "recommended_reason": ranked.get("recommended_reason", ""),
        "recommended_story": recommended,
        "top_five": top_five,
        # Compatibility alias for Phase 2 scripts.
        "top5": top_five,
        "jp_copy": editorial["jp_copy"].strip(),
        "en_copy": editorial["en_copy"].strip(),
        "duck_name": editorial["duck_name"].strip(),
        "duck_jp": editorial["duck_jp"].strip(),
        "duck_en": editorial["duck_en"].strip(),
        "x_jp": editorial["x_jp"].strip(),
        "x_en": editorial["x_en"].strip(),
        "source": str(recommended.get("source", "")).strip(),
        "source_url": str(recommended.get("url", "")).strip(),
        "image_concepts": editorial["image_concepts"],
        "gate_a_approval_format": "<1-5> OK",
        "gate_a_package_created_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    return package


def format_top_five(top_five: list[dict[str, Any]]) -> str:
    lines: list[str] = []

    for index, item in enumerate(top_five, start=1):
        title = str(item.get("title", "")).strip()
        title_ja = str(item.get("title_ja", "")).strip()
        source = str(item.get("source", "")).strip()
        url = str(item.get("url", "")).strip()
        score = item.get("total_score")
        reason = str(item.get("reason", "")).strip()
        reason_ja = str(item.get("reason_ja", "")).strip()

        lines.append(f"{index}. {title_ja}")
        lines.append(f"   EN: {title}")

        meta: list[str] = []
        if source:
            meta.append(source)
        if score is not None:
            meta.append(f"Score: {score}")
        if meta:
            lines.append("   " + " | ".join(meta))

        if reason_ja:
            lines.append(f"   日本語: {reason_ja}")
        if reason:
            lines.append(f"   English: {reason}")
        if url:
            lines.append(f"   {url}")

        lines.append("")

    return "\n".join(lines).rstrip()


def format_image_concepts(concepts: list[dict[str, Any]]) -> str:
    lines: list[str] = []

    for item in concepts:
        number = item["number"]
        lines.extend(
            [
                f"[{number}] {item['title_ja']} / {item['title']}",
                "",
                "日本語:",
                item["concept_ja"],
                f"構図・スタイル: {item['visual_direction_ja']}",
                "",
                "English:",
                item["concept"],
                f"Visual: {item['visual_direction']}",
                "",
            ]
        )

    return "\n".join(lines).rstrip()


def build_email(
    package: dict[str, Any],
) -> tuple[str, str]:
    story = package["recommended_story"]
    issue_date = package["issue_date"]

    subject = f"The Daily Duck — Story Approval — {issue_date}"

    body = f"""The Daily Duck — Gate A

今日の記事・コピーと、
使用する画像コンセプトを同時に承認してください。

==================================================
RECOMMENDED STORY
==================================================

{story.get("title", "")}

Why recommended:
{package.get("recommended_reason", "")}

Source:
{story.get("source", "")}
{story.get("url", "")}

==================================================
TOP 5 SHORTLIST / 候補ニュース5件
==================================================

{format_top_five(package["top_five"])}

==================================================
EDITORIAL COPY
==================================================

JP:
{package["jp_copy"]}

EN:
{package["en_copy"]}

Duck name:
{package["duck_name"]}

Duck JP:
{package["duck_jp"]}

Duck EN:
{package["duck_en"]}

X JP:
{package["x_jp"]}

X EN:
{package["x_en"]}

==================================================
IMAGE CONCEPTS — CHOOSE ONE / 画像コンセプト — 1案選択
==================================================

{format_image_concepts(package["image_concepts"])}

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
- この段階では実画像はまだ生成しません。
- Gate B完了前にWebサイト/Xへ公開しません。
"""

    return subject, body


def send_email(subject: str, body: str) -> int:
    gmail_address = required_env("GMAIL_ADDRESS")
    gmail_password = required_env("GMAIL_APP_PASSWORD")

    recipients = [
        value.strip()
        for value in required_env("EMAIL_TO").split(",")
        if value.strip()
    ]

    if not recipients:
        raise RuntimeError("EMAIL_TO contains no valid recipients.")

    msg = EmailMessage()
    msg["From"] = gmail_address
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        timeout=30,
    ) as smtp:
        smtp.login(gmail_address, gmail_password)
        smtp.send_message(msg)

    return len(recipients)


def main() -> int:
    ranked = load_json(RANKED_PATH)

    recommended, top_five = resolve_recommended_story(ranked)

    print(
        "Resolved recommended story:",
        recommended.get("id"),
        recommended.get("title"),
    )

    editorial = generate_editorial_package(recommended, top_five)

    package = build_package(
        ranked=ranked,
        recommended=recommended,
        top_five=top_five,
        editorial=editorial,
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

    subject, body = build_email(package)

    EMAIL_TEXT_PATH.write_text(
        body,
        encoding="utf-8",
    )

    recipient_count = send_email(
        subject,
        body,
    )

    print(f"Gate A package created: {PACKAGE_PATH}")
    print(f"Recommended story ID: {package['recommended_id']}")
    print(f"TOP 5 count: {len(package['top_five'])}")
    print(f"Image concept count: {len(package['image_concepts'])}")
    print(f"Gate A email sent to {recipient_count} recipient(s).")
    print(f"Subject: {subject}")
    print("Valid replies: 1 OK / 2 OK / 3 OK / 4 OK / 5 OK")
    print("STATE: WAITING_STORY_APPROVAL")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
