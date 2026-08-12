#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google import genai

APPROVED_STORY_PATH = Path("automation_state/approved_story.json")
OUTPUT_PATH = Path("automation_state/image_concepts.json")
TEXT_MODEL = (os.getenv("GEMINI_TEXT_MODEL") or "").strip() or "gemini-3.6-flash"


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


def clean_json_text(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def find_story_container(data: dict[str, Any]) -> dict[str, Any]:
    """
    Tolerates the Phase 1 state/package shapes used so far.
    We intentionally keep the full approved state available to Gemini,
    but extract a compact story object for validation and metadata.
    """
    candidate_keys = (
        "approved_story",
        "story",
        "recommended_story",
        "recommended",
        "selected_story",
    )

    for key in candidate_keys:
        value = data.get(key)
        if isinstance(value, dict):
            return value

    package = data.get("package")
    if isinstance(package, dict):
        for key in candidate_keys:
            value = package.get(key)
            if isinstance(value, dict):
                return value

    # Some Phase 1 states may store the approved editorial package at root.
    if any(k in data for k in ("title", "source", "url", "jp_copy", "en_copy")):
        return data

    raise ValueError(
        "Could not locate the approved story inside automation_state/approved_story.json."
    )


def validate_approved_state(data: dict[str, Any]) -> None:
    state = str(data.get("state", "")).strip().upper()
    if state != "APPROVED_STORY":
        raise ValueError(
            f"Image concepts may only be generated from APPROVED_STORY; got {state!r}."
        )


def generate_concepts(approved_state: dict[str, Any], story: dict[str, Any]) -> list[dict[str, Any]]:
    client = genai.Client(api_key=required_env("GEMINI_API_KEY"))

    output_example = {
        "concepts": [
            {
                "number": 1,
                "title_ja": "短い日本語タイトル",
                "title_en": "Short English title",
                "concept_ja": "日本語の画像コンセプト説明",
                "concept_en": "English image concept description",
                "composition_ja": "日本語の構図・被写体・背景・小道具の説明",
                "composition_en": "English composition, subject, background and props",
                "generation_prompt_en": "Detailed English prompt for final image generation",
                "alt_ja": "日本語alt案",
                "alt_en": "English alt text draft"
            }
            for _ in range(5)
        ]
    }

    prompt = f"""
You are the visual editorial director for The Daily Duck.

Create EXACTLY FIVE distinct image concepts for ONE already-approved Daily Duck story.
These are concepts only. Do NOT generate images in this step.

APPROVAL RULE:
The article has already been approved by the human editor.
Do not change, replace, reinterpret, or re-rank the article.

FACTUAL RULE:
Use only facts supported by the approved story/editorial package below.
Do not invent people, locations, numbers, objects, quotations, scientific details,
or events that are not supported by the approved material.

BRAND / MASCOT RULES:
- The Daily Duck mascot must remain a recognizable friendly yellow duck.
- Orange beak.
- Large dark glossy eyes.
- Small feather tuft.
- Warm, approachable expression.
- Story-specific clothing and props are allowed when appropriate.
- Keep the visual style simple, clean, modern, charming, editorial, and not overly vintage.
- The final image must work both as the website hero image and the X post image.
- Avoid long embedded text, logos, watermarks, UI screenshots, and dense typography.

CONCEPT DIVERSITY:
- Return exactly 5 meaningfully different visual approaches.
- They may differ in setting, camera angle, metaphor, action, props, framing, or mood.
- Do not make five near-duplicates.
- Every concept must still clearly communicate the SAME approved story.

FINAL-IMAGE INTENT:
After the human selects one concept number, that selected concept will be used
directly to generate ONE final canonical image. There is no later five-image
variation round. Therefore each generation_prompt_en must be specific and
production-ready.

Return ONLY valid JSON matching this structure:
{json.dumps(output_example, ensure_ascii=False, indent=2)}

APPROVED STORY (compact):
{json.dumps(story, ensure_ascii=False, indent=2)}

FULL APPROVED STATE / EDITORIAL PACKAGE:
{json.dumps(approved_state, ensure_ascii=False, indent=2)}
""".strip()

    response = client.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
    )

    raw = getattr(response, "text", None)
    if not raw:
        raise RuntimeError("Gemini returned no text.")

    try:
        parsed = json.loads(clean_json_text(raw))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini returned invalid JSON: {exc}") from exc

    concepts = parsed.get("concepts")
    if not isinstance(concepts, list) or len(concepts) != 5:
        raise ValueError("Gemini must return exactly five image concepts.")

    normalized: list[dict[str, Any]] = []
    required_fields = (
        "title_ja",
        "title_en",
        "concept_ja",
        "concept_en",
        "composition_ja",
        "composition_en",
        "generation_prompt_en",
        "alt_ja",
        "alt_en",
    )

    for index, item in enumerate(concepts, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Concept {index} must be a JSON object.")

        normalized_item = dict(item)
        normalized_item["number"] = index

        for field in required_fields:
            value = str(normalized_item.get(field, "")).strip()
            if not value:
                raise ValueError(f"Concept {index} is missing required field: {field}")
            normalized_item[field] = value

        normalized.append(normalized_item)

    return normalized


def main() -> None:
    approved_state = load_json(APPROVED_STORY_PATH)
    validate_approved_state(approved_state)

    story = find_story_container(approved_state)
    concepts = generate_concepts(approved_state, story)

    generated_at = datetime.now(timezone.utc).isoformat()

    result = {
        "state": "IMAGE_CONCEPT_REVIEW",
        "generated_at": generated_at,
        "source_state": "APPROVED_STORY",
        "approved_story_path": str(APPROVED_STORY_PATH),
        "story": story,
        "concepts": concepts,
        "selection_rule": "Reply with exactly one digit: 1, 2, 3, 4, or 5.",
        "next_state_after_valid_selection": "APPROVED_IMAGE_CONCEPT",
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"STATE: {result['state']}")
    print(f"Generated exactly {len(concepts)} image concepts.")
    print(f"Saved: {OUTPUT_PATH}")
    for concept in concepts:
        print(f"{concept['number']}: {concept['title_ja']}")


if __name__ == "__main__":
    main()
