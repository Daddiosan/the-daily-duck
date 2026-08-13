#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import random
import sys
import time

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI


# ============================================================
# Paths
# ============================================================

CONCEPTS_PATH = Path(
    "automation_state/image_concepts.json"
)

WEB_DIR = Path(
    "automation_state/concept_assets/web"
)


# ============================================================
# Image settings
# ============================================================

IMAGE_MODEL = (
    os.getenv("OPENAI_IMAGE_MODEL") or ""
).strip() or "gpt-image-2"

IMAGE_SIZE = (
    os.getenv("OPENAI_WEB_IMAGE_SIZE") or ""
).strip() or "1536x1024"

IMAGE_QUALITY = (
    os.getenv("OPENAI_WEB_IMAGE_QUALITY") or ""
).strip() or "low"

CONCEPT_COUNT = 3


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
# JSON helpers
# ============================================================

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
# Validate concepts
# ============================================================

def validate_concepts(
    data: dict[str, Any],
) -> list[dict[str, Any]]:

    state = str(
        data.get("state", "")
    ).strip().upper()

    if state != "IMAGE_CONCEPT_ASSET_GENERATION":
        raise ValueError(
            "Expected IMAGE_CONCEPT_ASSET_GENERATION, "
            f"got {state!r}."
        )

    concepts = data.get(
        "concepts"
    )

    if (
        not isinstance(concepts, list)
        or len(concepts) != CONCEPT_COUNT
    ):
        raise ValueError(
            "Exactly 3 image concepts are required."
        )

    normalized: list[
        dict[str, Any]
    ] = []

    for index, concept in enumerate(
        concepts,
        start=1,
    ):
        if not isinstance(
            concept,
            dict,
        ):
            raise ValueError(
                f"Concept {index} must be an object."
            )

        if int(
            concept.get(
                "number",
                0,
            )
        ) != index:
            raise ValueError(
                "Concept numbering must be exactly 1-3."
            )

        production_prompt = str(
            concept.get(
                "generation_prompt_en",
                "",
            )
        ).strip()

        if not production_prompt:
            raise ValueError(
                f"Concept {index} has no generation_prompt_en."
            )

        normalized.append(
            concept
        )

    return normalized


# ============================================================
# Prompt
# ============================================================

def build_prompt(
    concept: dict[str, Any],
) -> str:

    title = str(
        concept.get(
            "title_en",
            "",
        )
    ).strip()

    concept_text = str(
        concept.get(
            "concept_en",
            "",
        )
    ).strip()

    composition = str(
        concept.get(
            "composition_en",
            "",
        )
    ).strip()

    production_prompt = str(
        concept.get(
            "generation_prompt_en",
            "",
        )
    ).strip()

    return f"""
Create a polished website hero illustration for The Daily Duck.

CONCEPT TITLE:
{title}

VISUAL CONCEPT:
{concept_text}

COMPOSITION:
{composition}

PRODUCTION DIRECTION:
{production_prompt}


THE DAILY DUCK MASCOT

The image must contain the established Daily Duck mascot:

- one recognizable friendly yellow duck
- orange beak
- large glossy dark eyes
- small feather tuft
- warm approachable expression
- consistent recurring Daily Duck identity


PURPOSE

This image will be used as:

- the primary Daily Duck website hero image
- the visual source used later to build a separate X branded editorial card

The X card typography and layout will be added later by Python.
Do NOT render the X card layout inside this image.


STYLE

- clean
- modern
- premium editorial illustration
- charming
- emotionally uplifting
- simple rather than overly vintage
- polished publication quality
- strong clear focal point
- rich but natural lighting
- landscape composition
- suitable for a professional news/editorial site


COMPOSITION REQUIREMENTS

- landscape orientation
- visually balanced at 1536x1024
- preserve breathing room around the main subjects
- avoid important content at extreme edges
- main subjects should remain clearly visible if the image is slightly cropped
- create depth and visual interest without clutter


FACTUAL SAFETY

Do not invent unsupported factual claims.

Do not add unsupported:
- people
- organizations
- locations
- statistics
- quotations
- scientific details
- dates
- branded objects


DO NOT INCLUDE

- headline text
- captions
- logos
- watermarks
- UI elements
- speech bubbles
- numbers
- readable signs
- embedded typography


FINAL REQUIREMENT

Create only the hero artwork itself.
Do not create a poster, social card, magazine cover, or layout with text.
""".strip()


# ============================================================
# Retry handling
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
        "SERVER ERROR",
        "INTERNAL",
        "TIMEOUT",
        "TIMED OUT",
        "TEMPORAR",
    )

    return any(
        token in message
        for token in retry_tokens
    )


def generate_image_with_retry(
    client: OpenAI,
    prompt: str,
    concept_number: int,
    max_attempts: int = 4,
) -> bytes:

    last_exception: Exception | None = None

    for attempt in range(
        1,
        max_attempts + 1,
    ):
        try:

            print(
                f"Concept {concept_number}: "
                f"OpenAI image attempt "
                f"{attempt}/{max_attempts}"
            )

            response = client.images.generate(
                model=IMAGE_MODEL,
                prompt=prompt,
                size=IMAGE_SIZE,
                quality=IMAGE_QUALITY,
                n=1,
            )

            if not response.data:
                raise RuntimeError(
                    "OpenAI returned no image data."
                )

            image_base64 = (
                response.data[0].b64_json
            )

            if not image_base64:
                raise RuntimeError(
                    "OpenAI image response contained no b64_json."
                )

            return base64.b64decode(
                image_base64
            )

        except Exception as exc:

            last_exception = exc

            if not is_retryable_openai_error(
                exc
            ):
                print(
                    "OpenAI error is not retryable."
                )
                raise

            if attempt >= max_attempts:
                print(
                    "OpenAI image retry limit reached."
                )
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
                "Temporary OpenAI image API error detected."
            )

            print(
                str(exc)[:500]
            )

            print(
                f"Waiting {wait_seconds:.1f} seconds before retry..."
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

def main() -> int:

    api_key = required_env(
        "OPENAI_API_KEY"
    )

    data = load_json(
        CONCEPTS_PATH
    )

    concepts = validate_concepts(
        data
    )

    client = OpenAI(
        api_key=api_key
    )

    WEB_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Remove stale images from an earlier run.
    for old_file in WEB_DIR.glob(
        "concept_*_web.png"
    ):
        old_file.unlink()

    print(
        "Generating exactly 3 website hero images."
    )

    print(
        f"Model: {IMAGE_MODEL}"
    )

    print(
        f"Size: {IMAGE_SIZE}"
    )

    print(
        f"Quality: {IMAGE_QUALITY}"
    )

    for concept in concepts:

        number = int(
            concept["number"]
        )

        prompt = build_prompt(
            concept
        )

        print(
            f"Generating website image "
            f"{number}/{CONCEPT_COUNT}..."
        )

        image_bytes = (
            generate_image_with_retry(
                client=client,
                prompt=prompt,
                concept_number=number,
            )
        )

        image_path = (
            WEB_DIR
            / f"concept_{number}_web.png"
        )

        image_path.write_bytes(
            image_bytes
        )

        concept[
            "web_image_status"
        ] = "GENERATED"

        concept[
            "web_image_path"
        ] = image_path.as_posix()

        concept[
            "web_generated_at"
        ] = datetime.now(
            timezone.utc
        ).isoformat()

        concept[
            "web_model"
        ] = IMAGE_MODEL

        concept[
            "web_size"
        ] = IMAGE_SIZE

        concept[
            "web_quality"
        ] = IMAGE_QUALITY

        print(
            f"Saved: {image_path}"
        )

    data[
        "state"
    ] = "WEB_IMAGES_READY"

    data[
        "web_image_count"
    ] = CONCEPT_COUNT

    data[
        "web_images_generated_at"
    ] = datetime.now(
        timezone.utc
    ).isoformat()

    save_json(
        CONCEPTS_PATH,
        data,
    )

    print(
        "Generated exactly 3 website images."
    )

    print(
        "STATE: WEB_IMAGES_READY"
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
