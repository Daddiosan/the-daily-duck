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
                "title": "DISTINCT DAILY DUCK TITLE",
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
THE DAILY DUCK — THREE CREATIVE LANES
============================================================

Generate EXACTLY THREE replacement English titles.
Each title MUST use a different creative lane.

============================================================
TITLE 1 — DUCK PUN
============================================================

Title 1 MUST contain a genuine, explainable DUCK-RELATED English pun.

Simply inserting QUACK, DUCK, WADDLE, BILL, FEATHER, EGG,
FLOCK, WEBBED, or BEAK is NOT enough.

Use a real mechanism such as:
- phonetic substitution
- recognizable phrase / idiom transformation
- double meaning
- homophone-like play
- familiar expression transformed with duck vocabulary

VALID mechanism examples:
- QUACK TO THE FUTURE! -> "Back to the Future"
- BILL-IEVE IT OR NOT! -> "Believe it or not"
- WADDLE IT TAKE? -> "What'll it take?"
- QUACK OF DAWN! -> "Crack of dawn"
- EGG-CELLENT NEWS! -> "Excellent"
- DUCKING OUT OF SIGHT! -> double meaning of "ducking"

INVALID:
- AMAZING DUCK NEWS!
- QUACKING DISCOVERY!
- HAPPY WADDLE DAY!

============================================================
TITLE 2 — SMART WORDPLAY
============================================================

Title 2 MUST contain clever English wordplay connected to the story,
but it does NOT need to involve ducks.

Possible mechanisms:
- idiom twist
- double meaning
- rhyme
- alliteration
- familiar phrase transformation
- story-specific vocabulary used unexpectedly

Do NOT simply make another duck pun.
This lane exists to prevent daily repetition.

============================================================
TITLE 3 — POSTER COPY
============================================================

Title 3 does NOT need a pun.

Make it the strongest, most memorable, most visually punchy
poster-style line for the approved story.

Think:
- magazine cover
- movie-poster energy
- social-card headline
- short advertising-style copy

Do NOT make it sound like an ordinary newspaper headline.

============================================================
LENGTH / QUALITY
============================================================

For all three:
- Usually around 4 to 9 words.
- Shorter is fine when strong.
- Slightly longer is fine when rhythm is excellent.
- Prefer ALL CAPS.
- Instantly understandable to English-speaking readers.
- Warm rather than cynical.
- Avoid childish baby-talk.
- Avoid dry academic language.
- Avoid generic clickbait.
- Do not invent unsupported facts.
- Keep every title clearly connected to the approved story.

============================================================
VARIETY
============================================================

The three choices must feel clearly different:

1. DUCK PUN = Daily Duck brand anchor
2. SMART WORDPLAY = fresh clever English
3. POSTER COPY = strongest visual/catchphrase impact

Do not repeat the same key joke or phrase.
Do not force duck vocabulary into Titles 2 or 3.

============================================================
meaning_ja
============================================================

Title 1 meaning_ja:
- natural Japanese meaning
- exact original English expression behind the duck pun
- how the duck substitution / double meaning creates the joke

Title 2 meaning_ja:
- natural Japanese meaning
- explanation of its English wordplay / idiom / rhythm

Title 3 meaning_ja:
- natural Japanese meaning
- explanation of its poster-copy nuance and impact

============================================================
SELF-CHECK
============================================================

TITLE 1:
Is this truly a duck-related pun with an identifiable source phrase?

TITLE 2:
Is this clever wordplay and clearly different from Title 1?

TITLE 3:
Is this powerful poster copy even without a pun?

ALL:
Are they accurate, concise, memorable, and clearly different?

Rewrite any option that fails its lane.

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
    ] = "DUCK_PUN_WORDPLAY_POSTER_COPY"

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
