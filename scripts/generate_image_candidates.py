#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI

CONCEPTS_PATH = Path("automation_state/image_concepts.json")
OUTPUT_STATE_PATH = Path("automation_state/image_candidates.json")
OUTPUT_DIR = Path("automation_state/image_candidates")

IMAGE_MODEL = (os.getenv("OPENAI_IMAGE_MODEL") or "").strip() or "gpt-image-2"
IMAGE_SIZE = (os.getenv("OPENAI_IMAGE_SIZE") or "").strip() or "1536x1024"
IMAGE_QUALITY = (os.getenv("OPENAI_IMAGE_QUALITY") or "").strip() or "medium"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Required file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return data


def validate_concepts(data: dict[str, Any]) -> list[dict[str, Any]]:
    state = str(data.get("state", "")).strip().upper()
    if state != "IMAGE_CONCEPT_REVIEW":
        raise ValueError(
            f"Expected IMAGE_CONCEPT_REVIEW state, got {state!r}."
        )

    concepts = data.get("concepts")
    if not isinstance(concepts, list) or len(concepts) != 5:
        raise ValueError("Exactly five image concepts are required.")

    normalized: list[dict[str, Any]] = []
    for i, concept in enumerate(concepts, start=1):
        if not isinstance(concept, dict):
            raise ValueError(f"Concept {i} must be an object.")
        c = dict(concept)
        c["number"] = i
        normalized.append(c)

    return normalized


def compact_story(data: dict[str, Any]) -> dict[str, Any]:
    story = data.get("story")
    return story if isinstance(story, dict) else {}


def build_prompt(story: dict[str, Any], concept: dict[str, Any]) -> str:
    title = str(story.get("title") or story.get("title_ja") or "").strip()
    source = str(story.get("source") or "").strip()
    reason = str(story.get("reason") or story.get("recommended_reason") or "").strip()

    concept_title = str(
        concept.get("title_en") or concept.get("title_ja") or f"Concept {concept['number']}"
    ).strip()
    concept_en = str(
        concept.get("concept_en") or concept.get("concept_ja") or ""
    ).strip()
    composition_en = str(
        concept.get("composition_en") or concept.get("composition_ja") or ""
    ).strip()
    production_prompt = str(concept.get("generation_prompt_en") or "").strip()

    return f"""
Create one polished editorial hero image for The Daily Duck.

APPROVED STORY
Title: {title}
Source: {source}
Story meaning: {reason}

SELECTED VISUAL CONCEPT #{concept['number']}: {concept_title}
Concept: {concept_en}
Composition: {composition_en}
Production direction: {production_prompt}

PERMANENT THE DAILY DUCK MASCOT
- a recognizable cheerful yellow duck
- orange beak
- large dark glossy eyes
- a small feather tuft
- friendly, warm expression
- consistent proportions and identity across Daily Duck images

VISUAL STYLE
- premium, charming, modern editorial illustration / soft 3D illustration
- simple rather than overly vintage
- warm, emotionally uplifting
- clean composition with strong single focal point
- suitable as both a website hero image and an X post image
- landscape composition
- rich but natural lighting
- polished enough for publication

IMPORTANT
- No headline, captions, numbers, logos, watermarks, labels, UI, or readable text in the image.
- Do not invent unsupported factual claims.
- Story-specific dog, clothing, props, setting, or historical cues are allowed only when they fit the approved story.
- Keep the duck clearly recognizable as The Daily Duck mascot.
""".strip()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    required_env("OPENAI_API_KEY")
    data = load_json(CONCEPTS_PATH)
    concepts = validate_concepts(data)
    story = compact_story(data)

    client = OpenAI()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    candidates: list[dict[str, Any]] = []

    for concept in concepts:
        number = int(concept["number"])
        prompt = build_prompt(story, concept)

        print(
            f"Generating image {number}/5 "
            f"with model={IMAGE_MODEL}, size={IMAGE_SIZE}, quality={IMAGE_QUALITY}"
        )

        result = client.images.generate(
            model=IMAGE_MODEL,
            prompt=prompt,
            size=IMAGE_SIZE,
            quality=IMAGE_QUALITY,
            n=1,
        )

        if not result.data or not result.data[0].b64_json:
            raise RuntimeError(f"OpenAI returned no image data for candidate {number}.")

        image_bytes = base64.b64decode(result.data[0].b64_json)
        image_path = OUTPUT_DIR / f"candidate_{number}.png"
        image_path.write_bytes(image_bytes)

        candidates.append(
            {
                "number": number,
                "concept_title_ja": str(concept.get("title_ja", "")).strip(),
                "concept_title_en": str(concept.get("title_en", "")).strip(),
                "concept_ja": str(concept.get("concept_ja", "")).strip(),
                "concept_en": str(concept.get("concept_en", "")).strip(),
                "image_path": str(image_path),
                "generation_prompt": prompt,
                "model": IMAGE_MODEL,
                "size": IMAGE_SIZE,
                "quality": IMAGE_QUALITY,
                "sha256": sha256_file(image_path),
            }
        )

    result_state = {
        "state": "IMAGE_CANDIDATES_READY",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_state": "IMAGE_CONCEPT_REVIEW",
        "source_path": str(CONCEPTS_PATH),
        "story": story,
        "candidates": candidates,
        "selection_rule": "Reply with exactly one digit: 1, 2, 3, 4, or 5.",
        "next_state_after_valid_selection": "READY_TO_PUBLISH",
    }

    OUTPUT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(result_state, f, ensure_ascii=False, indent=2)

    print("STATE: IMAGE_CANDIDATES_READY")
    print("Generated exactly 5 real image candidates.")
    print(f"Saved state: {OUTPUT_STATE_PATH}")


if __name__ == "__main__":
    main()
