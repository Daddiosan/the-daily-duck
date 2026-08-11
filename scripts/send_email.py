#!/usr/bin/env python3
"""
The Daily Duck - Gate A editorial approval email

Phase 2 revision:
- Presents exactly five IMAGE CONCEPT choices in the Gate A email.
- User approves story/copy + image concept with exact reply:
    1 OK
    2 OK
    3 OK
    4 OK
    5 OK
- Plain "OK" no longer approves.

Inputs:
- gate_a_package.json

Environment:
- GEMINI_API_KEY
- GMAIL_ADDRESS
- GMAIL_APP_PASSWORD
- EMAIL_TO
Optional:
- GEMINI_TEXT_MODEL (default: gemini-2.5-flash)
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

PACKAGE_PATH = Path("gate_a_package.json")
EMAIL_TEXT_PATH = Path("daily_duck_email.txt")
TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash")


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


def first_value(data: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def story_context(pkg: dict[str, Any]) -> str:
    recommended = pkg.get("recommended_story")
    if isinstance(recommended, dict):
        title = first_value(recommended, "title", "headline", "name")
        summary = first_value(recommended, "summary", "description", "reason", "jp", "en")
        source = first_value(recommended, "source", "source_name")
        url = first_value(recommended, "url", "source_url", "link")
    else:
        title = first_value(pkg, "title", "headline", "recommended_title")
        summary = first_value(pkg, "summary", "description", "jp_copy", "story_jp")
        source = first_value(pkg, "source", "source_name")
        url = first_value(pkg, "source_url", "url", "link")

    duck_name = first_value(pkg, "duck_name", "name")
    existing_concept = first_value(pkg, "image_concept", "visual_concept")

    return "\n".join(
        [
            f"TITLE: {title}",
            f"SUMMARY: {summary}",
            f"SOURCE: {source}",
            f"URL: {url}",
            f"DUCK NAME: {duck_name}",
            f"EXISTING IMAGE CONCEPT: {existing_concept}",
        ]
    )


def parse_json_array(text: str) -> list[dict[str, Any]]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Gemini did not return a JSON array.")
    data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, list) or len(data) != 5:
        raise ValueError("Expected exactly five image concepts.")
    return data


def normalize_concepts(raw: list[dict[str, Any]]) -> list[dict[str, str | int]]:
    concepts: list[dict[str, str | int]] = []
    for idx, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError("Each concept must be an object.")
        title = str(item.get("title", "")).strip()
        concept = str(item.get("concept", item.get("description", ""))).strip()
        visual = str(item.get("visual_direction", item.get("visual", ""))).strip()
        if not title or not concept:
            raise ValueError(f"Concept {idx} is incomplete.")
        concepts.append(
            {
                "number": idx,
                "title": title,
                "concept": concept,
                "visual_direction": visual,
            }
        )
    return concepts


def generate_image_concepts(pkg: dict[str, Any]) -> list[dict[str, str | int]]:
    existing = pkg.get("image_concepts")
    if isinstance(existing, list) and len(existing) == 5:
        return normalize_concepts(existing)

    api_key = required_env("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    prompt = f"""
You are the visual editor for The Daily Duck.

Create EXACTLY five clearly different image CONCEPTS for the approved-candidate story below.
These are not five finished images. The human editor will choose one concept at Gate A,
and later the system will generate five visual executions of that selected concept.

Brand / mascot rules:
- recognizable same yellow duck character
- orange beak
- large dark glossy eyes
- small feather tuft
- friendly expression
- story-specific clothing and props are allowed
- clean modern editorial feeling; avoid heavy vintage treatment
- image must work as the shared canonical hero image for website and X
- do not put long article text into the artwork
- be faithful to the story; do not invent factual claims

Make the five concepts meaningfully different in composition, camera/viewpoint,
setting, action, props, and storytelling approach while keeping the same mascot identity.

Return ONLY valid JSON as an array of exactly five objects:
[
  {{
    "title": "short concept title",
    "concept": "2-4 sentence description of the scene",
    "visual_direction": "short composition/style direction"
  }}
]

STORY:
{story_context(pkg)}
""".strip()

    response = client.models.generate_content(model=TEXT_MODEL, contents=prompt)
    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("Gemini returned no text while generating image concepts.")
    return normalize_concepts(parse_json_array(text))


def shortlist_text(pkg: dict[str, Any]) -> str:
    shortlist = (
        pkg.get("top5")
        or pkg.get("top_5")
        or pkg.get("shortlist")
        or pkg.get("candidates")
        or []
    )
    if not isinstance(shortlist, list):
        return str(shortlist)

    lines = []
    for idx, item in enumerate(shortlist[:5], start=1):
        if isinstance(item, dict):
            title = first_value(item, "title", "headline", "name", default=f"Candidate {idx}")
            reason = first_value(item, "reason", "summary", "description")
            source = first_value(item, "source", "source_name")
            url = first_value(item, "url", "source_url", "link")
            lines.append(f"{idx}. {title}")
            if reason:
                lines.append(f"   {reason}")
            if source or url:
                lines.append(f"   Source: {source} {url}".rstrip())
        else:
            lines.append(f"{idx}. {item}")
    return "\n".join(lines)


def build_email(pkg: dict[str, Any], concepts: list[dict[str, str | int]]) -> tuple[str, str]:
    issue_date = first_value(pkg, "date", "issue_date", default=datetime.now().date().isoformat())
    subject = f"The Daily Duck — Story Approval — {issue_date}"

    recommended = pkg.get("recommended_story")
    if isinstance(recommended, dict):
        rec_title = first_value(recommended, "title", "headline", "name")
        rec_summary = first_value(recommended, "summary", "description", "reason")
        rec_source = first_value(recommended, "source", "source_name")
        rec_url = first_value(recommended, "url", "source_url", "link")
    else:
        rec_title = first_value(pkg, "title", "headline", "recommended_title")
        rec_summary = first_value(pkg, "summary", "description", "recommendation_reason")
        rec_source = first_value(pkg, "source", "source_name")
        rec_url = first_value(pkg, "source_url", "url", "link")

    concept_lines = []
    for c in concepts:
        concept_lines.extend(
            [
                f"[{c['number']}] {c['title']}",
                f"{c['concept']}",
                f"Visual: {c['visual_direction']}",
                "",
            ]
        )

    body = f"""The Daily Duck — Gate A

今日の記事・コピーと、画像コンセプトを同時に承認してください。

==================================================
RECOMMENDED STORY
==================================================
{rec_title}

{rec_summary}

Source: {rec_source}
{rec_url}

==================================================
TOP 5 SHORTLIST
==================================================
{shortlist_text(pkg)}

==================================================
DUCK / COPY
==================================================
Duck name:
{first_value(pkg, "duck_name", "name")}

JP:
{first_value(pkg, "jp_copy", "story_jp", "jp")}

EN:
{first_value(pkg, "en_copy", "story_en", "en")}

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
APPROVAL COMMAND
==================================================
このメールに、使用したい画像コンセプト番号 + OK だけを返信してください。

1 OK
2 OK
3 OK
4 OK
5 OK

例:
3 OK

重要:
- 「OK」だけでは承認されません。
- 1〜5の案番号が必要です。
- 上記5コマンド以外では APPROVED_STORY に進みません。
- この時点では画像生成・Web公開・X公開は行いません。
"""
    return subject, body


def main() -> int:
    pkg = load_json(PACKAGE_PATH)
    concepts = generate_image_concepts(pkg)

    pkg["image_concepts"] = concepts
    pkg["gate_a_approval_format"] = "<1-5> OK"
    pkg["gate_a_updated_at"] = datetime.now(timezone.utc).isoformat()
    PACKAGE_PATH.write_text(
        json.dumps(pkg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    subject, body = build_email(pkg, concepts)
    EMAIL_TEXT_PATH.write_text(body, encoding="utf-8")

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

    print(f"Gate A email sent to {len(recipients)} recipient(s).")
    print(f"Subject: {subject}")
    print("Valid approvals: 1 OK / 2 OK / 3 OK / 4 OK / 5 OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
