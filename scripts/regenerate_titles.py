#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google import genai


OPTIONS_PATH = Path(
    "automation_state/design_options.json"
)

TEXT_MODEL = (
    os.getenv("GEMINI_TEXT_MODEL") or ""
).strip() or "gemini-3.6-flash"

TITLE_COUNT = 3

GEMINI_API_MAX_ATTEMPTS = int(
    os.getenv("GEMINI_API_MAX_ATTEMPTS", "5")
)

GEMINI_RETRY_BASE_SECONDS = float(
    os.getenv("GEMINI_RETRY_BASE_SECONDS", "10")
)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


def first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def clean_json_text(value: str) -> str:
    cleaned = value.strip()

    cleaned = re.sub(
        r"^```(?:json)?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    cleaned = re.sub(
        r"\s*```$",
        "",
        cleaned,
    )

    return cleaned.strip()


def load_package() -> dict[str, Any]:
    if not OPTIONS_PATH.exists():
        raise FileNotFoundError(
            f"Missing required file: {OPTIONS_PATH}"
        )

    data = json.loads(
        OPTIONS_PATH.read_text(
            encoding="utf-8"
        )
    )

    if not isinstance(data, dict):
        raise ValueError(
            "design_options.json must contain a JSON object."
        )

    return data


def validate_existing_design(
    package: dict[str, Any],
) -> None:
    concepts = package.get(
        "image_concepts"
    )

    previews = package.get(
        "design_previews"
    )

    if (
        not isinstance(concepts, list)
        or len(concepts) != 3
    ):
        raise ValueError(
            "Exactly 3 image concepts must already exist."
        )

    if (
        not isinstance(previews, list)
        or len(previews) != 3
    ):
        raise ValueError(
            "Exactly 3 generated images must already exist. "
            "This script intentionally does NOT generate images."
        )

    for index, preview in enumerate(
        previews,
        start=1,
    ):
        if not isinstance(preview, dict):
            raise ValueError(
                f"Preview {index} must be an object."
            )

        image_path = Path(
            first_text(
                preview.get("image_path")
            )
        )

        if not image_path.exists():
            raise FileNotFoundError(
                f"Existing preview image missing: {image_path}"
            )


def approved_story_from(
    package: dict[str, Any],
) -> dict[str, Any]:
    compact = package.get(
        "approved_story_compact"
    )

    if isinstance(compact, dict):
        return compact

    approved = package.get(
        "approved_story"
    )

    if not isinstance(approved, dict):
        raise ValueError(
            "Approved story data is missing."
        )

    for key in (
        "approved_story",
        "selected_story",
        "gate_a_approved_story",
        "story",
        "recommended_story",
    ):
        value = approved.get(key)

        if isinstance(value, dict):
            return value

    return approved


def is_retryable(
    exc: Exception,
) -> bool:
    text = str(exc).lower()

    return any(
        marker in text
        for marker in (
            "429",
            "500",
            "502",
            "503",
            "504",
            "resource_exhausted",
            "internal",
            "unavailable",
            "deadline_exceeded",
            "high demand",
            "temporarily unavailable",
            "service unavailable",
            "timeout",
            "timed out",
        )
    )


def call_gemini(
    client: genai.Client,
    prompt: str,
):
    last_error: Exception | None = None

    for attempt in range(
        1,
        GEMINI_API_MAX_ATTEMPTS + 1,
    ):
        try:
            print(
                "Gemini title request "
                f"{attempt}/{GEMINI_API_MAX_ATTEMPTS}..."
            )

            return client.models.generate_content(
                model=TEXT_MODEL,
                contents=prompt,
            )

        except Exception as exc:
            last_error = exc

            if (
                not is_retryable(exc)
                or attempt >= GEMINI_API_MAX_ATTEMPTS
            ):
                raise

            wait_seconds = (
                GEMINI_RETRY_BASE_SECONDS
                * (2 ** (attempt - 1))
                + random.uniform(0, 3)
            )

            print(
                "Temporary Gemini error. "
                f"Retrying in {wait_seconds:.1f}s...",
                file=sys.stderr,
            )

            time.sleep(wait_seconds)

    if last_error is not None:
        raise last_error

    raise RuntimeError(
        "Gemini retry loop ended unexpectedly."
    )


def generate_titles(
    package: dict[str, Any],
) -> list[dict[str, Any]]:
    story = approved_story_from(
        package
    )

    client = genai.Client(
        api_key=required_env(
            "GEMINI_API_KEY"
        )
    )

    example = {
        "title_ideas": [
            {
                "number": 1,
                "title": "DUCK-PUN ENGLISH TITLE",
                "meaning_ja": (
                    "日本語の意味と英語のダジャレ・"
                    "言葉遊びの説明"
                ),
            }
            for _ in range(TITLE_COUNT)
        ]
    }

    prompt = f"""
You are the headline editor for The Daily Duck.

The Daily Duck is ENGLISH-FIRST.
English is the canonical publication language.

Generate EXACTLY THREE replacement publication titles
for the already-approved story below.

IMPORTANT:
DO NOT change, regenerate, or discuss the existing images.
This task is TITLES ONLY.

============================================================
THE DAILY DUCK HEADLINE VOICE
============================================================

The titles must sound unmistakably like The Daily Duck.

Do NOT return:
- ordinary newspaper headlines
- neutral summaries
- academic titles
- generic clickbait

Use clever, natural duck-related English wordplay whenever
the story allows it.

Useful vocabulary includes, but is not limited to:
QUACK, QUACKING, DUCK, DUCKING, WADDLE, BILL, FEATHER,
EGG, EGG-CELLENT, FLOCK, WEBBED, BEAK.

Do not mechanically force the same duck word into every title.

Create three DIFFERENT styles:

1. BIG PUN
   Strongest and funniest story-appropriate duck pun.

2. SMART WORDPLAY
   Clever idiom twist, double meaning, or story-specific
   duck-flavored phrase.

3. SHORT DUCK PUNCH
   Very short, memorable, energetic and brandable.

Style examples only:
- WHALE, WHALE, WHALE... QUACKING GOOD NEWS!
- DUCKING OUT OF SIGHT!
- QUACK TO THE FUTURE!
- EGG-CELLENT NEWS!
- WHAT THE DUCK?!

Do not copy an example unless it genuinely fits this story.

QUALITY RULES:
- English-speaking readers should understand the joke.
- Keep the meaning connected to the approved story.
- Prefer roughly 2-8 words.
- ALL CAPS preferred.
- At least TWO of THREE titles must contain explicit
  duck-related wordplay.
- Ideally all three feel duck-flavored.
- Avoid awkward forced puns.
- Avoid childish baby-talk.
- Do not invent unsupported facts.
- Japanese must not appear inside the English title.

For meaning_ja, explain BOTH:
- the natural Japanese meaning
- the English pun/wordplay or nuance

Before returning JSON, silently ask:
"Could this title appear unchanged on an ordinary news site?"
If yes, rewrite it to sound more like The Daily Duck.

Return ONLY valid JSON.
No Markdown fences.

Return exactly:

{json.dumps(
    example,
    ensure_ascii=False,
    indent=2,
)}

APPROVED STORY:

{json.dumps(
    story,
    ensure_ascii=False,
    indent=2,
)}
""".strip()

    response = call_gemini(
        client,
        prompt,
    )

    raw = getattr(
        response,
        "text",
        None,
    )

    if not raw:
        raise RuntimeError(
            "Gemini returned no title text."
        )

    parsed = json.loads(
        clean_json_text(raw)
    )

    if not isinstance(parsed, dict):
        raise ValueError(
            "Gemini title response must be an object."
        )

    titles = parsed.get(
        "title_ideas"
    )

    if (
        not isinstance(titles, list)
        or len(titles) != TITLE_COUNT
    ):
        raise ValueError(
            "Gemini must return exactly 3 title ideas."
        )

    normalized: list[dict[str, Any]] = []

    for index, item in enumerate(
        titles,
        start=1,
    ):
        if not isinstance(item, dict):
            raise ValueError(
                f"Title {index} must be an object."
            )

        title = first_text(
            item.get("title")
        )

        meaning_ja = first_text(
            item.get("meaning_ja")
        )

        if not title or not meaning_ja:
            raise ValueError(
                f"Title {index} is incomplete."
            )

        normalized.append(
            {
                "number": index,
                "title": title,
                "meaning_ja": meaning_ja,
            }
        )

    return normalized


def main() -> int:
    package = load_package()

    validate_existing_design(
        package
    )

    old_titles = package.get(
        "title_ideas",
        [],
    )

    new_titles = generate_titles(
        package
    )

    package[
        "previous_title_ideas"
    ] = old_titles

    package[
        "title_ideas"
    ] = new_titles

    package[
        "titles_regenerated_at"
    ] = datetime.now(
        timezone.utc
    ).isoformat()

    package[
        "title_style"
    ] = "DAILY_DUCK_PUN_ENGLISH"

    # Put the package back into an email-ready state.
    # Existing images and concepts are preserved byte-for-byte.
    package[
        "state"
    ] = "DESIGN_OPTIONS_READY"

    # The next email should be treated as the current approval email.
    package.pop(
        "final_email_subject",
        None,
    )

    package.pop(
        "email_subject",
        None,
    )

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
        "Exactly 3 Daily Duck pun-style titles regenerated."
    )
    print(
        "Existing 3 concepts preserved."
    )
    print(
        "Existing 3 image files preserved."
    )
    print(
        "No OpenAI image request was made."
    )
    print(
        "STATE: DESIGN_OPTIONS_READY"
    )
    print(
        "NEXT: run send_design_approval_email.py"
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
