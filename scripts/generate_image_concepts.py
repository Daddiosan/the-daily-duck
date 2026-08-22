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

OPTIONS_PATH = Path(
    "automation_state/design_options.json"
)

TEXT_MODEL = (
    os.getenv(
        "GEMINI_TEXT_MODEL"
    )
    or ""
).strip() or "gemini-3.6-flash"


# ============================================================
# Generation settings
# ============================================================

IMAGE_CONCEPT_COUNT = 3
TITLE_IDEA_COUNT = 3

EDITORIAL_MAX_ATTEMPTS = int(
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


# ============================================================
# Basic helpers
# ============================================================

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

    data = json.loads(
        path.read_text(
            encoding="utf-8"
        )
    )

    if not isinstance(
        data,
        dict,
    ):

        raise ValueError(
            f"{path} must contain "
            "a JSON object."
        )

    return data


def first_text(
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


# ============================================================
# Approved story validation
# ============================================================

def validate_approved_state(
    data: dict[str, Any],
) -> None:

    state = first_text(
        data.get(
            "state"
        )
    ).upper()

    if state != "APPROVED_STORY":

        raise ValueError(
            "Design options may only be "
            "generated from APPROVED_STORY; "
            f"got {state!r}."
        )


def find_approved_story(
    data: dict[str, Any],
) -> dict[str, Any]:
    """
    Return the selected Gate A story while
    tolerating the state shapes used by
    The Daily Duck during Phase 2.
    """

    for key in (
        "approved_story",
        "selected_story",
        "gate_a_approved_story",
        "story",
        "recommended_story",
    ):

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

        for key in (
            "approved_story",
            "selected_story",
            "gate_a_approved_story",
            "story",
            "recommended_story",
        ):

            value = package.get(
                key
            )

            if isinstance(
                value,
                dict,
            ):

                return value

    # Some versions persist the approved
    # editorial package at root level.

    if any(
        key in data
        for key in (
            "title_en",
            "title",
            "en_copy",
            "jp_copy",
            "duck_name",
            "x_en",
        )
    ):

        return data

    raise ValueError(
        "Could not locate the approved story "
        "in approved_story.json."
    )


def issue_date_from(
    data: dict[str, Any],
    story: dict[str, Any],
) -> str:

    issue_date = first_text(
        data.get(
            "issue_date"
        ),
        data.get(
            "date"
        ),
        story.get(
            "issue_date"
        ),
        story.get(
            "date"
        ),
    )

    if not issue_date:

        raise ValueError(
            "Approved story is missing "
            "issue_date/date."
        )

    return issue_date


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

    last_error: (
        Exception | None
    ) = None

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
                "API error. "
                "Retrying in approximately "
                f"{wait_seconds:.1f} seconds...",
                file=sys.stderr,
            )

            time.sleep(
                wait_seconds
            )

    if last_error is not None:

        raise last_error

    raise RuntimeError(
        "Gemini retry loop "
        "ended unexpectedly."
    )


# ============================================================
# Generate concepts + title ideas
# ============================================================

def generate_options(
    approved_state: dict[str, Any],
    approved_story: dict[str, Any],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """
    Generate:

      - exactly THREE image concepts
      - exactly THREE English title options

    English is canonical.
    Japanese fields are review translations only.
    """

    client = genai.Client(
        api_key=required_env(
            "GEMINI_API_KEY"
        )
    )

    output_example = {

        "image_concepts": [

            {
                "number":
                    1,

                "title_en":
                    "Short English concept title",

                "concept_en":
                    "Canonical English visual concept",

                "composition_en":
                    (
                        "Canonical English composition, "
                        "setting, framing, subject, props "
                        "and mood direction"
                    ),

                "generation_prompt_en":
                    (
                        "Production-ready English prompt "
                        "for generating five real variations "
                        "of this same concept later"
                    ),

                "alt_en":
                    "Canonical English alt-text draft",

                "title_ja":
                    "自然な日本語コンセプト名",

                "concept_ja":
                    "英語正本を基にした自然な日本語説明",

                "composition_ja":
                    "英語正本を基にした自然な日本語構図説明",

                "alt_ja":
                    "英語正本を基にした日本語alt案",
            }

            for _ in range(
                IMAGE_CONCEPT_COUNT
            )
        ],

        "title_ideas": [

            {
                "number":
                    1,

                "title":
                    "PUNCHY ENGLISH TITLE",

                "meaning_ja":
                    (
                        "日本語で意味・ニュアンスを"
                        "簡潔に説明"
                    ),
            }

            for _ in range(
                TITLE_IDEA_COUNT
            )
        ],
    }

    prompt = f"""
You are the visual editorial director for The Daily Duck.

The Daily Duck is ENGLISH-FIRST.

English is the canonical/master language.
Japanese is review translation only.

You are working on ONE story that has already passed Gate A.

Do not change the story.

============================================================
TASK A — IMAGE CONCEPTS
============================================================

Create EXACTLY THREE distinct visual concepts
for the approved story.

Do NOT create four or five concepts.

Return exactly concepts:

1
2
3

For every concept, create the English master fields FIRST:

1. title_en
2. concept_en
3. composition_en
4. generation_prompt_en
5. alt_en

Only after the English master is complete, create the Japanese
review translations:

6. title_ja
7. concept_ja
8. composition_ja
9. alt_ja

The THREE concepts must be meaningfully different visual approaches,
but all THREE must represent the SAME approved story.

The human will select exactly ONE concept.

After that selection, the system will generate EXACTLY FIVE real
image variations from that ONE selected concept.

Therefore generation_prompt_en must be precise enough to LOCK the
selected concept while still allowing execution-level variation such as:

- duck pose
- subtle camera angle
- crop
- lighting nuance
- small prop placement
- spacing
- depth of field

It must NOT allow the later image generator to switch
to another visual concept.

============================================================
THE DAILY DUCK MASCOT
============================================================

Every concept must preserve:

- recognizable friendly yellow duck
- orange beak
- large dark glossy eyes
- small feather tuft
- warm approachable expression
- consistent mascot identity

============================================================
VISUAL DIRECTION
============================================================

- clean
- modern
- charming
- warm
- premium editorial
- simple rather than overly vintage
- strong single focal point
- landscape hero-image composition
- no logos
- no watermarks
- no UI
- no readable embedded text
- no headline inside the generated image

============================================================
FACTUAL RULE
============================================================

Use ONLY facts supported by the approved story package.

Do not invent:

- names
- people
- dates
- numbers
- locations
- quotations
- organizations
- scientific details
- factual props that imply unsupported facts

Illustrative visual metaphor is allowed only when
it does not falsely present invented details as factual.

============================================================
TASK B — TITLE IDEAS
============================================================

Create EXACTLY THREE short, punchy ENGLISH
publication-title options for the SAME approved story.

The title options must:

- be English only
- be concise
- be memorable
- fit The Daily Duck tone
- not invent facts
- not use clickbait that changes the meaning
- be suitable for the website and X card

For each title, meaning_ja should explain
the English meaning and nuance naturally
in Japanese for the human editor.

============================================================
OUTPUT RULES
============================================================

- Exactly THREE image concepts.
- Exactly THREE title ideas.
- Number image concepts 1-3.
- Number title ideas 1-3.
- Every required field must be non-empty.
- English is canonical.
- Japanese is translation/review support.
- Return ONLY valid JSON.
- No Markdown fences.

Return exactly this structure:

{json.dumps(
    output_example,
    ensure_ascii=False,
    indent=2,
)}

============================================================
APPROVED STORY
============================================================

{json.dumps(
    approved_story,
    ensure_ascii=False,
    indent=2,
)}

============================================================
FULL APPROVED STATE
============================================================

{json.dumps(
    approved_state,
    ensure_ascii=False,
    indent=2,
)}
""".strip()

    required_concept_fields = (
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

    required_title_fields = (
        "title",
        "meaning_ja",
    )

    last_error: (
        Exception | None
    ) = None

    for attempt in range(
        1,
        EDITORIAL_MAX_ATTEMPTS + 1,
    ):

        try:

            print(
                "Design option generation "
                f"attempt {attempt}/"
                f"{EDITORIAL_MAX_ATTEMPTS}..."
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
                    "Gemini returned no "
                    "design option text."
                )

            parsed = json.loads(
                clean_json_text(
                    raw
                )
            )

            if not isinstance(
                parsed,
                dict,
            ):

                raise ValueError(
                    "Gemini response must "
                    "be a JSON object."
                )

            concepts = parsed.get(
                "image_concepts"
            )

            titles = parsed.get(
                "title_ideas"
            )

            # ------------------------------------------------
            # Exact concept count = 3
            # ------------------------------------------------

            if (
                not isinstance(
                    concepts,
                    list,
                )
                or len(
                    concepts
                ) != IMAGE_CONCEPT_COUNT
            ):

                raise ValueError(
                    "Gemini must return exactly "
                    f"{IMAGE_CONCEPT_COUNT} "
                    "image concepts."
                )

            # ------------------------------------------------
            # Exact title count = 3
            # ------------------------------------------------

            if (
                not isinstance(
                    titles,
                    list,
                )
                or len(
                    titles
                ) != TITLE_IDEA_COUNT
            ):

                raise ValueError(
                    "Gemini must return exactly "
                    f"{TITLE_IDEA_COUNT} "
                    "title ideas."
                )

            normalized_concepts: list[
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
                        "Image concept "
                        f"{index} must "
                        "be an object."
                    )

                normalized = dict(
                    item
                )

                normalized[
                    "number"
                ] = index

                for field in (
                    required_concept_fields
                ):

                    value = first_text(
                        normalized.get(
                            field
                        )
                    )

                    if not value:

                        raise ValueError(
                            "Image concept "
                            f"{index} is missing "
                            f"{field}."
                        )

                    normalized[
                        field
                    ] = value

                normalized_concepts.append(
                    normalized
                )

            normalized_titles: list[
                dict[str, Any]
            ] = []

            for index, item in enumerate(
                titles,
                start=1,
            ):

                if not isinstance(
                    item,
                    dict,
                ):

                    raise ValueError(
                        "Title idea "
                        f"{index} must "
                        "be an object."
                    )

                normalized = dict(
                    item
                )

                normalized[
                    "number"
                ] = index

                for field in (
                    required_title_fields
                ):

                    value = first_text(
                        normalized.get(
                            field
                        )
                    )

                    if not value:

                        raise ValueError(
                            "Title idea "
                            f"{index} is missing "
                            f"{field}."
                        )

                    normalized[
                        field
                    ] = value

                normalized_titles.append(
                    normalized
                )

            return (
                normalized_concepts,
                normalized_titles,
            )

        except Exception as exc:

            last_error = exc

            if (
                attempt
                < EDITORIAL_MAX_ATTEMPTS
            ):

                print(
                    "WARNING: Invalid/incomplete "
                    "design option package: "
                    f"{exc}"
                )

                print(
                    "Retrying design option "
                    "generation..."
                )

                continue

    if last_error is not None:

        raise last_error

    raise RuntimeError(
        "Design option generation "
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

    approved_story = (
        find_approved_story(
            approved_state
        )
    )

    issue_date = issue_date_from(
        approved_state,
        approved_story,
    )

    (
        image_concepts,
        title_ideas,
    ) = generate_options(
        approved_state,
        approved_story,
    )

    package = {

        "state":
            "CONCEPTS_READY",

        "issue_date":
            issue_date,

        "generated_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        # Preserve the complete Gate A approved
        # state so later stages retain every field
        # needed by website/X publication.

        "approved_story":
            approved_state,

        "approved_story_compact":
            approved_story,

        "image_concepts":
            image_concepts,

        "title_ideas":
            title_ideas,

        "selected_image_concept_number":
            None,

        "selected_image_concept":
            None,

        "design_previews":
            [],

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

        # ----------------------------------------------------
        # Human chooses ONE of the THREE concepts.
        # ----------------------------------------------------

        "concept_selection_rule": {

            "valid_replies":
                [
                    "1",
                    "2",
                    "3",
                ],

            "next_state":
                "APPROVED_IMAGE_CONCEPT",
        },

        # ----------------------------------------------------
        # After concept selection:
        # generate FIVE real images from that ONE concept.
        # ----------------------------------------------------

        "real_image_flow": {

            "source":
                "one_human_selected_concept",

            "candidate_count":
                5,

            "next_5_supported":
                True,
        },

        # ----------------------------------------------------
        # Final selection:
        #
        # image = 1-5
        # title = 1-3
        #
        # Example:
        #   4 1
        # ----------------------------------------------------

        "final_selection_rule": {

            "image_numbers":
                [
                    1,
                    2,
                    3,
                    4,
                    5,
                ],

            "title_numbers":
                [
                    1,
                    2,
                    3,
                ],

            "format":
                "IMAGE_NUMBER TITLE_NUMBER",

            "example":
                "4 1",

            "next_5_command":
                "NEXT 5",

            "next_state":
                "READY_TO_PUBLISH",
        },
    }

    OPTIONS_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    OPTIONS_PATH.write_text(
        json.dumps(
            package,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        "Generated exactly "
        f"{IMAGE_CONCEPT_COUNT} "
        "image concepts."
    )

    print(
        "Generated exactly "
        f"{TITLE_IDEA_COUNT} "
        "English title ideas."
    )

    print(
        "Concept selection replies: "
        "1 / 2 / 3"
    )

    print(
        "Real image count after "
        "concept selection: 5"
    )

    print(
        "LANGUAGE: ENGLISH-FIRST"
    )

    print(
        f"Saved: {OPTIONS_PATH}"
    )

    print(
        "STATE: CONCEPTS_READY"
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
