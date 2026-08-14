#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google import genai
from model_config import IMAGE_MODEL, IMAGE_ASPECT_RATIO, IMAGE_SIZE

STATE_DIR = Path("automation_state")
SELECTED_PATH = STATE_DIR / "selected_design.json"
APPROVED_PATH = STATE_DIR / "approved_story.json"
OUTPUT_PATH = STATE_DIR / "image_candidates.json"
REGEN_PATH = STATE_DIR / "image_regeneration_request.json"
OUTPUT_DIR = Path("automation_images") / "candidates"


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


def load_selected() -> dict[str, Any]:
    if SELECTED_PATH.exists():
        data = json.loads(SELECTED_PATH.read_text(encoding="utf-8"))
        if data.get("state") != "DESIGN_SELECTED":
            raise RuntimeError("selected_design.json is not DESIGN_SELECTED.")
        return data

    approved = json.loads(APPROVED_PATH.read_text(encoding="utf-8"))
    concept = approved.get("selected_image_concept")
    title = first_text(approved.get("selected_title"))
    if not isinstance(concept, dict) or not title:
        raise RuntimeError("Complete the Design Selection step first.")
    return {
        "state": "DESIGN_SELECTED",
        "issue_date": first_text(approved.get("issue_date"), approved.get("date")),
        "selected_image_concept_number": approved.get("selected_image_concept_number"),
        "selected_image_concept": concept,
        "selected_title_number": approved.get("selected_title_number"),
        "selected_title": title,
        "approved_story": approved,
    }


def prompt_for(data: dict[str, Any], number: int, batch: int) -> str:
    approved = data.get("approved_story") if isinstance(data.get("approved_story"), dict) else {}
    story = approved.get("recommended_story") if isinstance(approved.get("recommended_story"), dict) else {}
    concept = data["selected_image_concept"]

    return f"""
Create one polished hero image for The Daily Duck.

This is execution {number} of 5 in batch {batch}.
ALL FIVE images must use the SAME already-approved concept and approved title.
Only vary pose, camera angle, framing, lighting, background details, and composition.

NEWS
Headline: {first_text(story.get("title"), story.get("title_ja"))}
Source: {first_text(story.get("source"), approved.get("source"))}
Meaning: {first_text(approved.get("en_copy"), approved.get("jp_copy"), story.get("reason"))}

APPROVED DAILY DUCK TITLE
{first_text(data.get("selected_title"))}

APPROVED IMAGE CONCEPT
Name: {first_text(concept.get("title_en"), concept.get("title_ja"))}
Concept: {first_text(concept.get("concept_en"), concept.get("concept_ja"))}
Visual direction: {first_text(concept.get("visual_direction"))}

PERMANENT MASCOT
- cheerful recognizable yellow duck
- orange beak
- large dark glossy eyes
- small feather tuft
- friendly warm expression
- consistent Daily Duck identity

STYLE
- premium modern editorial hero image
- cheerful and visually clean
- simple rather than overly vintage
- landscape composition
- strong focal point
- NO headline, captions, readable text, logos, UI, or watermarks inside the hero image
""".strip()


def main() -> int:
    data = load_selected()
    issue_date = first_text(data.get("issue_date"))

    old_batch = 0
    if OUTPUT_PATH.exists():
        try:
            old_batch = int(json.loads(OUTPUT_PATH.read_text(encoding="utf-8")).get("batch", 0))
        except Exception:
            old_batch = 0
    batch = max(1, old_batch + 1)

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    batch_dir = OUTPUT_DIR / issue_date / f"batch-{batch}"
    batch_dir.mkdir(parents=True, exist_ok=True)

    candidates = []
    for number in range(1, 6):
        prompt = prompt_for(data, number, batch)
        print(f"Generating {number}/5: {IMAGE_MODEL} {IMAGE_ASPECT_RATIO} {IMAGE_SIZE}")
        interaction = client.interactions.create(
            model=IMAGE_MODEL,
            input=prompt,
            response_format={
                "type": "image",
                "mime_type": "image/png",
                "aspect_ratio": IMAGE_ASPECT_RATIO,
                "image_size": IMAGE_SIZE,
            },
        )
        image = interaction.output_image
        if image is None or not image.data:
            raise RuntimeError(f"No image returned for candidate {number}.")

        path = batch_dir / f"candidate_{number}.png"
        path.write_bytes(base64.b64decode(image.data))

        concept = data["selected_image_concept"]
        candidates.append({
            "number": number,
            "concept_title_ja": first_text(concept.get("title_ja")),
            "concept_title_en": first_text(concept.get("title_en")),
            "concept_ja": first_text(concept.get("concept_ja")),
            "concept_en": first_text(concept.get("concept_en")),
            "selected_title": first_text(data.get("selected_title")),
            "image_path": path.as_posix(),
            "generation_prompt": prompt,
            "model": IMAGE_MODEL,
            "aspect_ratio": IMAGE_ASPECT_RATIO,
            "image_size": IMAGE_SIZE,
            "sha256": sha256_file(path),
        })

    approved = data.get("approved_story") if isinstance(data.get("approved_story"), dict) else {}
    story = approved.get("recommended_story") if isinstance(approved.get("recommended_story"), dict) else {}
    result = {
        "state": "IMAGE_CANDIDATES_READY",
        "issue_date": issue_date,
        "batch": batch,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "story": story,
        "selected_image_concept_number": data.get("selected_image_concept_number"),
        "selected_image_concept": data.get("selected_image_concept"),
        "selected_title_number": data.get("selected_title_number"),
        "selected_title": data.get("selected_title"),
        "candidates": candidates,
        "selection_rule": "Reply 1-5, or NEXT 5 for five more using the same approved concept/title.",
    }
    OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if REGEN_PATH.exists():
        REGEN_PATH.unlink()

    print(f"IMAGE MODEL: {IMAGE_MODEL}")
    print(f"BATCH: {batch}")
    print("STATE: IMAGE_CANDIDATES_READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
