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


def build_prompt(
    package: dict[str, Any],
    concept: dict[str, Any],
    variation_number: int,
    batch_number: int,
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
            "Selected concept is missing concept_en."
        )

    if not generation_prompt:
        raise ValueError(
            "Selected concept is missing generation_prompt_en."
        )

    return f"""
Create ONE polished, publishable hero-image candidate for The Daily Duck.

LANGUAGE POLICY
All image-generation direction is canonical English.
Do not add Japanese or any other readable text to the image.

APPROVED STORY — LOCKED
Headline: {headline}
Summary: {summary}
Source: {source}

HUMAN-SELECTED IMAGE CONCEPT — LOCKED
Concept title: {concept_title}
Concept: {concept_text}
Composition: {composition}
Production direction: {generation_prompt}

THIS IS:
- real-image batch {batch_number}
- variation {variation_number} of 5

CRITICAL CONCEPT-LOCK RULE

All five images in this batch MUST be executions of the SAME
human-selected concept above.

Do NOT:
- switch to another concept
- reinterpret the visual concept
- replace the central setting
- change the visual metaphor
- change the story
- add unsupported facts

You MAY vary only execution-level details such as:
- precise camera distance
- subtle camera angle
- duck pose
- facial expression within the same mood
- small prop placement
- crop
- spacing
- depth of field
- lighting nuance

Every output must still be immediately recognizable as the SAME concept.

THE DAILY DUCK MASCOT
- cheerful recognizable yellow duck
- orange beak
- large dark glossy eyes
- small feather tuft
- warm friendly expression
- consistent mascot identity and proportions

STYLE
- premium modern editorial illustration / soft 3D or photorealistic composite as appropriate
- cheerful
- clean
- polished
- simple rather than overly vintage
- landscape composition
- strong focal point
- suitable as a website hero image
- publication quality

DO NOT INCLUDE
- headline
- captions
- readable text
- numbers
- logos
- UI
- watermarks
- final X-card header/footer

FACTUAL RULE
Do not invent factual, scientific, geographic, organizational,
or biographical details beyond what is supported by the approved story.

This output is one of five final-image candidates.
""".strip()


def generate_one(
    client: OpenAI,
    prompt: str,
    path: Path,
    number: int,
) -> None:

    print(
        f"Generating real image {number}/5: "
        f"{OPENAI_IMAGE_MODEL}, "
        f"{OPENAI_IMAGE_SIZE}, "
        f"quality={OPENAI_IMAGE_QUALITY}"
    )

    result = client.images.generate(
        model=OPENAI_IMAGE_MODEL,
        prompt=prompt,
        n=1,
        size=OPENAI_IMAGE_SIZE,
        quality=OPENAI_IMAGE_QUALITY,
        output_format="png",
    )

    if (
        not result.data
        or not result.data[0].b64_json
    ):
        raise RuntimeError(
            "OpenAI returned no image "
            f"for candidate {number}."
        )

    path.write_bytes(
        base64.b64decode(
            result.data[0].b64_json
        )
    )


def main() -> int:

    if not OPTIONS_PATH.exists():
        raise FileNotFoundError(
            f"Missing required file: {OPTIONS_PATH}"
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

    state = first_text(
        package.get(
            "state"
        )
    ).upper()

    if state != "APPROVED_IMAGE_CONCEPT":
        raise RuntimeError(
            "Expected APPROVED_IMAGE_CONCEPT, "
            f"got {state!r}"
        )

    selected_concept = package.get(
        "selected_image_concept"
    )

    if not isinstance(
        selected_concept,
        dict,
    ):
        raise ValueError(
            "selected_image_concept is missing."
        )

    selected_number = int(
        package.get(
            "selected_image_concept_number",
            0,
        )
    )

    if selected_number not in (
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

    issue_date = first_text(
        package.get(
            "issue_date"
        )
    )

    if not issue_date:
        raise ValueError(
            "issue_date is missing."
        )

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

    client = OpenAI(
        api_key=os.environ[
            "OPENAI_API_KEY"
        ]
    )

    previews: list[
        dict[str, Any]
    ] = []

    for variation_number in range(
        1,
        6,
    ):

        prompt = build_prompt(
            package=package,
            concept=selected_concept,
            variation_number=variation_number,
            batch_number=batch_number,
        )

        path = (
            out_dir
            / f"preview_{variation_number}.png"
        )

        generate_one(
            client=client,
            prompt=prompt,
            path=path,
            number=variation_number,
        )

        previews.append(
            {
                "number":
                    variation_number,

                "variation_number":
                    variation_number,

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
                    prompt,
            }
        )

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
        "preview_generation_rule"
    ] = (
        "Exactly five real image variations "
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

    print(
        "Exactly 5 real preview images created "
        "from ONE selected concept."
    )

    print(
        "Selected concept remains LOCKED: "
        f"{selected_number}"
    )

    print(
        f"PREVIEW BATCH: {batch_number}"
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
