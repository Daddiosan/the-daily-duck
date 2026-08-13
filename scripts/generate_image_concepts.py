#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google import genai


APPROVED_STORY_PATH = Path("automation_state/approved_story.json")
OUTPUT_PATH = Path("automation_state/image_concepts.json")

TEXT_MODEL = (
    os.getenv("GEMINI_TEXT_MODEL") or ""
).strip() or "gemini-3.6-flash"

CONCEPT_COUNT = 3


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Required file not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")

    return data


def clean_json_text(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def find_story_container(data: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "selected_story",
        "approved_story",
        "story",
        "recommended_story",
        "recommended",
    ):
        value = data.get(key)
        if isinstance(value, dict):
            return value

    package = data.get("gate_a_package")

    if isinstance(package, dict):
        for key in (
            "selected_story",
            "recommended_story",
            "story",
        ):
            value = package.get(key)
            if isinstance(value, dict):
                return value

    raise ValueError(
        "Could not locate selected story in approved_story.json."
    )


def validate_approved_state(data: dict[str, Any]) -> None:
    state = str(data.get("state", "")).strip().upper()

    if state != "APPROVED_STORY":
        raise ValueError(
            f"Expected APPROVED_STORY, got {state!r}."
        )


def retryable(exc: Exception) -> bool:
    message = str(exc).upper()

    return any(
        token in message
        for token in (
            "429",
            "RESOURCE_EXHAUSTED",
            "500",
            "502",
            "503",
            "504",
            "UNAVAILABLE",
            "HIGH DEMAND",
            "TIMEOUT",
            "TEMPORAR",
            "INTERNAL",
        )
    )


def generate_with_retry(
    client: genai.Client,
    prompt: str,
    attempts: int = 5,
):
    for attempt in range(1, attempts + 1):
        try:
            print(f"Gemini attempt {attempt}/{attempts}")

            return client.models.generate_content(
                model=TEXT_MODEL,
                contents=prompt,
            )

        except Exception as exc:
            if not retryable(exc) or attempt >= attempts:
                raise

            wait = min(60, (2 ** attempt) * 5) + random.uniform(0, 3)

            print(f"Temporary Gemini error: {str(exc)[:500]}")
            print(f"Retrying in {wait:.1f}s")

            time.sleep(wait)

    raise RuntimeError("Gemini request failed.")


def generate_concepts(
    approved_state: dict[str, Any],
    story: dict[str, Any],
) -> list[dict[str, Any]]:

    client = genai.Client(
        api_key=required_env("GEMINI_API_KEY")
    )

    example = {
        "concepts": [
            {
                "number": 1,
                "title_ja": "短い日本語タイトル",
                "title_en": "Short English title",
                "concept_ja": "日本語のコンセプト説明",
                "concept_en": "English concept",
                "composition_ja": "日本語の構図",
                "composition_en": "English composition",
                "generation_prompt_en": "Detailed production prompt",
                "alt_ja": "日本語alt",
                "alt_en": "English alt",
            }
            for _ in range(CONCEPT_COUNT)
        ]
    }

    prompt = f"""
You are the visual editorial director for The Daily Duck.

Create EXACTLY THREE different visual concepts for the ONE approved story.

WORKFLOW:
- One WEB hero image will be generated from each concept.
- An X branded editorial card will later be created from each WEB image.
- The editor receives THREE sets:
  1 WEB + X
  2 WEB + X
  3 WEB + X
- The editor replies with exactly 1, 2, or 3.
- The WEB image and X card with the same number are both approved.
- There is NO additional final-image generation round.

MASCOT:
- recognizable friendly yellow duck
- orange beak
- large glossy dark eyes
- small feather tuft
- warm expression
- consistent Daily Duck identity

STYLE:
- clean
- modern
- charming
- premium editorial
- visually clear
- not overly vintage
- usable as a website hero
- no embedded text
- no logo
- no watermark
- no UI

DIVERSITY:
The three concepts must be meaningfully different in framing,
setting, action, mood, props, or visual metaphor.

FACTUAL SAFETY:
Do not invent unsupported factual claims, people, locations,
quotations, statistics, organizations or events.

generation_prompt_en must describe ONLY the visual hero artwork.
Do NOT ask the image model to render typography.

Return ONLY JSON:

{json.dumps(example, ensure_ascii=False, indent=2)}

APPROVED STORY:

{json.dumps(story, ensure_ascii=False, indent=2)}

FULL APPROVED STATE:

{json.dumps(approved_state, ensure_ascii=False, indent=2)}
""".strip()

    response = generate_with_retry(
        client,
        prompt,
    )

    raw = getattr(response, "text", None)

    if not raw:
        raise RuntimeError("Gemini returned no text.")

    parsed = json.loads(
        clean_json_text(raw)
    )

    concepts = parsed.get("concepts")

    if (
        not isinstance(concepts, list)
        or len(concepts) != CONCEPT_COUNT
    ):
        raise ValueError(
            "Gemini must return exactly three concepts."
        )

    required = (
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

    result: list[dict[str, Any]] = []

    for index, raw_concept in enumerate(concepts, start=1):
        if not isinstance(raw_concept, dict):
            raise ValueError(f"Concept {index} is invalid.")

        concept = dict(raw_concept)
        concept["number"] = index

        for field in required:
            value = str(concept.get(field, "")).strip()

            if not value:
                raise ValueError(
                    f"Concept {index} missing {field}."
                )

            concept[field] = value

        concept["web_image_status"] = "NOT_GENERATED"
        concept["web_image_path"] = ""

        concept["x_image_status"] = "NOT_GENERATED"
        concept["x_image_path"] = ""

        result.append(concept)

    return result


def main() -> None:
    approved = load_json(APPROVED_STORY_PATH)

    validate_approved_state(approved)

    story = find_story_container(approved)

    concepts = generate_concepts(
        approved,
        story,
    )

    issue_date = str(
        approved.get("issue_date")
        or approved.get("date")
        or ""
    ).strip()

    output = {
        "state": "IMAGE_CONCEPT_ASSET_GENERATION",
        "issue_date": issue_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_state": "APPROVED_STORY",
        "story": story,
        "concept_count": CONCEPT_COUNT,
        "concepts": concepts,
        "selection_rule": "Reply exactly 1, 2, or 3.",
        "selection_meaning":
            "The WEB image and X card with the same number are approved together.",
        "next_state_after_assets":
            "IMAGE_CONCEPT_REVIEW",
    }

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUTPUT_PATH.write_text(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print("Generated exactly 3 concepts.")
    print("STATE: IMAGE_CONCEPT_ASSET_GENERATION")


if __name__ == "__main__":
    main()
