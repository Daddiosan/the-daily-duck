#!/usr/bin/env python3
"""
The Daily Duck - Phase 2 image candidate generator

Precondition:
- automation_state/approved_story.json
- state == APPROVED_STORY
- selected_image_concept is present

Generates EXACTLY five images for the selected concept.

Cost safety:
Image generation is paid on the Gemini API at the time this file was prepared.
This script refuses to call the image API unless:
  ENABLE_PAID_IMAGE_GENERATION=true

Recommended low-cost model:
  gemini-3.1-flash-lite-image

NEXT 5 handling:
- If automation_state/image_regeneration_request.json exists and is valid,
  generation batch increments and five NEW prompts are created.
- The selected Gate A concept does not change.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google import genai

STATE_DIR = Path("automation_state")
APPROVED_PATH = STATE_DIR / "approved_story.json"
CANDIDATES_PATH = STATE_DIR / "image_candidates.json"
REGEN_REQUEST_PATH = STATE_DIR / "image_regeneration_request.json"
IMAGE_ROOT = Path("automation_images")

IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-lite-image")
TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash")
ASPECT_RATIO = os.getenv("DAILY_DUCK_IMAGE_ASPECT_RATIO", "1:1")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def paid_generation_enabled() -> bool:
    return os.getenv("ENABLE_PAID_IMAGE_GENERATION", "").strip().lower() == "true"


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Required file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return data


def issue_date(approved: dict[str, Any]) -> str:
    return str(
        approved.get("date")
        or approved.get("issue_date")
        or datetime.now().date().isoformat()
    ).strip()


def story_summary(approved: dict[str, Any]) -> str:
    rec = approved.get("recommended_story")
    if isinstance(rec, dict):
        title = rec.get("title") or rec.get("headline") or ""
        summary = rec.get("summary") or rec.get("description") or ""
    else:
        title = approved.get("title") or approved.get("headline") or ""
        summary = approved.get("summary") or approved.get("jp_copy") or ""
    return f"{title}\n{summary}".strip()


def current_batch() -> int:
    if not CANDIDATES_PATH.exists():
        return 0
    try:
        data = load_json(CANDIDATES_PATH)
        return int(data.get("batch", 0))
    except Exception:
        return 0


def validate_regeneration_request(approved: dict[str, Any]) -> bool:
    if not REGEN_REQUEST_PATH.exists():
        return False
    req = load_json(REGEN_REQUEST_PATH)
    if req.get("action") != "NEXT_5":
        return False
    req_date = str(req.get("issue_date", "")).strip()
    return not req_date or req_date == issue_date(approved)


def build_prompt_variations(
    client: genai.Client,
    approved: dict[str, Any],
    batch: int,
) -> list[str]:
    concept = approved.get("selected_image_concept")
    if not isinstance(concept, dict):
        raise ValueError("selected_image_concept is missing from approved_story.json")

    concept_title = str(concept.get("title", "")).strip()
    concept_body = str(concept.get("concept", "")).strip()
    concept_visual = str(concept.get("visual_direction", "")).strip()

    prompt = f"""
You are preparing image-generation prompts for The Daily Duck.

The human editor already selected ONE concept at Gate A.
Do NOT change that concept. Create exactly five distinct visual executions of it.

This is generation batch {batch}.
If batch > 1, the previous five images were rejected. Push for fresher compositions,
poses, viewpoints, scene arrangements, and props while staying within the SAME concept.

Permanent mascot identity:
- same recognizable yellow duck
- orange beak
- large dark glossy eyes
- small feather tuft
- friendly expression
- story-specific clothing/props allowed

Visual direction:
- clean, modern editorial
- not heavy vintage
- polished and charming
- readable at social-media size
- no long text, logos, watermarks, fake headlines, or UI
- no fabricated factual details
- image should work as the same canonical image for website and X
- square-friendly composition

APPROVED STORY:
{story_summary(approved)}

SELECTED CONCEPT:
Title: {concept_title}
Concept: {concept_body}
Direction: {concept_visual}

Return ONLY valid JSON:
[
  {{"number":1,"prompt":"complete image prompt","alt_ja":"...","alt_en":"..."}},
  {{"number":2,"prompt":"complete image prompt","alt_ja":"...","alt_en":"..."}},
  {{"number":3,"prompt":"complete image prompt","alt_ja":"...","alt_en":"..."}},
  {{"number":4,"prompt":"complete image prompt","alt_ja":"...","alt_en":"..."}},
  {{"number":5,"prompt":"complete image prompt","alt_ja":"...","alt_en":"..."}}
]
""".strip()

    response = client.models.generate_content(model=TEXT_MODEL, contents=prompt)
    text = getattr(response, "text", "") or ""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        raise ValueError("Could not parse five prompt variations from Gemini.")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, list) or len(data) != 5:
        raise ValueError("Gemini must return exactly five prompt variations.")

    prompts = []
    for idx, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError("Prompt variation is not an object.")
        item["number"] = idx
        if not str(item.get("prompt", "")).strip():
            raise ValueError(f"Prompt {idx} is empty.")
        prompts.append(item)
    return prompts


def generate_one_image(client: genai.Client, prompt: str, output_path: Path) -> None:
    interaction = client.interactions.create(
        model=IMAGE_MODEL,
        input=prompt,
        response_format={
            "type": "image",
            "mime_type": "image/png",
            "aspect_ratio": ASPECT_RATIO,
        },
    )
    output_image = getattr(interaction, "output_image", None)
    data = getattr(output_image, "data", None) if output_image is not None else None
    if not data:
        raise RuntimeError("Gemini returned no image data.")
    output_path.write_bytes(base64.b64decode(data))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    approved = load_json(APPROVED_PATH)
    if approved.get("state") != "APPROVED_STORY":
        raise RuntimeError("Image generation is allowed only from state APPROVED_STORY.")
    if not approved.get("selected_image_concept"):
        raise RuntimeError("No Gate A selected_image_concept is stored.")

    regen = validate_regeneration_request(approved)
    previous_batch = current_batch()
    batch = previous_batch + 1 if (regen or previous_batch == 0) else previous_batch

    # Do not silently regenerate the same batch.
    if previous_batch > 0 and not regen:
        print(f"Image candidates already exist for batch {previous_batch}. No regeneration request.")
        print("STATE: WAITING_IMAGE_SELECTION")
        return 0

    if not paid_generation_enabled():
        raise RuntimeError(
            "PAID IMAGE GENERATION SAFETY LOCK: "
            "Set ENABLE_PAID_IMAGE_GENERATION=true only after accepting Gemini image-generation charges."
        )

    api_key = required_env("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    prompts = build_prompt_variations(client, approved, batch)

    date = issue_date(approved)
    batch_dir = IMAGE_ROOT / date / f"batch-{batch}"
    if batch_dir.exists():
        shutil.rmtree(batch_dir)
    batch_dir.mkdir(parents=True, exist_ok=True)

    candidates = []
    for idx, item in enumerate(prompts, start=1):
        path = batch_dir / f"candidate-{idx}.png"
        print(f"Generating candidate {idx}/5 for batch {batch}...")
        generate_one_image(client, str(item["prompt"]), path)
        candidates.append(
            {
                "number": idx,
                "batch": batch,
                "image_path": path.as_posix(),
                "prompt": str(item["prompt"]),
                "alt_ja": str(item.get("alt_ja", "")).strip(),
                "alt_en": str(item.get("alt_en", "")).strip(),
                "sha256": sha256(path),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    if len(candidates) != 5:
        raise RuntimeError("Internal error: exactly five candidates were not generated.")

    metadata = {
        "state": "WAITING_IMAGE_SELECTION",
        "issue_date": date,
        "batch": batch,
        "selected_image_concept_number": approved.get("selected_image_concept_number"),
        "selected_image_concept": approved.get("selected_image_concept"),
        "image_model": IMAGE_MODEL,
        "aspect_ratio": ASPECT_RATIO,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidates": candidates,
        "valid_replies": ["1", "2", "3", "4", "5", "NEXT 5"],
    }

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CANDIDATES_PATH.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if REGEN_REQUEST_PATH.exists():
        REGEN_REQUEST_PATH.unlink()

    print(f"Generated exactly 5 candidates. Batch: {batch}")
    print(f"Saved metadata to {CANDIDATES_PATH}")
    print("STATE: WAITING_IMAGE_SELECTION")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
