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
    error_text = str(exc).lower()

    return any(
        marker in error_text
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

            response = client.models.generate_content(
                model=TEXT_MODEL,
                contents=prompt,
            )

            print(
                "Gemini title request succeeded."
            )

            return response

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
                "title": "CATCHY DAILY DUCK POSTER COPY",
                "meaning_ja": (
                    "日本語の意味と、英語の言葉遊び・"
                    "リズム・ニュアンスの説明"
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
THE DAILY DUCK POSTER-COPY VOICE
============================================================

The titles should feel like POSTER COPY,
not ordinary news headlines.

The tone should be:
- instantly readable
- memorable
- playful
- visually punchy
- warm rather than cynical
- suitable for a poster, social card, or magazine cover

Duck-related puns and wordplay are welcome when they fit naturally,
but DO NOT force a duck pun into every title.

Useful duck-language vocabulary may include:
QUACK, QUACKING, DUCK, DUCKING, WADDLE, BILL, FEATHER,
FEATHERS, EGG, EGG-CELLENT, FLOCK, WEBBED, BEAK.

IMPORTANT:
The goal is NOT maximum pun density.
The goal is a catchy poster line that sounds like The Daily Duck.

Create three DIFFERENT styles:

1. DUCK PUN
   A natural, story-relevant duck pun or playful word twist.

2. SMART POSTER COPY
   A clever, stylish, memorable line that may or may not use
   explicit duck vocabulary.

3. BIG PUNCH
   The strongest, boldest, most poster-like line of the three.

LENGTH:
- Usually around 4 to 9 words.
- Shorter is fine when the line is strong.
- Slightly longer is fine when the rhythm is good.
- Avoid long explanatory sentences.

STYLE EXAMPLES ONLY:
- QUACK TO THE FUTURE!
- ONE SMALL WADDLE, ONE GIANT DISCOVERY!
- WHALE, WHALE... WHAT HAVE WE HERE?
- DUCKING INTO SOMETHING AMAZING!
- WHAT THE DUCK?!

Do NOT copy these unless they genuinely fit the approved story.

QUALITY RULES:
- English-speaking readers should understand the line immediately.
- It must connect clearly to the approved story.
- Do not invent unsupported facts.
- Avoid generic newspaper-style wording.
- Avoid dry academic phrasing.
- Avoid childish baby-talk.
- Avoid awkward or incomprehensible forced puns.
- Punchy punctuation is welcome when natural.
- ALL CAPS is preferred.
- At least ONE of the THREE titles should contain explicit
  duck-themed wordplay when natural.
- All three should feel unmistakably like The Daily Duck.

For meaning_ja, explain:
1. the natural Japanese meaning, and
2. any English pun, wordplay, rhythm, or nuance.

Before returning JSON, silently check:
"Does this sound like poster copy?"
If not, rewrite it.

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
    ] = "DAILY_DUCK_POSTER_COPY"

    # Existing concepts and images are preserved exactly.
    # Put the package back into an email-ready state.
    package[
        "state"
    ] = "DESIGN_OPTIONS_READY"

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
        "Exactly 3 Daily Duck poster-copy titles regenerated."
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
