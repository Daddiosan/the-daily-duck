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


APPROVED_STORY_PATH = Path(
    "automation_state/approved_story.json"
)

OUTPUT_PATH = Path(
    "automation_state/image_concepts.json"
)

TEXT_MODEL = (
    os.getenv("GEMINI_TEXT_MODEL") or ""
).strip() or "gemini-3.6-flash"


# ============================================================
# Environment
# ============================================================

def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


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

    if not isinstance(data, dict):
        raise ValueError(
            f"{path} must contain a JSON object."
        )

    return data


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


# ============================================================
# Find selected / approved story
# ============================================================

def find_story_container(
    data: dict[str, Any],
) -> dict[str, Any]:
    """
    Locate the one story already selected at Gate A.
    """

    candidate_keys = (
        "selected_story",
        "approved_story",
        "story",
        "recommended_story",
        "recommended",
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

    gate_a_package = data.get(
        "gate_a_package"
    )

    if isinstance(
        gate_a_package,
        dict,
    ):
        for key in candidate_keys:
            value = gate_a_package.get(key)

            if isinstance(value, dict):
                return value

    # Compatibility with older state shapes
    if any(
        key in data
        for key in (
            "title",
            "source",
            "url",
            "jp_copy",
            "en_copy",
        )
    ):
        return data

    raise ValueError(
        "Could not locate the approved story inside "
        "automation_state/approved_story.json."
    )


# ============================================================
# State validation
# ============================================================

def validate_approved_state(
    data: dict[str, Any],
) -> None:
    state = str(
        data.get("state", "")
    ).strip().upper()

    if state != "APPROVED_STORY":
        raise ValueError(
            "Image concepts may only be generated "
            "from APPROVED_STORY; "
            f"got {state!r}."
        )


# ============================================================
# Gemini retry logic
# ============================================================

def is_retryable_gemini_error(
    exc: Exception,
) -> bool:
    """
    Retry temporary API/server/load failures only.

    Examples:
    - 429 RESOURCE_EXHAUSTED
    - 500 INTERNAL
    - 502
    - 503 UNAVAILABLE / high demand
    - 504 timeout/gateway issues
    """

    message = str(exc).upper()

    retry_tokens = (
        "429",
        "RESOURCE_EXHAUSTED",
        "500",
        "INTERNAL",
        "502",
        "503",
        "UNAVAILABLE",
        "HIGH DEMAND",
        "504",
        "DEADLINE_EXCEEDED",
        "TIMEOUT",
        "TIMED OUT",
        "TEMPORAR",
    )

    return any(
        token in message
        for token in retry_tokens
    )


def generate_content_with_retry(
    client: genai.Client,
    prompt: str,
    max_attempts: int = 5,
) -> Any:
    """
    Call Gemini with exponential backoff.

    Approximate waits:
    attempt 1 failure -> ~10 sec
    attempt 2 failure -> ~20 sec
    attempt 3 failure -> ~40 sec
    attempt 4 failure -> ~60 sec

    Non-temporary errors fail immediately.
    """

    last_exception: Exception | None = None

    for attempt in range(
        1,
        max_attempts + 1,
    ):
        try:
            print(
                f"Gemini request attempt "
                f"{attempt}/{max_attempts}..."
            )

            response = (
                client.models.generate_content(
                    model=TEXT_MODEL,
                    contents=prompt,
                )
            )

            print(
                "Gemini request succeeded."
            )

            return response

        except Exception as exc:
            last_exception = exc

            if not is_retryable_gemini_error(
                exc
            ):
                print(
                    "Gemini error is not considered temporary."
                )
                raise

            print(
                "Temporary Gemini API error detected."
            )

            print(
                f"Error: {str(exc)[:800]}"
            )

            if attempt >= max_attempts:
                print(
                    "Gemini retry limit reached."
                )
                raise

            # Exponential backoff:
            # 10, 20, 40, 60 sec max
            base_wait = min(
                60,
                (2 ** attempt) * 5,
            )

            # Add small jitter so simultaneous jobs
            # do not all retry at exactly the same time.
            jitter = random.uniform(
                0,
                3,
            )

            wait_seconds = (
                base_wait + jitter
            )

            print(
                f"Waiting {wait_seconds:.1f} seconds "
                "before retry..."
            )

            time.sleep(
                wait_seconds
            )

    if last_exception:
        raise last_exception

    raise RuntimeError(
        "Gemini request failed unexpectedly."
    )


# ============================================================
# Generate five image concepts
# ============================================================

def generate_concepts(
    approved_state: dict[str, Any],
    story: dict[str, Any],
) -> list[dict[str, Any]]:

    client = genai.Client(
        api_key=required_env(
            "GEMINI_API_KEY"
        )
    )

    output_example = {
        "concepts": [
            {
                "number": 1,

                "title_ja":
                    "短い日本語タイトル",

                "title_en":
                    "Short English title",

                "concept_ja":
                    "日本語の画像コンセプト説明",

                "concept_en":
                    "English image concept description",

                "composition_ja":
                    "日本語の構図・被写体・背景・小道具の説明",

                "composition_en":
                    "English composition, subject, background and props",

                "generation_prompt_en":
                    "Detailed English production prompt",

                "alt_ja":
                    "日本語alt案",

                "alt_en":
                    "English alt text draft",
            }
            for _ in range(5)
        ]
    }

    prompt = f"""
You are the visual editorial director for The Daily Duck.

Create EXACTLY FIVE distinct visual concepts for ONE already-approved
Daily Duck story.

IMPORTANT WORKFLOW:

This step defines five different visual concepts.

Immediately after this step:

1. ONE preview image will be generated for EACH concept.
2. The human editor will receive all five preview images by email.
3. The human editor will reply with exactly 1, 2, 3, 4, or 5.
4. That selected concept becomes the locked visual direction.
5. THEN exactly FIVE final image variations will be generated from
   ONLY that selected concept.
6. The human editor will choose one final image from 1-5,
   or reply NEXT 5 to generate another five variations while keeping
   the SAME selected concept.
7. Only the finally selected image becomes the canonical Daily Duck image.

Therefore:

- These five concepts must be meaningfully different from one another.
- generation_prompt_en must be strong enough to generate both:
  a) the concept preview image
  b) later final variations after that concept is selected.

ARTICLE APPROVAL RULE:

The story has already been selected and approved by the human editor.

Do NOT:
- replace the story
- re-rank the story
- change its meaning
- choose another TOP 5 story

FACTUAL RULE:

Use ONLY facts supported by the approved story and approved editorial package.

Do not invent:
- people
- places
- dates
- numbers
- quotations
- organizations
- scientific findings
- background events
- unsupported physical objects

If specific factual information is unavailable,
use a symbolic or editorial visual treatment rather than inventing facts.

THE DAILY DUCK MASCOT:

Every concept must preserve the established Daily Duck identity:

- recognizable friendly yellow duck
- orange beak
- large dark glossy eyes
- small feather tuft
- warm approachable expression

Story-specific:
- clothing
- props
- environments
- actions

are allowed when appropriate.

VISUAL STYLE:

- simple
- clean
- modern
- charming
- editorial
- visually strong
- not overly vintage
- suitable for both website hero image and X post

Avoid:
- logos
- watermarks
- UI screenshots
- long embedded text
- dense typography
- photorealistic humans as the main subject
- five near-identical concepts

CONCEPT DIVERSITY:

The five concepts must differ meaningfully in areas such as:

- scene or setting
- camera angle
- framing
- duck pose/action
- storytelling metaphor
- background
- props
- mood
- visual hierarchy

All five must still communicate the SAME approved story.

IMAGE GENERATION PROMPT:

generation_prompt_en must:

- be written in English
- be detailed and production-ready
- clearly describe the Daily Duck mascot
- clearly describe composition
- clearly describe environment
- clearly describe lighting/mood
- avoid text/logos/watermarks
- remain faithful to the approved article
- support later visual variation without changing the core concept

Return ONLY valid JSON matching this structure:

{json.dumps(
    output_example,
    ensure_ascii=False,
    indent=2,
)}

APPROVED STORY:

{json.dumps(
    story,
    ensure_ascii=False,
    indent=2,
)}

FULL APPROVED STATE / EDITORIAL PACKAGE:

{json.dumps(
    approved_state,
    ensure_ascii=False,
    indent=2,
)}
""".strip()

    # --------------------------------------------------------
    # Gemini request with automatic retry
    # --------------------------------------------------------

    response = generate_content_with_retry(
        client=client,
        prompt=prompt,
        max_attempts=5,
    )

    raw = getattr(
        response,
        "text",
        None,
    )

    if not raw:
        raise RuntimeError(
            "Gemini returned no text."
        )

    # --------------------------------------------------------
    # Parse returned JSON
    # --------------------------------------------------------

    try:
        parsed = json.loads(
            clean_json_text(
                raw
            )
        )

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Gemini returned invalid JSON: "
            f"{exc}"
        ) from exc

    if not isinstance(
        parsed,
        dict,
    ):
        raise ValueError(
            "Gemini response must be a JSON object."
        )

    concepts = parsed.get(
        "concepts"
    )

    if (
        not isinstance(
            concepts,
            list,
        )
        or len(concepts) != 5
    ):
        raise ValueError(
            "Gemini must return exactly five image concepts."
        )

    normalized: list[
        dict[str, Any]
    ] = []

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

    # --------------------------------------------------------
    # Normalize and validate all five concepts
    # --------------------------------------------------------

    for index, item in enumerate(
        concepts,
        start=1,
    ):
        if not isinstance(
            item,
            dict,
        ):
            raise ValueError(
                f"Concept {index} must be a JSON object."
            )

        normalized_item = dict(
            item
        )

        # Never trust model numbering.
        # Number locally and deterministically.
        normalized_item[
            "number"
        ] = index

        for field in required_fields:
            value = str(
                normalized_item.get(
                    field,
                    "",
                )
            ).strip()

            if not value:
                raise ValueError(
                    f"Concept {index} is missing "
                    f"required field: {field}"
                )

            normalized_item[
                field
            ] = value

        # Preview generation happens in the next script.
        normalized_item[
            "preview_status"
        ] = "NOT_GENERATED"

        normalized_item[
            "preview_image_path"
        ] = ""

        normalized.append(
            normalized_item
        )

    return normalized


# ============================================================
# Main
# ============================================================

def main() -> None:

    approved_state = load_json(
        APPROVED_STORY_PATH
    )

    validate_approved_state(
        approved_state
    )

    story = find_story_container(
        approved_state
    )

    print(
        "APPROVED_STORY confirmed."
    )

    print(
        "Generating exactly five image concepts..."
    )

    concepts = generate_concepts(
        approved_state,
        story,
    )

    generated_at = datetime.now(
        timezone.utc
    ).isoformat()

    issue_date = str(
        approved_state.get(
            "issue_date"
        )
        or approved_state.get(
            "date"
        )
        or ""
    ).strip()

    result = {
        "state":
            "IMAGE_CONCEPT_PREVIEW_GENERATION",

        "issue_date":
            issue_date,

        "generated_at":
            generated_at,

        "source_state":
            "APPROVED_STORY",

        "approved_story_path":
            str(
                APPROVED_STORY_PATH
            ),

        "story":
            story,

        "concepts":
            concepts,

        "concept_count":
            5,

        "preview_rule":
            (
                "Generate exactly one preview "
                "image for each concept."
            ),

        "selection_rule":
            (
                "Reply with exactly one digit: "
                "1, 2, 3, 4, or 5."
            ),

        "after_concept_selection":
            (
                "Generate exactly five final "
                "image variations from ONLY "
                "the selected concept."
            ),

        "final_selection_rule":
            (
                "Reply 1-5 to select the final "
                "image, or NEXT 5 to regenerate "
                "five images from the same "
                "selected concept."
            ),

        "next_state_after_previews":
            "IMAGE_CONCEPT_REVIEW",

        "next_state_after_valid_selection":
            "APPROVED_IMAGE_CONCEPT",
    }

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with OUTPUT_PATH.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            result,
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(
        f"STATE: {result['state']}"
    )

    print(
        f"Generated exactly "
        f"{len(concepts)} image concepts."
    )

    print(
        f"Saved: {OUTPUT_PATH}"
    )

    for concept in concepts:
        print(
            f"{concept['number']}: "
            f"{concept['title_ja']}"
        )


if __name__ == "__main__":
    main()
