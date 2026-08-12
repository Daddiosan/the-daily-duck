#!/usr/bin/env python3
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


def resolve_recommended_story(ranked: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    top_five = ranked.get("top_five")
    if not isinstance(top_five, list) or len(top_five) < 5:
        raise ValueError("ai_ranked_news.json must contain at least five items in 'top_five'.")

    normalized = []
    for item in top_five[:5]:
        if not isinstance(item, dict):
            raise ValueError("Every item in 'top_five' must be a JSON object.")
        normalized.append(dict(item))

    recommended_id = ranked.get("recommended_id")
    recommended = next(
        (dict(x) for x in normalized if str(x.get("id")) == str(recommended_id)),
        None,
    )
    if recommended is None:
        raise ValueError(f"recommended_id={recommended_id!r} was not found in top_five.")

    recommended["recommended_reason"] = str(ranked.get("recommended_reason", "")).strip()
    return recommended, normalized


def clean_json_text(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def generate_editorial_package(recommended: dict[str, Any], top_five: list[dict[str, Any]]) -> dict[str, Any]:
    client = genai.Client(api_key=required_env("GEMINI_API_KEY"))

    source_story = {
        "id": recommended.get("id"),
        "title": recommended.get("title", ""),
        "source": recommended.get("source", ""),
        "url": recommended.get("url", ""),
        "reason": recommended.get("reason", ""),
        "recommended_reason": recommended.get("recommended_reason", ""),
    }

    # IMPORTANT: Build the JSON example separately. This avoids f-string brace parsing errors.
    output_example = {
        "jp_copy": "Japanese editorial copy",
        "en_copy": "English editorial copy",
        "duck_name": "short English mascot/story nickname",
        "duck_jp": "short playful Japanese duck line",
        "duck_en": "short playful English duck line",
        "x_jp": "Japanese X draft",
        "x_en": "English X draft",
        "top_five_ja": [
            {"id": "same story id", "title_ja": "natural Japanese title", "reason_ja": "natural Japanese summary/reason"}
            for _ in range(5)
        ],
        "image_concepts": [
            {
                "number": n,
                "title": "short English title",
                "title_ja": "natural Japanese title",
                "concept": "2-4 sentence English scene description",
                "concept_ja": "natural Japanese translation of the scene description",
                "visual_direction": "short English composition/style direction",
                "visual_direction_ja": "natural Japanese translation of the visual direction",
            }
            for n in range(1, 6)
        ],
    }

    prompt = """
You are the editorial assistant for The Daily Duck.

The Daily Duck publishes category-neutral uplifting news. The goal is to make
readers feel happier, hopeful, amused, inspired, warm, or positively curious.

Create the Gate A editorial package for the selected story below.

STRICT FACTUAL RULE:
Use ONLY facts present in SOURCE STORY and TOP FIVE STORIES. Do not invent
names, numbers, dates, places, quotations, study details, scientific findings,
or other factual claims. If source information is sparse, keep the copy general.

STYLE:
- Warm, simple, intelligent, concise; not clickbait.
- Japanese must be natural Japanese; English must be natural English.
- Duck lines may be playful but must not add unsupported facts.
- X drafts should be concise.
- Do not put URLs into generated copy fields.

TOP FIVE JAPANESE RULES:
- Return exactly five top_five_ja entries, one for each supplied story.
- Preserve each story id exactly.
- title_ja must naturally translate the English headline.
- reason_ja must naturally translate/summarize that story's supplied reason.

IMAGE CONCEPT RULES:
- Create EXACTLY five meaningfully different concepts for the RECOMMENDED story.
- Every concept must clearly relate to that story.
- Permanent mascot: recognizable yellow duck, orange beak, large dark glossy
  eyes, small feather tuft, friendly expression.
- Story-specific clothing/props are allowed.
- Simple, clean, modern editorial feeling; warm and charming; not overly vintage.
- Suitable for both website hero and X.
- No long article text embedded in the image.
- No unsupported factual details.
- Provide complete English and Japanese fields for every concept.

Return ONLY one valid JSON object matching this structure exactly:
""" + json.dumps(output_example, ensure_ascii=False, indent=2) + """

SOURCE STORY:
""" + json.dumps(source_story, ensure_ascii=False, indent=2) + """

TOP FIVE STORIES TO TRANSLATE:
""" + json.dumps(top_five, ensure_ascii=False, indent=2)

    response = client.models.generate_content(model=TEXT_MODEL, contents=prompt.strip())
    response_text = getattr(response, "text", None)
    if not response_text:
        raise RuntimeError("Gemini returned no editorial text.")

    try:
        generated = json.loads(clean_json_text(response_text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gemini did not return valid JSON: {exc}") from exc

    if not isinstance(generated, dict):
        raise ValueError("Gemini editorial response must be a JSON object.")

    for field in ["jp_copy", "en_copy", "duck_name", "duck_jp", "duck_en", "x_jp", "x_en"]:
        if not isinstance(generated.get(field), str) or not generated[field].strip():
            raise ValueError(f"Gemini response is missing non-empty '{field}'.")

    top_five_ja = generated.get("top_five_ja")
    if not isinstance(top_five_ja, list) or len(top_five_ja) != 5:
        raise ValueError("Gemini must return exactly five top_five_ja entries.")

    ja_by_id: dict[str, dict[str, str]] = {}
    for item in top_five_ja:
        if not isinstance(item, dict):
            raise ValueError("Every top_five_ja entry must be an object.")
        story_id = str(item.get("id", "")).strip()
        title_ja = str(item.get("title_ja", "")).strip()
        reason_ja = str(item.get("reason_ja", "")).strip()
        if not story_id or not title_ja or not reason_ja:
            raise ValueError("Each top_five_ja entry requires id, title_ja and reason_ja.")
        ja_by_id[story_id] = {"title_ja": title_ja, "reason_ja": reason_ja}

    for story in top_five:
        translation = ja_by_id.get(str(story.get("id")))
        if translation is None:
            raise ValueError(f"Japanese translation missing for story id={story.get('id')!r}.")
        story["title_ja"] = translation["title_ja"]
        story["reason_ja"] = translation["reason_ja"]

    concepts = generated.get("image_concepts")
    if not isinstance(concepts, list) or len(concepts) != 5:
        raise ValueError("Gemini must return exactly five image_concepts.")

    normalized_concepts = []
    for number, item in enumerate(concepts, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Image concept {number} must be an object.")
        fields = {
            "title": str(item.get("title", "")).strip(),
            "title_ja": str(item.get("title_ja", "")).strip(),
            "concept": str(item.get("concept", "")).strip(),
            "concept_ja": str(item.get("concept_ja", "")).strip(),
            "visual_direction": str(item.get("visual_direction", "")).strip(),
            "visual_direction_ja": str(item.get("visual_direction_ja", "")).strip(),
        }
        if not all(fields.values()):
            raise ValueError(f"Image concept {number} must contain complete English/Japanese fields.")
        normalized_concepts.append({"number": number, **fields})

    generated["image_concepts"] = normalized_concepts
    return generated


def build_package(ranked, recommended, top_five, editorial):
    issue_date = datetime.now().date().isoformat()
    return {
        "date": issue_date,
        "issue_date": issue_date,
        "phase": 2,
        "state": "WAITING_STORY_APPROVAL",
        "recommended_id": ranked.get("recommended_id"),
        "recommended_reason": ranked.get("recommended_reason", ""),
        "recommended_story": recommended,
        "top_five": top_five,
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
        "gate_a_package_created_at": datetime.now(timezone.utc).isoformat(),
    }


def format_top_five(top_five):
    lines = []
    for index, item in enumerate(top_five, start=1):
        lines += [f"{index}. {item.get('title_ja', '')}", f"   EN: {item.get('title', '')}"]
        meta = []
        if item.get("source"):
            meta.append(str(item["source"]))
        if item.get("total_score") is not None:
            meta.append(f"Score: {item['total_score']}")
        if meta:
            lines.append("   " + " | ".join(meta))
        if item.get("reason_ja"):
            lines.append(f"   日本語: {item['reason_ja']}")
        if item.get("reason"):
            lines.append(f"   English: {item['reason']}")
        if item.get("url"):
            lines.append(f"   {item['url']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_image_concepts(concepts):
    lines = []
    for item in concepts:
        lines += [
            f"[{item['number']}] {item['title_ja']} / {item['title']}", "",
            "日本語:", item["concept_ja"],
            f"構図・スタイル: {item['visual_direction_ja']}", "",
            "English:", item["concept"],
            f"Visual: {item['visual_direction']}", "",
        ]
    return "\n".join(lines).rstrip()


def build_email(package):
    story = package["recommended_story"]
    subject = f"The Daily Duck — Story Approval — {package['issue_date']}"
    body = f"""The Daily Duck — Gate A

今日の記事・コピーと、使用する画像コンセプトを同時に承認してください。

==================================================
RECOMMENDED STORY / 本日のおすすめ
==================================================

{story.get('title_ja', story.get('title', ''))}
EN: {story.get('title', '')}

おすすめ理由:
{story.get('reason_ja', '')}

Why recommended:
{package.get('recommended_reason', '')}

Source:
{story.get('source', '')}
{story.get('url', '')}

==================================================
TOP 5 SHORTLIST / 候補ニュース5件
==================================================

{format_top_five(package['top_five'])}

==================================================
EDITORIAL COPY / 記事案
==================================================

JP:
{package['jp_copy']}

EN:
{package['en_copy']}

Duck name:
{package['duck_name']}

Duck JP:
{package['duck_jp']}

Duck EN:
{package['duck_en']}

X JP:
{package['x_jp']}

X EN:
{package['x_en']}

==================================================
IMAGE CONCEPTS — CHOOSE ONE / 画像コンセプト — 1案選択
==================================================

{format_image_concepts(package['image_concepts'])}

==================================================
GATE A APPROVAL / 承認
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


def send_email(subject, body):
    gmail_address = required_env("GMAIL_ADDRESS")
    gmail_password = required_env("GMAIL_APP_PASSWORD")
    recipients = [x.strip() for x in required_env("EMAIL_TO").split(",") if x.strip()]
    if not recipients:
        raise RuntimeError("EMAIL_TO contains no valid recipients.")
    msg = EmailMessage()
    msg["From"] = gmail_address
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(gmail_address, gmail_password)
        smtp.send_message(msg)
    return len(recipients)


def main():
    ranked = load_json(RANKED_PATH)
    recommended, top_five = resolve_recommended_story(ranked)
    print("Resolved recommended story:", recommended.get("id"), recommended.get("title"))

    editorial = generate_editorial_package(recommended, top_five)
    package = build_package(ranked, recommended, top_five, editorial)

    PACKAGE_PATH.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    subject, body = build_email(package)
    EMAIL_TEXT_PATH.write_text(body, encoding="utf-8")
    recipient_count = send_email(subject, body)

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
