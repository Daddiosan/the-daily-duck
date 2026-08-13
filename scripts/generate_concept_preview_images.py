#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI


CONCEPTS_PATH = Path("automation_state/image_concepts.json")
PREVIEW_DIR = Path("automation_state/concept_previews")

IMAGE_MODEL = (
    os.getenv("OPENAI_IMAGE_MODEL", "").strip()
    or "gpt-image-2"
)

IMAGE_SIZE = (
    os.getenv("OPENAI_PREVIEW_IMAGE_SIZE", "").strip()
    or "1024x1024"
)

IMAGE_QUALITY = (
    os.getenv("OPENAI_PREVIEW_IMAGE_QUALITY", "").strip()
    or "low"
)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Required file not found: {path}"
        )

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
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


def validate_package(
    data: dict[str, Any],
) -> list[dict[str, Any]]:
    state = str(
        data.get("state", "")
    ).strip().upper()

    if state not in (
        "IMAGE_CONCEPT_PREVIEW_GENERATION",
        "IMAGE_CONCEPT_REVIEW",
    ):
        raise ValueError(
            "Expected IMAGE_CONCEPT_PREVIEW_GENERATION "
            "or IMAGE_CONCEPT_REVIEW, "
            f"got {state!r}."
        )

    concepts = data.get("concepts")

    if (
        not isinstance(concepts, list)
        or len(concepts) != 5
    ):
        raise ValueError(
            "Exactly five image concepts are required."
        )

    for index, concept in enumerate(
        concepts,
        start=1,
    ):
        if not isinstance(concept, dict):
            raise ValueError(
                f"Concept {index} must be an object."
            )

        if int(
            concept.get("number", 0)
        ) != index:
            raise ValueError(
                "Concept numbering must be exactly 1-5."
            )

        prompt = str(
            concept.get(
                "generation_prompt_en",
                "",
            )
        ).strip()

        if not prompt:
            raise ValueError(
                f"Concept {index} has no generation_prompt_en."
            )

    return concepts


def build_preview_prompt(
    concept: dict[str, Any],
) -> str:
    core_prompt = str(
        concept["generation_prompt_en"]
    ).strip()

    return f"""
Create a concept-preview image for The Daily Duck.

CORE VISUAL CONCEPT:
{core_prompt}

MANDATORY DAILY DUCK CHARACTER:
- one recognizable friendly yellow duck mascot
- orange beak
- large dark glossy eyes
- small feather tuft
- warm, approachable expression
- simple, clean, modern editorial illustration
- charming but not childish
- not overly vintage

PREVIEW PURPOSE:
This image is one of five DIFFERENT concept previews.
It should clearly communicate this specific visual direction so a human
editor can compare it against four other concepts.

COMPOSITION:
- strong clear focal point
- suitable for website hero image
- suitable for X social post
- square composition
- leave reasonable breathing room around the duck
- no important content cropped at the edges

DO NOT INCLUDE:
- text
- captions
- logos
- watermarks
- UI elements
- speech bubbles
- visible brand names

Maintain the concept exactly.
Do not introduce unrelated story elements.
""".strip()


def generate_one(
    client: OpenAI,
    prompt: str,
    output_path: Path,
) -> None:
    result = client.images.generate(
        model=IMAGE_MODEL,
        prompt=prompt,
        size=IMAGE_SIZE,
        quality=IMAGE_QUALITY,
        n=1,
    )

    if not result.data:
        raise RuntimeError(
            "OpenAI returned no image data."
        )

    image_base64 = result.data[0].b64_json

    if not image_base64:
        raise RuntimeError(
            "OpenAI image response contained no b64_json."
        )

    image_bytes = base64.b64decode(
        image_base64
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path.write_bytes(
        image_bytes
    )


def main() -> None:
    required_env("OPENAI_API_KEY")

    data = load_json(
        CONCEPTS_PATH
    )

    concepts = validate_package(
        data
    )

    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"]
    )

    PREVIEW_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Remove old previews to prevent stale files
    # being mistaken for today's five concepts.
    for old_file in PREVIEW_DIR.glob(
        "concept_*.png"
    ):
        old_file.unlink()

    for concept in concepts:
        number = int(
            concept["number"]
        )

        output_path = (
            PREVIEW_DIR
            / f"concept_{number}.png"
        )

        prompt = build_preview_prompt(
            concept
        )

        print(
            f"Generating concept preview {number}/5..."
        )

        generate_one(
            client,
            prompt,
            output_path,
        )

        concept[
            "preview_status"
        ] = "GENERATED"

        concept[
            "preview_image_path"
        ] = output_path.as_posix()

        concept[
            "preview_generated_at"
        ] = datetime.now(
            timezone.utc
        ).isoformat()

        concept[
            "preview_model"
        ] = IMAGE_MODEL

        concept[
            "preview_quality"
        ] = IMAGE_QUALITY

        concept[
            "preview_size"
        ] = IMAGE_SIZE

        print(
            f"Saved: {output_path}"
        )

    data["state"] = (
        "IMAGE_CONCEPT_REVIEW"
    )

    data[
        "preview_generation_completed_at"
    ] = datetime.now(
        timezone.utc
    ).isoformat()

    data[
        "preview_image_count"
    ] = 5

    save_json(
        CONCEPTS_PATH,
        data,
    )

    print(
        "Generated exactly five concept preview images."
    )

    print(
        "STATE: IMAGE_CONCEPT_REVIEW"
    )


if __name__ == "__main__":
    main()
