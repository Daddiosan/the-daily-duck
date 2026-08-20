#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import random
import re
import sys
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
    os.getenv(
        "GEMINI_TEXT_MODEL"
    )
    or ""
).strip() or "gemini-3.6-flash"


# ============================================================
# Retry settings
# ============================================================

CONCEPT_MAX_ATTEMPTS = int(
    os.getenv(
        "CONCEPT_MAX_ATTEMPTS",
        "3",
    )
)

GEMINI_API_MAX_ATTEMPTS = int(
    os.getenv(
        "GEMINI_API_MAX_ATTEMPTS",
        "5",
    )
)

GEMINI_RETRY_BASE_SECONDS = float(
    os.getenv(
        "GEMINI_RETRY_BASE_SECONDS",
        "10",
    )
)


def required_env(
    name: str,
) -> str:

    value = os.getenv(
        name,
        "",
    ).strip()

    if not value:
        raise RuntimeError(
            "Missing required environment "
            f"variable: {name}"
        )

    return value


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


def clean_json_text(
    value: str,
) -> str:

    cleaned = value.strip()

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


def text(
    *values: Any,
) -> str:

    for value in values:

        if (
            isinstance(
                value,
                str,
            )
            and value.strip()
        ):
            return value.strip()

    return ""


# ============================================================
# Approved story handling
# ============================================================

def find_story_container(
    data: dict[str, Any],
) -> dict[str, Any]:
    """
    Locate the approved editorial story while tolerating
    the state shapes used by The Daily Duck so far.
    """

    candidate_keys = (
        "approved_story",
        "story",
        "recommended_story",
        "recommended",
        "selected_story",
        "gate_a_approved_story",
    )

    for key in candidate_keys:

        value = data.get(
            key
        )

        if isinstance(
            value,
            dict,
        ):
            return value

    package = data.get(
        "package"
    )

    if isinstance(
        package,
        dict,
    ):

        for key in candidate_keys:

            value = package.get(
                key
            )

            if isinstance(
                value,
                dict,
            ):
                return value

    # Compatibility with states that store the approved
    # editorial package at the root.
    if any(
        key in data
        for key in (
            "title",
            "title_en",
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


def validate_approved_state(
    data: dict[str, Any],
) -> None:

    state = str(
        data.get(
            "state",
            "",
        )
    ).strip().upper()

    if state != "APPROVED_STORY":
        raise ValueError(
            "Image concepts may only be generated "
            "from APPROVED_STORY; "
            f"got {state!r}."
        )


# ============================================================
# Gemini retry
# ============================================================

def is_retryable_gemini_error(
    exc: Exception,
) -> bool:

    error_text = str(
        exc
    ).lower()

    retryable_markers = (
        "429",
        "500",
        "502",
        "503",
        "504",
        "resource_exhausted",
        "internal",
        "bad_gateway",
        "unavailable",
        "deadline_exceeded",
        "high demand",
        "temporarily unavailable",
        "service unavailable",
        "timeout",
        "timed out",
    )

    return any(
        marker in error_text
        for marker in retryable_markers
    )


def call_gemini_with_retry(
    client: genai.Client,
    prompt: str,
):

    if GEMINI_API_MAX_ATTEMPTS < 1:
        raise ValueError(
            "GEMINI_API_MAX_ATTEMPTS "
            "must be at least 1."
        )

    last_error: Exception | None = None

    for attempt in range(
        1,
        GEMINI_API_MAX_ATTEMPTS + 1,
    ):

        print(
            "Gemini API request attempt "
            f"{attempt}/"
            f"{GEMINI_API_MAX_ATTEMPTS}..."
        )

        try:

            response = (
                client.models.generate_content(
                    model=TEXT_MODEL,
                    contents=prompt,
                )
            )

            print(
                "Gemini API request succeeded."
            )

            return response

        except Exception as exc:

            last_error = exc

            if not (
                is_retryable_gemini_error(
                    exc
                )
            ):
                raise

            if (
                attempt
                >= GEMINI_API_MAX_ATTEMPTS
            ):
                raise

            wait_seconds = (
                GEMINI_RETRY_BASE_SECONDS
                * (2 ** (attempt - 1))
                + random.uniform(
                    0,
                    3,
                )
            )

            print(
                "WARNING: Temporary Gemini "
                "API error detected. "
                f"Retrying in approximately "
                f"{wait_seconds:.1f} seconds...",
                file=sys.stderr,
            )

            time.sleep(
                wait_seconds
            )

    if last_error is not None:
        raise last_error

    raise RuntimeError(
        "Gemini retry loop ended unexpectedly."
    )


# ============================================================
# English-first concept generation
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

    # English fields deliberately come first.
    output_example = {
        "concepts": [
            {
                "number":
                    1,

                "title_en":
                    "Short English concept title",

                "concept_en":
                    (
                        "Canonical English description "
                        "of the visual concept"
                    ),

                "composition_en":
                    (
                        "Canonical English composition, "
                        "subject, background, props, "
                        "camera and mood direction"
                    ),

                "generation_prompt_en":
                    (
                        "Detailed production-ready English "
                        "prompt used later to generate five "
                        "real image variations"
                    ),

                "alt_en":
                    "Canonical English alt-text draft",

                "title_ja":
                    "英語正本を基にした自然な日本語タイトル",

                "concept_ja":
                    "英語正本を基にした自然な日本語コンセプト説明",

                "composition_ja":
                    "英語正本を基にした自然な日本語構図説明",

                "alt_ja":
                    "英語正本を基にした自然な日本語alt案",
            }
            for _ in range(
                5
            )
        ]
    }

    prompt = f"""
You are the visual editorial director for The Daily Duck.

LANGUAGE POLICY — MANDATORY:

The Daily Duck is ENGLISH-FIRST.

English is the canonical/master language.
Japanese is a translation derived from the completed English master.

For EACH image concept, create these English master fields FIRST:

1. title_en
2. concept_en
3. composition_en
4. generation_prompt_en
5. alt_en

Only AFTER those English fields are complete, create:

6. title_ja
7. concept_ja
8. composition_ja
9. alt_ja

Japanese must faithfully preserve the English master's meaning,
visual intent, factual limits and tone. Japanese should sound
natural, not like awkward literal machine translation.

TASK:

Create EXACTLY FIVE distinct image concepts for ONE already-approved
Daily Duck story.

These are concepts only.
Do NOT generate actual images in this step.

APPROVAL RULE:

The article has already been approved by the human editor.

Do NOT:
- change the selected article
- replace the selected article
- re-rank the article
- add new factual claims
- reinterpret the story into a different story

FACTUAL RULE:

Use ONLY facts supported by the approved story/editorial package below.

Do not invent:
- names
- people
- dates
- numbers
- locations
- quotations
- organizations
- scientific details
- events
- factual objects or props that would falsely imply unsupported facts

A visual metaphor is allowed only when it is clearly illustrative and
does not falsely present invented details as factual.

BRAND / MASCOT RULES:

Every concept must preserve The Daily Duck mascot identity:

- recognizable friendly yellow duck
- orange beak
- large dark glossy eyes
- small feather tuft
- warm, approachable expression
- story-specific clothing and props are allowed when appropriate

VISUAL DIRECTION:

- simple
- clean
- modern
- charming
- warm
- editorial
- not overly vintage
- no gradients unless the concept naturally requires atmospheric lighting
- no logos
- no watermarks
- no UI screenshots
- avoid embedded text
- avoid dense typography
- make the duck clearly recognizable
- make the story visually understandable at a glance

CONCEPT DIVERSITY:

Return EXACTLY FIVE meaningfully different visual approaches.

They may differ in:
- setting
- framing
- camera angle
- action
- props
- scale
- visual metaphor
- mood
- storytelling approach

Do not return five near-duplicates.

All five must still represent the SAME approved story.

CURRENT DAILY DUCK IMAGE FLOW — IMPORTANT:

This step creates five CONCEPTS.

Then:
1. the human selects ONE concept
2. that selected concept becomes the fixed visual direction
3. the system generates FIVE real image variations from that ONE selected concept
4. the human selects ONE of those five real images
5. only that final selected real image becomes the canonical website image
6. a branded X card is built from the selected image
7. publication remains blocked until the final image selection is complete

Therefore generation_prompt_en must describe the selected concept
precisely enough that it can later produce FIVE visually varied
executions of the SAME concept without changing the concept itself.

GENERATION PROMPT REQUIREMENTS:

generation_prompt_en must:
- be entirely in English
- be production-ready for image generation
- preserve the same concept
- specify mascot identity
- specify subject and setting
- specify composition
- specify mood and visual style
- avoid unsupported facts
- avoid text/logo/watermark generation
- leave room for five execution-level variations while keeping
  the chosen concept unchanged

OUTPUT RULES:

- Return exactly five concepts.
- Return them in numbered order 1 through 5.
- Every required field must contain non-empty text.
- English fields are canonical.
- Japanese fields are translations.
- Return ONLY valid JSON.
- Do not use Markdown fences.

Return exactly this JSON structure:

{json.dumps(
    output_example,
    ensure_ascii=False,
    indent=2,
)}

APPROVED STORY — COMPACT:

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

    required_fields = (
        "title_en",
        "concept_en",
        "composition_en",
        "generation_prompt_en",
        "alt_en",
        "title_ja",
        "concept_ja",
        "composition_ja",
        "alt_ja",
    )

    last_error: Exception | None = None

    for attempt in range(
        1,
        CONCEPT_MAX_ATTEMPTS + 1,
    ):

        try:

            print(
                "Image concept generation attempt "
                f"{attempt}/"
                f"{CONCEPT_MAX_ATTEMPTS}..."
            )

            response = (
                call_gemini_with_retry(
                    client,
                    prompt,
                )
            )

            raw = getattr(
                response,
                "text",
                None,
            )

            if not raw:
                raise RuntimeError(
                    "Gemini returned no image "
                    "concept text."
                )

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
                    "Gemini image concept response "
                    "must be a JSON object."
                )

            concepts = parsed.get(
                "concepts"
            )

            if (
                not isinstance(
                    concepts,
                    list,
                )
                or len(
                    concepts
                ) != 5
            ):
                raise ValueError(
                    "Gemini must return exactly "
                    "five image concepts."
                )

            normalized: list[
                dict[str, Any]
            ] = []

            for index, item in enumerate(
                concepts,
                start=1,
            ):

                if not isinstance(
                    item,
                    dict,
                ):
                    raise ValueError(
                        f"Concept {index} must "
                        "be a JSON object."
                    )

                normalized_item = dict(
                    item
                )

                # Always normalize numbering ourselves.
                normalized_item[
                    "number"
                ] = index

                for field in required_fields:

                    value = text(
                        normalized_item.get(
                            field
                        )
                    )

                    if not value:
                        raise ValueError(
                            f"Concept {index} is "
                            "missing required field: "
                            f"{field}"
                        )

                    normalized_item[
                        field
                    ] = value

                normalized.append(
                    normalized_item
                )

            if attempt > 1:

                print(
                    "Image concepts recovered "
                    "successfully on attempt "
                    f"{attempt}."
                )

            return normalized

        except Exception as exc:

            last_error = exc

            if (
                attempt
                < CONCEPT_MAX_ATTEMPTS
            ):

                print(
                    "WARNING: Incomplete/invalid "
                    "image concepts on attempt "
                    f"{attempt}: {exc}"
                )

                print(
                    "Retrying image concept "
                    "generation..."
                )

                continue

            print(
                "ERROR: Image concept generation "
                "failed after "
                f"{CONCEPT_MAX_ATTEMPTS} attempts.",
                file=sys.stderr,
            )

    if last_error is not None:
        raise last_error

    raise RuntimeError(
        "Image concept generation "
        "ended unexpectedly."
    )


# ============================================================
# Main
# ============================================================

def main() -> int:

    approved_state = load_json(
        APPROVED_STORY_PATH
    )

    validate_approved_state(
        approved_state
    )

    story = find_story_container(
        approved_state
    )

    concepts = generate_concepts(
        approved_state,
        story,
    )

    generated_at = datetime.now(
        timezone.utc
    ).isoformat()

    result_data = {
        "state":
            "IMAGE_CONCEPT_REVIEW",

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

        "selection_rule":
            (
                "Reply with exactly one digit: "
                "1, 2, 3, 4, or 5."
            ),

        "next_state_after_valid_selection":
            "APPROVED_IMAGE_CONCEPT",

        "post_selection_flow": (
            "Selected concept -> generate exactly five "
            "real image variations -> human selects one "
            "final image -> READY_TO_PUBLISH"
        ),
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
            result_data,
            f,
            ensure_ascii=False,
            indent=2,
        )

        f.write(
            "\n"
        )

    print(
        f"STATE: {result_data['state']}"
    )

    print(
        "LANGUAGE: ENGLISH-FIRST"
    )

    print(
        "Generated exactly "
        f"{len(concepts)} image concepts."
    )

    print(
        f"Saved: {OUTPUT_PATH}"
    )

    print(
        "NEXT: Human selects one concept, "
        "then five real images are generated "
        "from that selected concept."
    )

    for concept in concepts:

        print(
            f"{concept['number']}: "
            f"{concept['title_en']} "
            f"/ {concept['title_ja']}"
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
