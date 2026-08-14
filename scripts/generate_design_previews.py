#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from google import genai
from model_config import IMAGE_MODEL, IMAGE_ASPECT_RATIO, IMAGE_SIZE

STATE_DIR = Path("automation_state")
OPTIONS_PATH = STATE_DIR / "design_options.json"
PREVIEW_DIR = Path("automation_images") / "design_previews"


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
        raise ValueError("design_options.json is missing approved_story.")

    story = approved.get("recommended_story")
    if not isinstance(story, dict):
        story = {}

    story_title = first_text(
        story.get("title"),
        story.get("title_ja"),
        approved.get("en_copy"),
    )
    story_meaning = first_text(
        approved.get("en_copy"),
        approved.get("jp_copy"),
        story.get("reason"),
    )
    source = first_text(story.get("source"), approved.get("source"))

    concept_name = first_text(concept.get("title_en"), concept.get("title_ja"))
    concept_body = first_text(concept.get("concept_en"), concept.get("concept_ja"))
    visual = first_text(concept.get("visual_direction"))

    return f"""
Create ONE polished real preview image for The Daily Duck.

This preview image is not a rough sketch. It is a publishable-quality hero-image candidate.
If the editor selects it in the approval email, THIS EXACT IMAGE will become the final website hero image.

APPROVED STORY
Headline: {story_title}
Source: {source}
Meaning: {story_meaning}

IMAGE CONCEPT
Name: {concept_name}
Concept: {concept_body}
Visual direction: {visual}

THE DAILY DUCK MASCOT
- cheerful recognizable yellow duck
- orange beak
- large dark glossy eyes
- small feather tuft
- friendly warm expression
- consistent identity and proportions

STYLE
- premium modern editorial hero image
- cheerful, warm, polished
- simple rather than overly vintage
- landscape composition, 16:9
- strong single focal point
- suitable for the website hero
- do not add the final social-card header/footer
- NO headline, captions, readable text, logos, UI, or watermarks inside the hero image

FACTUAL RULE
Do not invent scientific or factual details that are not necessary to visualize the supplied story/concept.
""".strip()


def main() -> int:
    if not OPTIONS_PATH.exists():
        raise FileNotFoundError(f"Missing {OPTIONS_PATH}")

    package = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    if package.get("state") not in ("WAITING_DESIGN_SELECTION", "DESIGN_PREVIEWS_READY"):
        raise RuntimeError(f"Unexpected state: {package.get('state')!r}")

    concepts = package.get("image_concepts")
    if not isinstance(concepts, list) or len(concepts) != 3:
        raise ValueError("Exactly 3 image concepts are required.")

    issue_date = first_text(package.get("issue_date"))
    issue_dir = PREVIEW_DIR / issue_date
    issue_dir.mkdir(parents=True, exist_ok=True)

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    previews = []

    for concept in concepts:
        number = int(concept["number"])
        prompt = build_prompt(package, concept)

        print(
            f"Generating preview {number}/3 using "
            f"{IMAGE_MODEL}, {IMAGE_ASPECT_RATIO}, {IMAGE_SIZE}"
        )

        # Gemini Interactions image output currently accepts JPEG for this model.
        interaction = client.interactions.create(
            model=IMAGE_MODEL,
            input=prompt,
            response_format={
                "type": "image",
                "mime_type": "image/jpeg",
                "aspect_ratio": IMAGE_ASPECT_RATIO,
                "image_size": IMAGE_SIZE,
            },
        )

        image = interaction.output_image
        if image is None or not image.data:
            raise RuntimeError(f"Gemini returned no image for preview {number}.")

        path = issue_dir / f"preview_{number}.jpg"
        path.write_bytes(base64.b64decode(image.data))

        previews.append({
            "number": number,
            "image_path": path.as_posix(),
            "mime_type": "image/jpeg",
            "sha256": sha256_file(path),
            "model": IMAGE_MODEL,
            "aspect_ratio": IMAGE_ASPECT_RATIO,
            "image_size": IMAGE_SIZE,
            "generation_prompt": prompt,
        })

    package["design_previews"] = previews
    package["state"] = "DESIGN_PREVIEWS_READY"
    OPTIONS_PATH.write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("Generated exactly 3 real JPEG preview images.")
    print("STATE: DESIGN_PREVIEWS_READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
