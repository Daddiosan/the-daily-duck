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

OPTIONS_PATH = Path("automation_state/design_options.json")
PREVIEW_ROOT = Path("automation_images/design_previews")


def first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_prompt(package: dict[str, Any], concept: dict[str, Any]) -> str:
    approved = package.get("approved_story")
    if not isinstance(approved, dict):
        raise ValueError("approved_story is missing.")

    story = approved.get("recommended_story")
    if not isinstance(story, dict):
        story = {}

    return f"""
Create one polished, publishable hero-image candidate for The Daily Duck.

APPROVED STORY
Headline: {first_text(story.get("title"), story.get("title_ja"))}
Summary: {first_text(approved.get("jp_copy"), approved.get("en_copy"), story.get("reason"))}
Source: {first_text(story.get("source"), approved.get("source"))}

IMAGE CONCEPT
Name: {first_text(concept.get("title_en"), concept.get("title_ja"))}
Concept: {first_text(concept.get("concept_en"), concept.get("concept_ja"))}
Visual direction: {first_text(concept.get("visual_direction"))}

THE DAILY DUCK MASCOT
- cheerful recognizable yellow duck
- orange beak
- large dark glossy eyes
- small feather tuft
- warm friendly expression
- consistent mascot identity

STYLE
- premium modern editorial illustration / photorealistic composite as appropriate
- cheerful, clean, polished
- simple rather than overly vintage
- landscape composition
- strong focal point
- suitable as a website hero image
- no headline, captions, readable text, logos, UI, or watermarks
- do not add the final social-card header or footer

FACTUAL RULE
Do not invent factual or scientific details beyond what is needed to visualize the supplied story.
""".strip()


def main() -> int:
    package = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    if package.get("state") != "WAITING_DESIGN_SELECTION":
        raise RuntimeError(
            f"Expected WAITING_DESIGN_SELECTION, got {package.get('state')!r}"
        )

    concepts = package.get("image_concepts")
    if not isinstance(concepts, list) or len(concepts) != 3:
        raise ValueError("Exactly 3 image concepts are required.")

    issue_date = first_text(package.get("issue_date"))
    out_dir = PREVIEW_ROOT / issue_date
    out_dir.mkdir(parents=True, exist_ok=True)

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    previews = []

    for concept in concepts:
        number = int(concept["number"])
        prompt = build_prompt(package, concept)

        print(
            f"Generating image {number}/3: "
            f"{OPENAI_IMAGE_MODEL}, {OPENAI_IMAGE_SIZE}, "
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

        if not result.data or not result.data[0].b64_json:
            raise RuntimeError(f"OpenAI returned no image for candidate {number}.")

        path = out_dir / f"preview_{number}.png"
        path.write_bytes(base64.b64decode(result.data[0].b64_json))

        previews.append({
            "number": number,
            "image_path": path.as_posix(),
            "mime_type": "image/png",
            "provider": "OpenAI",
            "model": OPENAI_IMAGE_MODEL,
            "size": OPENAI_IMAGE_SIZE,
            "quality": OPENAI_IMAGE_QUALITY,
            "sha256": sha256_file(path),
        })

    package["design_previews"] = previews
    package["image_provider"] = "OpenAI"
    package["image_model"] = OPENAI_IMAGE_MODEL
    package["state"] = "DESIGN_PREVIEWS_READY"
    OPTIONS_PATH.write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("Exactly 3 real preview images created.")
    print("STATE: DESIGN_PREVIEWS_READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
