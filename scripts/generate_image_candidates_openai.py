#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI


# ============================================================
# Paths
# ============================================================

APPROVED_CONCEPT_PATH = Path(
    "automation_state/approved_image_concept.json"
)

OUTPUT_STATE_PATH = Path(
    "automation_state/image_candidates.json"
)

OUTPUT_DIR = Path(
    "automation_state/image_candidates"
)


# ============================================================
# OpenAI image settings
# ============================================================

IMAGE_MODEL = (
    os.getenv("OPENAI_IMAGE_MODEL") or ""
).strip() or "gpt-image-2"

IMAGE_SIZE = (
    os.getenv("OPENAI_IMAGE_SIZE") or ""
).strip() or "1536x1024"

IMAGE_QUALITY = (
    os.getenv("OPENAI_IMAGE_QUALITY") or ""
).strip() or "medium"


# ============================================================
# Environment
# ============================================================

def required_env(name: str) -> str:
    value = os.getenv(
        name,
        "",
    ).strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


# ============================================================
# JSON
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

    if not isinstance(
        data,
        dict,
    ):
        raise ValueError(
            f"{path} must contain a JSON object."
        )

    return data


def save_json(
    path: Path,
    data: dict[str, Any],
) -> None:

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


# ============================================================
# Approved concept validation
# ============================================================

def validate_approved_concept(
    data: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:

    state = str(
        data.get("state", "")
    ).strip().upper()

    if state != "APPROVED_IMAGE_CONCEPT":
        raise ValueError(
            "Final image generation requires "
            "APPROVED_IMAGE_CONCEPT; "
            f"got {state!r}."
        )

    concept = data.get(
        "selected_image_concept"
    )

    if not isinstance(
        concept,
        dict,
    ):
        raise ValueError(
            "selected_image_concept is missing."
        )

    number = int(
        data.get(
            "selected_image_concept_number",
            0,
        )
    )

    if number not in (
        1,
        2,
        3,
        4,
        5,
    ):
        raise ValueError(
            "selected_image_concept_number "
            "must be 1-5."
        )

    prompt = str(
        concept.get(
            "generation_prompt_en",
            "",
        )
    ).strip()

    if not prompt:
        raise ValueError(
            "Selected concept has no "
            "generation_prompt_en."
        )

    story = data.get(
        "story"
    )

    if not isinstance(
        story,
        dict,
    ):
        story = {}

    return story, concept


# ============================================================
# Text helpers
# ============================================================

def first_text(
    *values: Any,
) -> str:

    for value in values:

        if (
            isinstance(value, str)
            and value.strip()
        ):
            return value.strip()

    return ""


# ============================================================
# Prompt
# ============================================================

def build_prompt(
    story: dict[str, Any],
    concept: dict[str, Any],
    variation_number: int,
) -> str:

    title = first_text(
        story.get("title_ja"),
        story.get("title"),
        story.get("headline_ja"),
        story.get("headline"),
    )

    reason = first_text(
        story.get("reason_ja"),
        story.get("reason"),
        story.get("recommended_reason"),
    )

    source = first_text(
        story.get("source"),
    )

    concept_title = first_text(
        concept.get("title_en"),
        concept.get("title_ja"),
    )

    concept_en = first_text(
        concept.get("concept_en"),
        concept.get("concept_ja"),
    )

    composition_en = first_text(
        concept.get("composition_en"),
        concept.get("composition_ja"),
    )

    production_prompt = first_text(
        concept.get("generation_prompt_en"),
    )

    # Variation instructions deliberately vary framing/detail
    # while preserving the exact same selected concept.
    variation_directions = {
        1: (
            "Use the selected concept in its most balanced, "
            "canonical editorial composition."
        ),

        2: (
            "Keep the exact same concept, but explore a slightly "
            "closer framing and stronger emotional focus on the duck."
        ),

        3: (
            "Keep the exact same concept, but explore a wider and "
            "more cinematic environmental composition."
        ),

        4: (
            "Keep the exact same concept, but vary camera angle, "
            "lighting, and spatial arrangement while preserving all "
            "core visual elements."
        ),

        5: (
            "Keep the exact same concept, but create a polished "
            "alternative composition with fresh pose and framing, "
            "without changing the concept itself."
        ),
    }

    variation_direction = (
        variation_directions[
            variation_number
        ]
    )

    return f"""
Create ONE polished final image candidate for The Daily Duck.

This is FINAL IMAGE VARIATION {variation_number} OF 5.

CRITICAL RULE:
All five final candidates must use the SAME already-selected visual concept.

Do NOT:
- invent a new concept
- switch to another concept
- reinterpret the story into a different visual idea
- change the core setting or symbolic meaning

APPROVED STORY

TITLE:
{title}

WHY THIS STORY:
{reason}

SOURCE:
{source}


LOCKED SELECTED VISUAL CONCEPT

TITLE:
{concept_title}

CONCEPT:
{concept_en}

COMPOSITION:
{composition_en}

PRODUCTION DIRECTION:
{production_prompt}


VARIATION DIRECTION

{variation_direction}

This variation may differ only in:
- camera angle
- pose
- framing
- lighting
- spatial arrangement
- small non-factual visual details
- depth and composition

It must remain unmistakably the SAME selected concept.


PERMANENT THE DAILY DUCK MASCOT

- one recognizable cheerful yellow duck
- orange beak
- large dark glossy eyes
- small feather tuft
- friendly warm expression
- consistent mascot identity
- charming but not childish


VISUAL STYLE

- premium modern editorial illustration
- soft 3D / polished illustrated look where appropriate
- simple and clean
- emotionally uplifting
- not overly vintage
- strong single focal point
- polished publication quality
- suitable for The Daily Duck website hero image
- suitable for X post image
- landscape composition
- rich but natural lighting


FACTUAL SAFETY

Do not introduce unsupported:
- people
- places
- organizations
- text
- dates
- numbers
- scientific details
- historical details
- branded objects

Use only elements supported by the approved story
or clearly symbolic/editorial visual elements.


DO NOT INCLUDE

- headline text
- captions
- numbers
- labels
- logos
- watermarks
- UI
- speech bubbles
- readable text


FINAL REQUIREMENT

This image must be a high-quality alternative execution
of the SAME locked selected concept.

Do not create a different concept.
""".strip()


# ============================================================
# SHA256
# ============================================================

def sha256_file(
    path: Path,
) -> str:

    h = hashlib.sha256()

    with path.open(
        "rb"
    ) as f:

        for chunk in iter(
            lambda: f.read(
                1024 * 1024
            ),
            b"",
        ):
            h.update(
                chunk
            )

    return h.hexdigest()


# ============================================================
# OpenAI retry
# ============================================================

def is_retryable_openai_error(
    exc: Exception,
) -> bool:

    message = str(
        exc
    ).upper()

    retry_tokens = (
        "429",
        "RATE LIMIT",
        "500",
        "502",
        "503",
        "504",
        "TIMEOUT",
        "TIMED OUT",
        "TEMPORAR",
        "SERVER ERROR",
        "INTERNAL",
    )

    return any(
        token in message
        for token in retry_tokens
    )


def generate_with_retry(
    client: OpenAI,
    prompt: str,
    candidate_number: int,
    max_attempts: int = 4,
) -> bytes:

    last_exception: Exception | None = None

    for attempt in range(
        1,
        max_attempts + 1,
    ):

        try:

            print(
                f"Candidate {candidate_number}: "
                f"OpenAI attempt "
                f"{attempt}/{max_attempts}"
            )

            result = (
                client.images.generate(
                    model=IMAGE_MODEL,
                    prompt=prompt,
                    size=IMAGE_SIZE,
                    quality=IMAGE_QUALITY,
                    n=1,
                )
            )

            if (
                not result.data
                or not result.data[0].b64_json
            ):
                raise RuntimeError(
                    "OpenAI returned no image data."
                )

            return base64.b64decode(
                result.data[0].b64_json
            )

        except Exception as exc:

            last_exception = exc

            if not is_retryable_openai_error(
                exc
            ):
                raise

            if attempt >= max_attempts:
                raise

            wait_seconds = (
                min(
                    45,
                    (2 ** attempt) * 4,
                )
                + random.uniform(
                    0,
                    2,
                )
            )

            print(
                "Temporary OpenAI image API "
                "error detected."
            )

            print(
                str(exc)[:500]
            )

            print(
                f"Waiting {wait_seconds:.1f}s..."
            )

            time.sleep(
                wait_seconds
            )

    if last_exception:
        raise last_exception

    raise RuntimeError(
        "Image generation failed unexpectedly."
    )


# ============================================================
# Main
# ============================================================

def main() -> None:

    required_env(
        "OPENAI_API_KEY"
    )

    approved = load_json(
        APPROVED_CONCEPT_PATH
    )

    story, concept = (
        validate_approved_concept(
            approved
        )
    )

    selected_concept_number = int(
        approved[
            "selected_image_concept_number"
        ]
    )

    issue_date = str(
        approved.get(
            "issue_date"
        )
        or approved.get(
            "date"
        )
        or ""
    ).strip()

    print(
        "APPROVED_IMAGE_CONCEPT confirmed."
    )

    print(
        "Selected concept: "
        f"{selected_concept_number}"
    )

    print(
        "Generating exactly five final "
        "image variations from ONLY "
        "the selected concept."
    )

    client = OpenAI(
        api_key=os.environ[
            "OPENAI_API_KEY"
        ]
    )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # Determine generation round
    # --------------------------------------------------------

    previous_round = 0

    if OUTPUT_STATE_PATH.exists():

        try:
            previous = load_json(
                OUTPUT_STATE_PATH
            )

            previous_round = int(
                previous.get(
                    "generation_round",
                    0,
                )
            )

        except Exception:
            previous_round = 0

    generation_round = (
        previous_round + 1
    )

    # --------------------------------------------------------
    # Remove stale candidate files
    # --------------------------------------------------------

    for old_file in OUTPUT_DIR.glob(
        "candidate_*.png"
    ):
        old_file.unlink()

    candidates: list[
        dict[str, Any]
    ] = []

    # --------------------------------------------------------
    # Generate candidates 1-5
    # --------------------------------------------------------

    for candidate_number in range(
        1,
        6,
    ):

        prompt = build_prompt(
            story=story,
            concept=concept,
            variation_number=candidate_number,
        )

        print(
            f"Generating final candidate "
            f"{candidate_number}/5 "
            f"from selected concept "
            f"#{selected_concept_number}"
        )

        image_bytes = generate_with_retry(
            client=client,
            prompt=prompt,
            candidate_number=candidate_number,
        )

        image_path = (
            OUTPUT_DIR
            / f"candidate_{candidate_number}.png"
        )

        image_path.write_bytes(
            image_bytes
        )

        candidates.append(
            {
                "number":
                    candidate_number,

                "selected_concept_number":
                    selected_concept_number,

                "concept_title_ja":
                    first_text(
                        concept.get(
                            "title_ja"
                        )
                    ),

                "concept_title_en":
                    first_text(
                        concept.get(
                            "title_en"
                        )
                    ),

                "concept_ja":
                    first_text(
                        concept.get(
                            "concept_ja"
                        )
                    ),

                "concept_en":
                    first_text(
                        concept.get(
                            "concept_en"
                        )
                    ),

                "image_path":
                    image_path.as_posix(),

                "generation_prompt":
                    prompt,

                "model":
                    IMAGE_MODEL,

                "size":
                    IMAGE_SIZE,

                "quality":
                    IMAGE_QUALITY,

                "sha256":
                    sha256_file(
                        image_path
                    ),
            }
        )

    # --------------------------------------------------------
    # Result state
    # --------------------------------------------------------

    result_state = {
        "state":
            "IMAGE_CANDIDATES_READY",

        "issue_date":
            issue_date,

        "generated_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "generation_round":
            generation_round,

        "source_state":
            "APPROVED_IMAGE_CONCEPT",

        "source_path":
            str(
                APPROVED_CONCEPT_PATH
            ),

        "story":
            story,

        "selected_image_concept_number":
            selected_concept_number,

        "selected_image_concept":
            concept,

        "candidate_count":
            5,

        "candidates":
            candidates,

        "selection_rule":
            (
                "Reply with exactly 1, 2, 3, 4, "
                "or 5 to select the canonical image."
            ),

        "regeneration_rule":
            (
                "Reply NEXT 5 to generate five "
                "new candidates from the SAME "
                "selected image concept."
            ),

        "next_state_after_valid_selection":
            "READY_TO_PUBLISH",
    }

    save_json(
        OUTPUT_STATE_PATH,
        result_state,
    )

    print(
        "STATE: IMAGE_CANDIDATES_READY"
    )

    print(
        "Generated exactly 5 final "
        "image candidates."
    )

    print(
        "All five use selected concept "
        f"#{selected_concept_number}."
    )

    print(
        f"Generation round: "
        f"{generation_round}"
    )

    print(
        f"Saved state: "
        f"{OUTPUT_STATE_PATH}"
    )


if __name__ == "__main__":
    main()
