#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
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
from openai import OpenAI


APPROVED_STORY_PATH = Path(
    "automation_state/approved_story.json"
)

OPTIONS_PATH = Path(
    "automation_state/design_options.json"
)

PREVIEW_ROOT = Path(
    "automation_images/design_previews"
)

TEXT_MODEL = (
    os.getenv("GEMINI_TEXT_MODEL") or ""
).strip() or "gemini-3.6-flash"

OPENAI_IMAGE_MODEL = (
    os.getenv("OPENAI_IMAGE_MODEL") or ""
).strip() or "gpt-image-2"

OPENAI_IMAGE_SIZE = (
    os.getenv("OPENAI_IMAGE_SIZE") or ""
).strip() or "1536x1024"

OPENAI_IMAGE_QUALITY = (
    os.getenv("OPENAI_IMAGE_QUALITY") or ""
).strip() or "medium"


IMAGE_CONCEPT_COUNT = 3
TITLE_IDEA_COUNT = 3

EDITORIAL_MAX_ATTEMPTS = int(
    os.getenv("CONCEPT_MAX_ATTEMPTS", "3")
)

GEMINI_API_MAX_ATTEMPTS = int(
    os.getenv("GEMINI_API_MAX_ATTEMPTS", "5")
)

GEMINI_RETRY_BASE_SECONDS = float(
    os.getenv("GEMINI_RETRY_BASE_SECONDS", "10")
)

MAX_DUPLICATE_RETRIES = 2


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

    data = json.loads(
        path.read_text(encoding="utf-8")
    )

    if not isinstance(data, dict):
        raise ValueError(
            f"{path} must contain a JSON object."
        )

    return data


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


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_approved_state(
    data: dict[str, Any],
) -> None:
    state = first_text(
        data.get("state")
    ).upper()

    if state != "APPROVED_STORY":
        raise ValueError(
            "Design options may only be generated "
            f"from APPROVED_STORY; got {state!r}."
        )


def find_approved_story(
    data: dict[str, Any],
) -> dict[str, Any]:
    for key in (
        "approved_story",
        "selected_story",
        "gate_a_approved_story",
        "story",
        "recommended_story",
    ):
        value = data.get(key)
        if isinstance(value, dict):
            return value

    package = data.get("package")

    if isinstance(package, dict):
        for key in (
            "approved_story",
            "selected_story",
            "gate_a_approved_story",
            "story",
            "recommended_story",
        ):
            value = package.get(key)
            if isinstance(value, dict):
                return value

    if any(
        key in data
        for key in (
            "title_en",
            "title",
            "en_copy",
            "jp_copy",
            "duck_name",
            "x_en",
        )
    ):
        return data

    raise ValueError(
        "Could not locate the approved story."
    )


def issue_date_from(
    data: dict[str, Any],
    story: dict[str, Any],
) -> str:
    issue_date = first_text(
        data.get("issue_date"),
        data.get("date"),
        story.get("issue_date"),
        story.get("date"),
    )

    if not issue_date:
        raise ValueError(
            "Approved story is missing issue_date/date."
        )

    return issue_date


def is_retryable_gemini_error(
    exc: Exception,
) -> bool:
    error_text = str(exc).lower()

    retryable_markers = (
        "429",
        "500",
        "502",
        "503",
        "504",
        "resource_exhausted",
        "internal",
        "bad_gateway",
        "unavailable",
        "deadline_exceeded",
        "high demand",
        "temporarily unavailable",
        "service unavailable",
        "timeout",
        "timed out",
    )

    return any(
        marker in error_text
        for marker in retryable_markers
    )


def call_gemini_with_retry(
    client: genai.Client,
    prompt: str,
):
    last_error: Exception | None = None

    for attempt in range(
        1,
        GEMINI_API_MAX_ATTEMPTS + 1,
    ):
        print(
            "Gemini API request attempt "
            f"{attempt}/{GEMINI_API_MAX_ATTEMPTS}..."
        )

        try:
            response = client.models.generate_content(
                model=TEXT_MODEL,
                contents=prompt,
            )

            print(
                "Gemini API request succeeded."
            )

            return response

        except Exception as exc:
            last_error = exc

            if not is_retryable_gemini_error(exc):
                raise

            if attempt >= GEMINI_API_MAX_ATTEMPTS:
                raise

            wait_seconds = (
                GEMINI_RETRY_BASE_SECONDS
                * (2 ** (attempt - 1))
                + random.uniform(0, 3)
            )

            print(
                "WARNING: Temporary Gemini API error. "
                f"Retrying in {wait_seconds:.1f}s...",
                file=sys.stderr,
            )

            time.sleep(wait_seconds)

    if last_error is not None:
        raise last_error

    raise RuntimeError(
        "Gemini retry loop ended unexpectedly."
    )


def generate_options(
    approved_state: dict[str, Any],
    approved_story: dict[str, Any],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    client = genai.Client(
        api_key=required_env("GEMINI_API_KEY")
    )

    output_example = {
        "image_concepts": [
            {
                "number": 1,
                "title_en": "Short English concept title",
                "concept_en": "Canonical English visual concept",
                "composition_en": (
                    "Canonical English composition, setting, "
                    "framing, subject, props and mood direction"
                ),
                "generation_prompt_en": (
                    "Production-ready English prompt for creating "
                    "one publishable image for this concept"
                ),
                "alt_en": "Canonical English alt-text draft",
                "title_ja": "自然な日本語コンセプト名",
                "concept_ja": "英語正本を基にした自然な日本語説明",
                "composition_ja": "英語正本を基にした自然な日本語構図説明",
                "alt_ja": "英語正本を基にした日本語alt案",
            }
            for _ in range(IMAGE_CONCEPT_COUNT)
        ],
        "title_ideas": [
            {
                "number": 1,
                "title": "CATCHY DAILY DUCK POSTER COPY",
                "meaning_ja": (
                    "日本語の意味と、英語の言葉遊び・リズム・ニュアンスを簡潔に説明"
                ),
            }
            for _ in range(TITLE_IDEA_COUNT)
        ],
    }

    prompt = f"""
You are the visual editorial director for The Daily Duck.

The Daily Duck is ENGLISH-FIRST.
English is canonical/master.
Japanese is review translation only.

The story below has already passed Gate A.
Do not change the story.

============================================================
TASK A — EXACTLY THREE IMAGE CONCEPTS
============================================================

Create EXACTLY THREE meaningfully different visual concepts.

The system will immediately generate ONE real image for EACH concept.

Therefore:
- concept 1 must have its own distinct visual idea
- concept 2 must have its own distinct visual idea
- concept 3 must have its own distinct visual idea
- generation_prompt_en must be production-ready for ONE image
- the three concepts must not be minor camera variations of one concept

Each generated image will later be attached to one approval email.

The human will reply with:

IMAGE_NUMBER TITLE_NUMBER

Example:
1 3

There is NO separate concept-selection turn.

============================================================
THE DAILY DUCK MASCOT
============================================================

Every concept must preserve:
- recognizable friendly yellow duck
- orange beak
- large dark glossy eyes
- small feather tuft
- warm approachable expression
- consistent mascot identity

============================================================
VISUAL DIRECTION
============================================================

- clean
- modern
- charming
- warm
- premium editorial
- simple rather than overly vintage
- strong focal point
- landscape hero-image composition
- no logos
- no watermarks
- no UI
- no readable embedded text
- no headline inside the generated image

============================================================
FACTUAL RULE
============================================================

Use ONLY facts supported by the approved story package.

Do not invent names, people, dates, numbers, locations,
quotations, organizations, scientific details, or factual
props that imply unsupported facts.

Illustrative visual metaphor is allowed only when it does not
falsely present invented details as factual.

============================================================
TASK B — EXACTLY THREE DAILY DUCK POSTER-COPY TITLES
============================================================

Create EXACTLY THREE short, catchy ENGLISH publication titles
that feel like POSTER COPY rather than ordinary news headlines.

The Daily Duck title voice should be:
- instantly readable
- memorable
- playful
- visually punchy
- warm rather than cynical
- suitable for a poster, social card, or magazine cover

Duck-related puns and wordplay are welcome when they fit naturally,
but DO NOT force a duck pun into every title.

Useful duck-language vocabulary may include:
- QUACK / QUACKING
- DUCK / DUCKING
- WADDLE
- BILL
- FEATHER / FEATHERS
- EGG / EGG-CELLENT
- FLOCK
- WEBBED
- BEAK

IMPORTANT:
The goal is NOT "maximum pun density."
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
- ALL CAPS is preferred for the final publication title.
- At least ONE of the THREE titles should contain explicit
  duck-themed wordplay when natural.
- All three should still feel unmistakably like The Daily Duck.

For each title, meaning_ja must explain:
1. the natural Japanese meaning, and
2. any English pun, wordplay, rhythm, or nuance.

Before returning JSON, silently check:
"Does this sound like poster copy?"
If not, rewrite it.

============================================================
OUTPUT RULES
============================================================

- Exactly THREE image concepts.
- Exactly THREE title ideas.
- Number concepts 1-3.
- Number titles 1-3.
- Every required field non-empty.
- Return ONLY valid JSON.
- No Markdown fences.

Return exactly this structure:

{json.dumps(
    output_example,
    ensure_ascii=False,
    indent=2,
)}

============================================================
APPROVED STORY
============================================================

{json.dumps(
    approved_story,
    ensure_ascii=False,
    indent=2,
)}

============================================================
FULL APPROVED STATE
============================================================

{json.dumps(
    approved_state,
    ensure_ascii=False,
    indent=2,
)}
""".strip()

    required_concept_fields = (
        "title_en",
        "concept_en",
        "composition_en",
        "generation_prompt_en",
        "alt_en",
        "title_ja",
        "concept_ja",
        "composition_ja",
        "alt_ja",
    )

    required_title_fields = (
        "title",
        "meaning_ja",
    )

    last_error: Exception | None = None

    for attempt in range(
        1,
        EDITORIAL_MAX_ATTEMPTS + 1,
    ):
        try:
            print(
                "Design option generation attempt "
                f"{attempt}/{EDITORIAL_MAX_ATTEMPTS}..."
            )

            response = call_gemini_with_retry(
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
                    "Gemini returned no design option text."
                )

            parsed = json.loads(
                clean_json_text(raw)
            )

            if not isinstance(parsed, dict):
                raise ValueError(
                    "Gemini response must be a JSON object."
                )

            concepts = parsed.get("image_concepts")
            titles = parsed.get("title_ideas")

            if (
                not isinstance(concepts, list)
                or len(concepts) != IMAGE_CONCEPT_COUNT
            ):
                raise ValueError(
                    "Gemini must return exactly 3 image concepts."
                )

            if (
                not isinstance(titles, list)
                or len(titles) != TITLE_IDEA_COUNT
            ):
                raise ValueError(
                    "Gemini must return exactly 3 title ideas."
                )

            normalized_concepts = []

            for index, item in enumerate(
                concepts,
                start=1,
            ):
                if not isinstance(item, dict):
                    raise ValueError(
                        f"Image concept {index} must be an object."
                    )

                normalized = dict(item)
                normalized["number"] = index

                for field in required_concept_fields:
                    value = first_text(
                        normalized.get(field)
                    )

                    if not value:
                        raise ValueError(
                            f"Image concept {index} is missing {field}."
                        )

                    normalized[field] = value

                normalized_concepts.append(
                    normalized
                )

            normalized_titles = []

            for index, item in enumerate(
                titles,
                start=1,
            ):
                if not isinstance(item, dict):
                    raise ValueError(
                        f"Title idea {index} must be an object."
                    )

                normalized = dict(item)
                normalized["number"] = index

                for field in required_title_fields:
                    value = first_text(
                        normalized.get(field)
                    )

                    if not value:
                        raise ValueError(
                            f"Title idea {index} is missing {field}."
                        )

                    normalized[field] = value

                normalized_titles.append(
                    normalized
                )

            return (
                normalized_concepts,
                normalized_titles,
            )

        except Exception as exc:
            last_error = exc

            if attempt < EDITORIAL_MAX_ATTEMPTS:
                print(
                    "WARNING: Invalid/incomplete package: "
                    f"{exc}"
                )
                print(
                    "Retrying design option generation..."
                )
                continue

    if last_error is not None:
        raise last_error

    raise RuntimeError(
        "Design option generation ended unexpectedly."
    )


def build_image_prompt(
    approved_story: dict[str, Any],
    concept: dict[str, Any],
    concept_number: int,
    retry: int,
) -> str:
    headline = first_text(
        approved_story.get("title_en"),
        approved_story.get("title"),
        approved_story.get("title_ja"),
    )

    summary = first_text(
        approved_story.get("reason_en"),
        approved_story.get("en_copy"),
        approved_story.get("reason"),
        approved_story.get("jp_copy"),
    )

    retry_note = ""

    if retry > 0:
        retry_note = f"""
RETRY {retry}:
The previous generated image was byte-identical to another option.
Make this concept visually unmistakable and distinct while preserving
the exact concept below.
""".strip()

    return f"""
Create ONE polished, publishable landscape hero image for The Daily Duck.

APPROVED STORY:
Headline: {headline}
Summary: {summary}

THIS IS CONCEPT {concept_number} OF 3.

CONCEPT TITLE:
{first_text(concept.get("title_en"))}

CONCEPT:
{first_text(concept.get("concept_en"))}

COMPOSITION:
{first_text(concept.get("composition_en"))}

PRODUCTION DIRECTION:
{first_text(concept.get("generation_prompt_en"))}

{retry_note}

The three concept images must look meaningfully different from each other.

Preserve The Daily Duck mascot:
- friendly recognizable yellow duck
- orange beak
- large dark glossy eyes
- small feather tuft
- warm expression

Style:
- clean
- modern
- charming
- premium editorial
- landscape hero composition
- publication quality

Do not include:
- readable text
- numbers
- headlines
- logos
- watermarks
- UI

Do not invent unsupported facts.
""".strip()


def generate_one_image(
    client: OpenAI,
    prompt: str,
    number: int,
) -> bytes:
    print(
        f"Generating concept image {number}/3: "
        f"{OPENAI_IMAGE_MODEL}, "
        f"{OPENAI_IMAGE_SIZE}, "
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

    if (
        not result.data
        or not result.data[0].b64_json
    ):
        raise RuntimeError(
            f"OpenAI returned no image for concept {number}."
        )

    return base64.b64decode(
        result.data[0].b64_json
    )


def generate_concept_images(
    issue_date: str,
    approved_story: dict[str, Any],
    concepts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, Path]:
    openai_client = OpenAI(
        api_key=required_env("OPENAI_API_KEY")
    )

    batch_number = 1

    out_dir = (
        PREVIEW_ROOT
        / issue_date
        / f"batch_{batch_number:02d}"
    )

    out_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    previews: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()

    for concept in concepts:
        number = int(concept["number"])

        chosen_bytes: bytes | None = None
        chosen_prompt = ""
        chosen_hash = ""

        for retry in range(
            0,
            MAX_DUPLICATE_RETRIES + 1,
        ):
            prompt = build_image_prompt(
                approved_story=approved_story,
                concept=concept,
                concept_number=number,
                retry=retry,
            )

            image_bytes = generate_one_image(
                openai_client,
                prompt,
                number,
            )

            digest = sha256_bytes(
                image_bytes
            )

            if digest not in seen_hashes:
                chosen_bytes = image_bytes
                chosen_prompt = prompt
                chosen_hash = digest
                break

            print(
                f"WARNING: concept image {number} "
                "was byte-identical to an earlier image. "
                "Regenerating..."
            )

        if chosen_bytes is None:
            raise RuntimeError(
                f"Could not create a unique image for concept {number}."
            )

        seen_hashes.add(chosen_hash)

        path = (
            out_dir
            / f"preview_{number}.png"
        )

        path.write_bytes(
            chosen_bytes
        )

        previews.append(
            {
                "number": number,
                "concept_number": number,
                "concept_title_en": first_text(
                    concept.get("title_en")
                ),
                "concept_title_ja": first_text(
                    concept.get("title_ja")
                ),
                "image_path": path.as_posix(),
                "mime_type": "image/png",
                "provider": "OpenAI",
                "model": OPENAI_IMAGE_MODEL,
                "size": OPENAI_IMAGE_SIZE,
                "quality": OPENAI_IMAGE_QUALITY,
                "sha256": chosen_hash,
                "generation_prompt": chosen_prompt,
                "alt_en": first_text(
                    concept.get("alt_en")
                ),
                "alt_ja": first_text(
                    concept.get("alt_ja")
                ),
            }
        )

    if len(previews) != 3:
        raise RuntimeError(
            f"Expected exactly 3 previews, got {len(previews)}."
        )

    return (
        previews,
        batch_number,
        out_dir,
    )


def main() -> int:
    approved_state = load_json(
        APPROVED_STORY_PATH
    )

    validate_approved_state(
        approved_state
    )

    approved_story = find_approved_story(
        approved_state
    )

    issue_date = issue_date_from(
        approved_state,
        approved_story,
    )

    (
        image_concepts,
        title_ideas,
    ) = generate_options(
        approved_state,
        approved_story,
    )

    (
        previews,
        batch_number,
        preview_path,
    ) = generate_concept_images(
        issue_date,
        approved_story,
        image_concepts,
    )

    package = {
        "state": "DESIGN_OPTIONS_READY",
        "issue_date": issue_date,
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),

        "approved_story": approved_state,
        "approved_story_compact": approved_story,

        "image_concepts": image_concepts,
        "title_ideas": title_ideas,

        # One real image is attached to each concept.
        "design_previews": previews,

        "preview_batch_number": batch_number,
        "preview_batch_path": preview_path.as_posix(),
        "preview_candidate_count": 3,

        "selected_image_concept_number": None,
        "selected_image_concept": None,
        "selected_image_number": None,
        "selected_title_number": None,

        "design_flow": {
            "selection_turns_after_gate_a": 1,
            "concept_count": 3,
            "images_per_concept": 1,
            "image_count": 3,
            "title_count": 3,
            "reply_format": "IMAGE_NUMBER TITLE_NUMBER",
            "example": "1 3",
            "next_3_supported": True,
            "full_width_supported": True,
        },

        "language_policy": {
            "primary_language": "en",
            "canonical_language": "en",
            "translation_language": "ja",
            "translation_source": "english_master",
        },
    }

    OPTIONS_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
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

    print("Generated exactly 3 image concepts.")
    print("Generated exactly 1 real image per concept.")
    print("Generated exactly 3 real images total.")
    print("Generated exactly 3 title ideas.")
    print("STATE: DESIGN_OPTIONS_READY")
    print(f"Saved: {OPTIONS_PATH}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )
        raise
