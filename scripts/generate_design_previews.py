#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from openai import OpenAI
from model_config import (
    OPENAI_IMAGE_MODEL,
    OPENAI_IMAGE_SIZE,
    OPENAI_IMAGE_QUALITY,
)


OPTIONS_PATH = Path(
    "automation_state/design_options.json"
)

PREVIEW_ROOT = Path(
    "automation_images/design_previews"
)

# Number of real images generated from ONE selected concept.
REAL_IMAGE_COUNT = 5

# Number of retries when OpenAI returns byte-identical output.
MAX_DUPLICATE_RETRIES = 2


# ============================================================
# Five intentionally different execution recipes.
#
# IMPORTANT:
# These are NOT five concepts.
#
# The human selects ONE of the THREE concepts first.
# These recipes then produce FIVE visual variations
# from that ONE locked concept.
# ============================================================

VARIATION_RECIPES: dict[int, str] = {

    1: """
VARIATION 1 — ESTABLISHING VIEW

- Use a wider establishing composition.
- Show more of the environment while keeping the duck the clear focal point.
- Camera at approximately eye level.
- Duck positioned slightly off-center using editorial negative space.
- Calm, balanced pose.
""".strip(),

    2: """
VARIATION 2 — CLOSE HERO VIEW

- Use a substantially closer hero framing than Variation 1.
- Duck occupies much more of the frame.
- Camera slightly lower than eye level for a confident, charming hero feel.
- Strong facial expression and readable body language.
- Background simplified and more softly separated.
""".strip(),

    3: """
VARIATION 3 — ACTION MOMENT

- Show the duck actively interacting with the key visual element of the SAME concept.
- Use a clear sense of motion or mid-action storytelling.
- Three-quarter camera angle.
- More dynamic diagonal composition than Variations 1 and 2.
- Keep the same setting, props, and visual concept.
""".strip(),

    4: """
VARIATION 4 — SIDE STORYTELLING VIEW

- Use a noticeably different side or three-quarter-side camera position.
- Place the duck on the opposite side of the frame from Variation 1.
- Emphasize the relationship between the duck and the concept's key environment/prop.
- Use layered foreground/background depth.
- Keep the exact same visual concept.
""".strip(),

    5: """
VARIATION 5 — CINEMATIC EDITORIAL VIEW

- Use the most cinematic composition of the set.
- Distinct crop and perspective from Variations 1-4.
- Slightly elevated or otherwise clearly different camera viewpoint.
- Strong editorial lighting and depth while remaining warm and cheerful.
- Preserve the same story, setting, mascot, and locked concept.
""".strip(),
}


# ============================================================
# Helpers
# ============================================================

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


def sha256_bytes(
    data: bytes,
) -> str:

    return hashlib.sha256(
        data
    ).hexdigest()


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
# Approved story helper
# ============================================================

def get_compact_story(
    package: dict[str, Any],
) -> dict[str, Any]:

    compact = package.get(
        "approved_story_compact"
    )

    if isinstance(
        compact,
        dict,
    ):

        return compact

    approved = package.get(
        "approved_story"
    )

    if not isinstance(
        approved,
        dict,
    ):

        raise ValueError(
            "approved_story is missing."
        )

    for key in (
        "approved_story",
        "selected_story",
        "gate_a_approved_story",
        "story",
        "recommended_story",
    ):

        value = approved.get(
            key
        )

        if isinstance(
            value,
            dict,
        ):

            return value

    return approved


# ============================================================
# Prompt
# ============================================================

def build_prompt(
    package: dict[str, Any],
    concept: dict[str, Any],
    variation_number: int,
    batch_number: int,
    duplicate_retry: int = 0,
) -> str:

    story = get_compact_story(
        package
    )

    headline = first_text(
        story.get(
            "title_en"
        ),
        story.get(
            "title"
        ),
        story.get(
            "title_ja"
        ),
    )

    summary = first_text(
        story.get(
            "reason_en"
        ),
        story.get(
            "en_copy"
        ),
        story.get(
            "reason"
        ),
        story.get(
            "jp_copy"
        ),
    )

    source = first_text(
        story.get(
            "source"
        )
    )

    concept_title = first_text(
        concept.get(
            "title_en"
        )
    )

    concept_text = first_text(
        concept.get(
            "concept_en"
        )
    )

    composition = first_text(
        concept.get(
            "composition_en"
        )
    )

    generation_prompt = first_text(
        concept.get(
            "generation_prompt_en"
        )
    )

    if not concept_text:

        raise ValueError(
            "Selected concept is missing "
            "concept_en."
        )

    if not generation_prompt:

        raise ValueError(
            "Selected concept is missing "
            "generation_prompt_en."
        )

    if variation_number not in (
        1,
        2,
        3,
        4,
        5,
    ):

        raise ValueError(
            "variation_number "
            "must be 1-5."
        )

    variation_recipe = (
        VARIATION_RECIPES[
            variation_number
        ]
    )

    retry_note = ""

    if duplicate_retry > 0:

        retry_note = f"""
DUPLICATE AVOIDANCE — RETRY {duplicate_retry}

The previous output was too similar or identical.

Make this rendition CLEARLY DIFFERENT in:

- camera framing
- duck pose
- crop
- spatial arrangement
- perspective

while preserving the locked concept.
""".strip()

    return f"""
Create ONE polished, publishable hero-image candidate for The Daily Duck.

============================================================
LANGUAGE POLICY
============================================================

All image-generation direction is canonical English.

Do not add Japanese or any other readable text to the image.

============================================================
APPROVED STORY — LOCKED
============================================================

Headline:
{headline}

Summary:
{summary}

Source:
{source}

============================================================
HUMAN-SELECTED IMAGE CONCEPT — LOCKED
============================================================

Concept title:
{concept_title}

Concept:
{concept_text}

Composition:
{composition}

Production direction:
{generation_prompt}

============================================================
CURRENT IMAGE
============================================================

This is:

- real-image batch {batch_number}
- variation {variation_number} of {REAL_IMAGE_COUNT}

IMPORTANT:

The five images must represent the SAME selected concept,
but they must NOT look like duplicate renders.

The following remain LOCKED:

- approved story
- selected visual concept
- setting
- mascot identity
- central visual idea

============================================================
MANDATORY VARIATION RECIPE
============================================================

{variation_recipe}

{retry_note}

============================================================
VARIATION REQUIREMENT
============================================================

Compared with the other candidates,
this image must be visibly different in
at least THREE of these execution dimensions:

- camera distance
- camera angle
- duck pose
- duck placement in frame
- crop
- foreground/background depth
- lighting direction
- prop placement
- body orientation

Do NOT merely make tiny facial,
lighting or texture changes.

============================================================
CRITICAL CONCEPT-LOCK RULE
============================================================

Do NOT:

- switch to another concept
- reinterpret the central visual idea
- replace the setting with a different setting
- change the story
- add unsupported factual details

============================================================
THE DAILY DUCK MASCOT
============================================================

Preserve the established mascot identity:

- cheerful recognizable yellow duck
- orange beak
- large dark glossy eyes
- small feather tuft
- warm friendly expression
- consistent mascot identity
- consistent mascot proportions

============================================================
STYLE
============================================================

- premium modern editorial illustration,
  soft 3D, or photorealistic composite as appropriate
- cheerful
- clean
- polished
- simple rather than overly vintage
- landscape composition
- strong focal point
- suitable as a website hero image
- publication quality

============================================================
DO NOT INCLUDE
============================================================

- headline
- captions
- readable text
- numbers
- logos
- UI
- watermarks
- final X-card header/footer

============================================================
FACTUAL RULE
============================================================

Do not invent factual, scientific,
geographic, organizational,
or biographical details beyond what
is supported by the approved story.

This output must be a distinct visual
execution of the SAME selected concept.
""".strip()


# ============================================================
# OpenAI image generation
# ============================================================

def generate_image_bytes(
    client: OpenAI,
    prompt: str,
    number: int,
) -> bytes:

    print(
        f"Generating real image "
        f"{number}/{REAL_IMAGE_COUNT}: "
        f"{OPENAI_IMAGE_MODEL}, "
        f"{OPENAI_IMAGE_SIZE}, "
        f"quality="
        f"{OPENAI_IMAGE_QUALITY}"
    )

    result = (
        client.images.generate(
            model=OPENAI_IMAGE_MODEL,
            prompt=prompt,
            n=1,
            size=OPENAI_IMAGE_SIZE,
            quality=OPENAI_IMAGE_QUALITY,
            output_format="png",
        )
    )

    if (
        not result.data
        or not result.data[0].b64_json
    ):

        raise RuntimeError(
            "OpenAI returned no image "
            f"for candidate {number}."
        )

    return base64.b64decode(
        result.data[0].b64_json
    )


# ============================================================
# Main
# ============================================================

def main() -> int:

    # --------------------------------------------------------
    # Load design state
    # --------------------------------------------------------

    if not OPTIONS_PATH.exists():

        raise FileNotFoundError(
            f"Missing required file: "
            f"{OPTIONS_PATH}"
        )

    package = json.loads(
        OPTIONS_PATH.read_text(
            encoding="utf-8"
        )
    )

    if not isinstance(
        package,
        dict,
    ):

        raise ValueError(
            "design_options.json "
            "must contain an object."
        )

    # --------------------------------------------------------
    # State validation
    # --------------------------------------------------------

    state = first_text(
        package.get(
            "state"
        )
    ).upper()

    if (
        state
        != "APPROVED_IMAGE_CONCEPT"
    ):

        raise RuntimeError(
            "Expected "
            "APPROVED_IMAGE_CONCEPT, "
            f"got {state!r}"
        )

    # --------------------------------------------------------
    # Selected concept
    # --------------------------------------------------------

    selected_concept = package.get(
        "selected_image_concept"
    )

    if not isinstance(
        selected_concept,
        dict,
    ):

        raise ValueError(
            "selected_image_concept "
            "is missing."
        )

    selected_number = int(
        package.get(
            "selected_image_concept_number",
            0,
        )
        or 0
    )

    # IMPORTANT:
    # Current Daily Duck specification:
    #
    # Exactly THREE concept choices.
    #
    # After choosing one of these three,
    # FIVE real images are generated.
    # --------------------------------------------------------

    if selected_number not in (
        1,
        2,
        3,
    ):

        raise ValueError(
            "selected_image_concept_number "
            "must be 1-3."
        )

    # --------------------------------------------------------
    # Issue date
    # --------------------------------------------------------

    issue_date = first_text(
        package.get(
            "issue_date"
        )
    )

    if not issue_date:

        raise ValueError(
            "issue_date is missing."
        )

    # --------------------------------------------------------
    # Batch number
    # --------------------------------------------------------

    previous_batch = int(
        package.get(
            "preview_batch_number",
            0,
        )
        or 0
    )

    batch_number = (
        previous_batch
        + 1
    )

    out_dir = (
        PREVIEW_ROOT
        / issue_date
        / f"batch_{batch_number:02d}"
    )

    out_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # OpenAI
    # --------------------------------------------------------

    api_key = os.getenv(
        "OPENAI_API_KEY",
        "",
    ).strip()

    if not api_key:

        raise RuntimeError(
            "OPENAI_API_KEY "
            "is not configured."
        )

    client = OpenAI(
        api_key=api_key
    )

    previews: list[
        dict[str, Any]
    ] = []

    seen_hashes: set[str] = set()

    # --------------------------------------------------------
    # Generate exactly FIVE real images.
    #
    # IMPORTANT:
    # Each API call generates ONE image.
    # This prevents one returned image being
    # accidentally reused for all five candidates.
    # --------------------------------------------------------

    for variation_number in range(
        1,
        REAL_IMAGE_COUNT + 1,
    ):

        chosen_bytes: (
            bytes | None
        ) = None

        chosen_prompt = ""
        chosen_hash = ""

        # ----------------------------------------------------
        # Duplicate protection
        # ----------------------------------------------------

        for retry in range(
            0,
            MAX_DUPLICATE_RETRIES + 1,
        ):

            prompt = build_prompt(
                package=package,
                concept=selected_concept,
                variation_number=(
                    variation_number
                ),
                batch_number=(
                    batch_number
                ),
                duplicate_retry=retry,
            )

            image_bytes = (
                generate_image_bytes(
                    client=client,
                    prompt=prompt,
                    number=variation_number,
                )
            )

            digest = sha256_bytes(
                image_bytes
            )

            if digest not in seen_hashes:

                chosen_bytes = (
                    image_bytes
                )

                chosen_prompt = (
                    prompt
                )

                chosen_hash = (
                    digest
                )

                break

            print(
                "WARNING: candidate "
                f"{variation_number} "
                "was byte-identical to "
                "an earlier image."
            )

            print(
                "Regenerating candidate "
                f"{variation_number}..."
            )

        if chosen_bytes is None:

            raise RuntimeError(
                "Could not create a "
                "unique image for "
                f"candidate "
                f"{variation_number} "
                "after "
                f"{MAX_DUPLICATE_RETRIES + 1} "
                "attempts."
            )

        seen_hashes.add(
            chosen_hash
        )

        # ----------------------------------------------------
        # Save image
        # ----------------------------------------------------

        path = (
            out_dir
            / (
                f"preview_"
                f"{variation_number}.png"
            )
        )

        path.write_bytes(
            chosen_bytes
        )

        # ----------------------------------------------------
        # Metadata
        # ----------------------------------------------------

        previews.append(
            {
                "number":
                    variation_number,

                "variation_number":
                    variation_number,

                "variation_recipe":
                    VARIATION_RECIPES[
                        variation_number
                    ],

                "batch_number":
                    batch_number,

                "selected_concept_number":
                    selected_number,

                "concept_title_en":
                    first_text(
                        selected_concept.get(
                            "title_en"
                        )
                    ),

                "concept_title_ja":
                    first_text(
                        selected_concept.get(
                            "title_ja"
                        )
                    ),

                "image_path":
                    path.as_posix(),

                "mime_type":
                    "image/png",

                "provider":
                    "OpenAI",

                "model":
                    OPENAI_IMAGE_MODEL,

                "size":
                    OPENAI_IMAGE_SIZE,

                "quality":
                    OPENAI_IMAGE_QUALITY,

                "sha256":
                    sha256_file(
                        path
                    ),

                "generation_prompt":
                    chosen_prompt,
            }
        )

    # --------------------------------------------------------
    # Safety validation:
    # exactly FIVE metadata entries must exist.
    # --------------------------------------------------------

    if (
        len(previews)
        != REAL_IMAGE_COUNT
    ):

        raise RuntimeError(
            "Expected exactly "
            f"{REAL_IMAGE_COUNT} "
            "generated previews, "
            f"got {len(previews)}."
        )

    # --------------------------------------------------------
    # Safety validation:
    # all five image hashes must be different.
    # --------------------------------------------------------

    unique_hashes = {
        preview["sha256"]
        for preview in previews
    }

    if (
        len(unique_hashes)
        != REAL_IMAGE_COUNT
    ):

        raise RuntimeError(
            "Generated preview batch "
            "contains duplicate image files."
        )

    # --------------------------------------------------------
    # Update design state
    # --------------------------------------------------------

    package[
        "design_previews"
    ] = previews

    package[
        "image_provider"
    ] = "OpenAI"

    package[
        "image_model"
    ] = OPENAI_IMAGE_MODEL

    package[
        "image_size"
    ] = OPENAI_IMAGE_SIZE

    package[
        "image_quality"
    ] = OPENAI_IMAGE_QUALITY

    package[
        "preview_batch_number"
    ] = batch_number

    package[
        "preview_batch_path"
    ] = out_dir.as_posix()

    package[
        "preview_candidate_count"
    ] = REAL_IMAGE_COUNT

    package[
        "preview_generation_rule"
    ] = (
        "Exactly five visibly distinct "
        "execution-level variations "
        "generated from the one locked "
        "human-selected concept."
    )

    package[
        "state"
    ] = "DESIGN_PREVIEWS_READY"

    OPTIONS_PATH.write_text(
        json.dumps(
            package,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # --------------------------------------------------------
    # Log
    # --------------------------------------------------------

    print()

    print(
        "Exactly 5 real preview images "
        "created from ONE selected concept."
    )

    print(
        "All five were generated using "
        "separate OpenAI image requests."
    )

    print(
        "All five passed SHA-256 "
        "duplicate-file validation."
    )

    print(
        "All five use explicit distinct "
        "variation recipes."
    )

    print(
        "Selected concept remains LOCKED: "
        f"{selected_number}"
    )

    print(
        f"PREVIEW BATCH: "
        f"{batch_number}"
    )

    print(
        f"PREVIEW PATH: "
        f"{out_dir}"
    )

    print(
        "LANGUAGE: ENGLISH-FIRST"
    )

    print(
        "STATE: DESIGN_PREVIEWS_READY"
    )

    return 0


if __name__ == "__main__":

    raise SystemExit(
        main()
    )
