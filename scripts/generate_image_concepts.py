#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google import genai
from model_config import TEXT_MODEL

STATE_DIR = Path("automation_state")
APPROVED_PATH = STATE_DIR / "approved_story.json"
OPTIONS_PATH = STATE_DIR / "design_options.json"


def first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def clean_json_text(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def main() -> int:
    approved = json.loads(APPROVED_PATH.read_text(encoding="utf-8"))
    if approved.get("state") != "APPROVED_STORY":
        raise RuntimeError(f"Expected APPROVED_STORY, got {approved.get('state')!r}")

    issue_date = first_text(approved.get("issue_date"), approved.get("date"))
    story = approved.get("recommended_story") if isinstance(approved.get("recommended_story"), dict) else {}

    story_title = first_text(story.get("title"), story.get("title_ja"), approved.get("en_copy"))
    story_summary = first_text(approved.get("jp_copy"), approved.get("en_copy"), story.get("reason"))
    source = first_text(story.get("source"), approved.get("source"))

    shape = {
        "image_concepts": [
            {"number": 1, "title_ja": "...", "title_en": "...", "concept_ja": "...", "concept_en": "...", "visual_direction": "..."},
            {"number": 2, "title_ja": "...", "title_en": "...", "concept_ja": "...", "concept_en": "...", "visual_direction": "..."},
            {"number": 3, "title_ja": "...", "title_en": "...", "concept_ja": "...", "concept_en": "...", "visual_direction": "..."}
        ],
        "title_ideas": [
            {"number": 1, "title": "...", "meaning_ja": "..."},
            {"number": 2, "title": "...", "meaning_ja": "..."},
            {"number": 3, "title": "...", "meaning_ja": "..."}
        ]
    }

    prompt = f"""
You are the art director and headline writer for The Daily Duck.

Create EXACTLY 3 distinct image concepts and EXACTLY 3 catchy English title ideas.

TITLE RULES
- Make the title unmistakably The Daily Duck.
- Prefer clever duck wordplay such as QUACK, DUCK, WADDLE, BILL, FEATHER, or POND when natural.
- Short, punchy, memorable, suitable for a large X-card headline.
- Do not merely repeat the news headline.
- Quality examples: QUACKSTRONAUT; DODO DNA? QUACKING AMAZING!
- Do not make misleading factual claims.

IMAGE RULES
- Exactly 3 meaningfully different visual directions.
- Cheerful, premium, modern editorial feel.
- Simple rather than overly vintage.
- No text, logos, watermarks or labels inside the hero image.
- The X card is composed separately at 1500x1200 (5:4).

Return ONLY valid JSON matching:
{json.dumps(shape, ensure_ascii=False, indent=2)}

APPROVED STORY
Date: {issue_date}
Headline: {story_title}
Summary: {story_summary}
Source: {source}
""".strip()

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    interaction = client.interactions.create(model=TEXT_MODEL, input=prompt)
    raw = interaction.output_text
    if not raw:
        raise RuntimeError("Gemini returned no design text.")

    generated = json.loads(clean_json_text(raw))
    concepts = generated.get("image_concepts")
    titles = generated.get("title_ideas")
    if not isinstance(concepts, list) or len(concepts) != 3:
        raise ValueError("Exactly 3 image concepts are required.")
    if not isinstance(titles, list) or len(titles) != 3:
        raise ValueError("Exactly 3 title ideas are required.")

    for i, item in enumerate(concepts, 1):
        item["number"] = i
    for i, item in enumerate(titles, 1):
        item["number"] = i

    payload = {
        "state": "WAITING_DESIGN_SELECTION",
        "issue_date": issue_date,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "text_model": TEXT_MODEL,
        "approved_story": approved,
        "image_concepts": concepts,
        "title_ideas": titles,
        "approval_format": "<image 1-3> <title 1-3>",
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OPTIONS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"TEXT MODEL: {TEXT_MODEL}")
    print("STATE: WAITING_DESIGN_SELECTION")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
