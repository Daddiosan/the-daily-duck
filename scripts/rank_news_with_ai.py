import json
import re

from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import (
    parse_qsl,
    urlencode,
    urlsplit,
    urlunsplit,
)

try:
    # Running as `python scripts/rank_news_with_ai.py` (production/workflow
    # invocation): the repo root is not on sys.path, only scripts/ is.
    from scripts import llm_provider
except ImportError:
    # Running as a script directly: scripts/ itself is on sys.path, so
    # llm_provider is a plain sibling module (matches this repo's existing
    # convention, e.g. `from model_config import ...` in other scripts/*.py).
    import llm_provider


INPUT_FILE = "news_candidates.json"
OUTPUT_FILE = "ai_ranked_news.json"
ARCHIVE_FILE = Path("data/archive.json")

# ------------------------------------------------------------
# Duplicate/history settings
# ------------------------------------------------------------

MAX_HISTORY_ITEMS = 60
CURRENT_TITLE_SIMILARITY = 0.92

# ------------------------------------------------------------
# Sad/tragic-story guard (Phase A, Human Decision 1)
#
# Deterministic, provider-independent, and derived directly from the
# "AVOID NEGATIVE STORIES" section of build_prompt() below -- the same
# category labels already used to instruct whichever LLM ranks the news.
# Scoped to the `category` field only, not `reason`/`title` free text:
# `category` is a short LLM-assigned label (see the CATEGORY section of
# build_prompt(), whose own example categories never include these terms),
# while `reason`/`title` are prose that can legitimately mention words like
# "illness" while describing an uplifting recovery story -- the prompt's
# own text explicitly allows that ("A recovery or conservation story may
# qualify when its dominant emotional feeling is hopeful and positive").
# Scanning prose would create false positives against that carve-out;
# scanning the category label does not.
# ------------------------------------------------------------

PROHIBITED_CATEGORY_PATTERNS = [
    re.compile(r"\bdeath\b", re.IGNORECASE),
    re.compile(r"\btragedy\b", re.IGNORECASE),
    re.compile(r"\btragic\b", re.IGNORECASE),
    re.compile(r"\bwar\b", re.IGNORECASE),
    re.compile(r"\bcrime\b", re.IGNORECASE),
    re.compile(r"\bcriminal\b", re.IGNORECASE),
    re.compile(r"\bdisaster\b", re.IGNORECASE),
    re.compile(r"\bfear\b", re.IGNORECASE),
    re.compile(r"\bsuffering\b", re.IGNORECASE),
    re.compile(r"\boutrage\b", re.IGNORECASE),
    re.compile(r"political conflict", re.IGNORECASE),
    re.compile(r"severe illness", re.IGNORECASE),
]


# ============================================================
# Basic helpers
# ============================================================

def text(value):
    if isinstance(value, str):
        return value.strip()

    return ""


def normalize_title(value):
    value = text(value).lower()

    value = re.sub(
        r"https?://\S+",
        "",
        value,
    )

    value = re.sub(
        r"[^a-z0-9\u3040-\u30ff\u3400-\u9fff]+",
        " ",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def normalize_url(value):
    value = text(value)

    if not value:
        return ""

    try:
        parts = urlsplit(value)

        scheme = (
            parts.scheme.lower()
            or "https"
        )

        netloc = parts.netloc.lower()

        path = (
            parts.path.rstrip("/")
            or "/"
        )

        ignored_params = {
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_term",
            "utm_content",
            "utm_id",
            "gclid",
            "fbclid",
            "mc_cid",
            "mc_eid",
        }

        query_items = []

        for key, val in parse_qsl(
            parts.query,
            keep_blank_values=True,
        ):
            if key.lower() not in ignored_params:
                query_items.append(
                    (key, val)
                )

        query_items.sort()

        query = urlencode(
            query_items,
            doseq=True,
        )

        return urlunsplit(
            (
                scheme,
                netloc,
                path,
                query,
                "",
            )
        )

    except Exception:
        return value.rstrip("/")


# ============================================================
# Load candidates
# ============================================================

def load_candidates():
    with open(
        INPUT_FILE,
        "r",
        encoding="utf-8",
    ) as file:
        data = json.load(file)

    if "candidates" in data:
        candidates = data["candidates"]

    elif "all_candidates" in data:
        candidates = data["all_candidates"]

    elif "shortlist" in data:
        candidates = data["shortlist"]

    elif "filtered" in data:
        candidates = data["filtered"]

    else:
        raise RuntimeError(
            "No candidate list found in "
            "news_candidates.json. "
            f"Available keys: {list(data.keys())}"
        )

    if not isinstance(
        candidates,
        list,
    ):
        raise RuntimeError(
            "Candidate data must be a list."
        )

    return candidates


# ============================================================
# Load Daily Duck archive
# ============================================================

def load_archive():
    if not ARCHIVE_FILE.exists():

        print(
            "WARNING: data/archive.json does not exist."
        )

        return []

    try:
        archive = json.loads(
            ARCHIVE_FILE.read_text(
                encoding="utf-8"
            )
        )

    except Exception as exc:
        raise RuntimeError(
            "Could not read data/archive.json: "
            f"{exc}"
        ) from exc

    if not isinstance(
        archive,
        list,
    ):
        raise RuntimeError(
            "data/archive.json must be a JSON array."
        )

    published = []

    for item in archive:

        if not isinstance(
            item,
            dict,
        ):
            continue

        if item.get(
            "published",
            True,
        ) is False:
            continue

        published.append(
            item
        )

    return published


# ============================================================
# Already-published URL exclusion
# ============================================================

def build_published_url_set(
    archive,
):
    urls = set()

    for item in archive:

        url = normalize_url(
            item.get(
                "sourceUrl"
            )
        )

        if url:
            urls.add(url)

    return urls


def remove_already_published_urls(
    candidates,
    archive,
):
    published_urls = (
        build_published_url_set(
            archive
        )
    )

    filtered = []
    removed = []

    for story in candidates:

        if not isinstance(
            story,
            dict,
        ):
            continue

        candidate_url = normalize_url(
            story.get(
                "url"
            )
        )

        if (
            candidate_url
            and candidate_url
            in published_urls
        ):
            removed.append(
                story
            )

            continue

        filtered.append(
            story
        )

    return filtered, removed


# ============================================================
# Duplicate cleanup inside today's candidates
# ============================================================

def remove_same_day_duplicates(
    candidates,
):
    kept = []
    removed = []

    seen_urls = set()
    seen_titles = []

    for story in candidates:

        if not isinstance(
            story,
            dict,
        ):
            continue

        url = normalize_url(
            story.get(
                "url"
            )
        )

        title = normalize_title(
            story.get(
                "title"
            )
        )

        # Exact URL duplicate
        if (
            url
            and url in seen_urls
        ):
            removed.append(
                story
            )

            continue

        # Very similar headline
        duplicate_title = False

        if title:

            for previous_title in seen_titles:

                similarity = SequenceMatcher(
                    None,
                    title,
                    previous_title,
                ).ratio()

                if (
                    similarity
                    >= CURRENT_TITLE_SIMILARITY
                ):
                    duplicate_title = True
                    break

        if duplicate_title:

            removed.append(
                story
            )

            continue

        kept.append(
            story
        )

        if url:
            seen_urls.add(url)

        if title:
            seen_titles.append(title)

    return kept, removed


# ============================================================
# Build archive history for Gemini
# ============================================================

def build_history_for_prompt(
    archive,
):
    history = []

    for item in archive[
        :MAX_HISTORY_ITEMS
    ]:

        if not isinstance(
            item,
            dict,
        ):
            continue

        history.append(
            {
                "date": text(
                    item.get("date")
                ),

                "daily_duck_title": text(
                    item.get("title")
                ),

                "source": text(
                    item.get("sourceLabel")
                ),

                "source_url": text(
                    item.get("sourceUrl")
                ),

                "story_en": text(
                    item.get("storyEn")
                )[:500],

                "story_ja": text(
                    item.get("storyJa")
                )[:500],

                "summary_en": text(
                    item.get(
                        "archiveSummaryEn"
                    )
                )[:350],

                "summary_ja": text(
                    item.get(
                        "archiveSummaryJa"
                    )
                )[:350],
            }
        )

    return history


# ============================================================
# Prompt
# ============================================================

def build_prompt(
    candidates,
    archive,
):
    stories = []

    for index, story in enumerate(
        candidates,
        start=1,
    ):

        stories.append(
            {
                "id": index,

                "source": text(
                    story.get(
                        "source"
                    )
                ),

                "title": text(
                    story.get(
                        "title"
                    )
                ),

                "description": text(
                    story.get(
                        "description"
                    )
                )[:700],

                "url": text(
                    story.get(
                        "url"
                    )
                ),

                "published": text(
                    story.get(
                        "published"
                    )
                ),
            }
        )

    history = build_history_for_prompt(
        archive
    )

    candidate_json = json.dumps(
        stories,
        ensure_ascii=False,
        indent=2,
    )

    history_json = json.dumps(
        history,
        ensure_ascii=False,
        indent=2,
    )

    return f"""
You are the senior editorial ranking assistant for
The Daily Duck.

============================================================
MISSION
============================================================

Choose news that leaves ordinary readers feeling:

- happier
- warmer
- hopeful
- amused
- delighted
- inspired
- pleasantly surprised
- positively curious

The Daily Duck is a cheerful GENERAL-INTEREST publication.

It is NOT a science publication.
It is NOT a technology publication.
It is NOT a research-news publication.

There is NO preferred subject category.

============================================================
CORRECT THE RECENT SCIENCE BIAS
============================================================

Recent Daily Duck editions have contained too many:

- science stories
- academic research stories
- neuroscience stories
- space stories
- astronomy stories
- technical discoveries

Correct that bias.

Scientific importance by itself is NOT a reason
to rank a story highly.

A simple, funny, delightful, heartwarming or surprising
general-interest story should beat a major scientific
breakthrough when ordinary readers would enjoy it more.

============================================================
CATEGORY DIVERSITY
============================================================

The TOP FIVE should feel like a varied and entertaining
front page.

Possible categories include:

- people
- animals
- community
- kindness
- culture
- food
- travel
- places
- sport
- creativity
- entertainment
- unusual events
- quirky news
- conservation
- positive environment
- nature
- children / family
- achievements
- science
- space
- technology
- other positive general-interest stories

Normally choose NO MORE THAN ONE story from the combined:

science / academic research / neuroscience /
space / astronomy / technology

category in today's TOP FIVE.

You may exceed this only if the available non-science
candidates are clearly too weak to create five good stories.

Do NOT choose poor stories simply to satisfy diversity.

However, when two stories are approximately equal in quality,
strongly prefer the category that is not already represented.

============================================================
PAST STORY DUPLICATES
============================================================

Below is The Daily Duck's PUBLISHED HISTORY.

A previously published story MUST NOT be selected again.

Reject a candidate when it is:

1. the exact same article,
2. the same URL,
3. the same event reported by another publisher,
4. a rewritten version of an already-used event,
5. a minor update without a genuinely new development.

Example:

Previously published:
"A zoo welcomes twin pandas."

Today:
"Another publisher reports that twin pandas were born."

=> DUPLICATE. DO NOT SELECT.

But:

Months later:
"The twin pandas make their first public appearance."

=> This can be a genuinely new event.

============================================================
DUPLICATES INSIDE TODAY'S TOP FIVE
============================================================

Do not select two publishers covering essentially the
same event.

Each TOP FIVE story should represent a meaningfully
different story.

============================================================
AVOID NEGATIVE STORIES
============================================================

Avoid stories whose main emotional focus is:

- death
- tragedy
- war
- crime
- political conflict
- disaster
- fear
- severe illness
- suffering
- outrage

A recovery or conservation story may qualify when its
dominant emotional feeling is hopeful and positive.

============================================================
WHAT SHOULD WIN
============================================================

Prefer:

- instant emotional appeal
- broad accessibility
- charm
- warmth
- surprise
- humour
- "I want to tell somebody this" value
- playful visual potential
- easy-to-understand stories
- freshness
- genuine novelty

Penalize:

- specialist-only interest
- technical importance without emotional appeal
- academic press releases that mainly matter to specialists
- stories requiring long technical explanations
- repetitive science / space / research themes
- anything already published by The Daily Duck

============================================================
SCORING
============================================================

Score selected stories:

- happiness: 0-10
- hope: 0-10
- general_interest: 0-10
- surprise: 0-10
- duck_visual: 0-10
- source_quality: 0-10
- freshness: 0-10
- broad_appeal: 0-10
- novelty_vs_archive: 0-10

total_score must be 0-100.

============================================================
CATEGORY
============================================================

Assign ONE concise category.

Examples:

people
animals
community
culture
food
sport
travel
nature
conservation
science
space
technology
quirky
creativity
other

============================================================
FINAL SELECTION
============================================================

Return exactly FIVE stories.

Choose exactly ONE recommended story.

The recommended story should usually have the strongest
combination of:

- broad appeal
- happiness / warmth
- surprise
- memorability
- visual fun

Do NOT automatically recommend the most scientifically
important story.

============================================================
OUTPUT FORMAT — MANDATORY
============================================================

Return a single JSON object with EXACTLY this top-level shape
(field names must match exactly):

{{
  "recommended_id": <integer, one of the five ids in "top_five">,
  "recommended_reason": "<string>",
  "top_five": [
    {{
      "id": <integer, exact original candidate id>,
      "title": "<string>",
      "source": "<string>",
      "url": "<string>",
      "category": "<string>",
      "happiness": <integer 0-10>,
      "hope": <integer 0-10>,
      "general_interest": <integer 0-10>,
      "surprise": <integer 0-10>,
      "duck_visual": <integer 0-10>,
      "source_quality": <integer 0-10>,
      "freshness": <integer 0-10>,
      "broad_appeal": <integer 0-10>,
      "novelty_vs_archive": <integer 0-10>,
      "total_score": <integer 0-100>,
      "reason": "<string>"
    }}
    ... exactly 5 objects in this array, no more, no fewer ...
  ]
}}

STRICT OUTPUT RULES:

- "top_five" MUST contain EXACTLY 5 objects. Not 4. Not 6.
- Do not wrap this object inside another key.
- Do not return a bare JSON array as the top-level response.
- Do not omit "recommended_id" or "recommended_reason".
- Do not rename any field.
- Every candidate id in "top_five" must be one of the ids supplied in
  TODAY'S CANDIDATES below, and no id may repeat.

============================================================
PUBLISHED DAILY DUCK HISTORY
============================================================

{history_json}

============================================================
TODAY'S CANDIDATES
============================================================

{candidate_json}

Return only the requested JSON.
"""


# ============================================================
# Gemini schema
# ============================================================

def build_schema():
    return {
        "type": "object",

        "properties": {

            "recommended_id": {
                "type": "integer"
            },

            "recommended_reason": {
                "type": "string"
            },

            "top_five": {
                "type": "array",
                "minItems": 5,
                "maxItems": 5,

                "items": {
                    "type": "object",

                    "properties": {

                        "id": {
                            "type": "integer"
                        },

                        "title": {
                            "type": "string"
                        },

                        "source": {
                            "type": "string"
                        },

                        "url": {
                            "type": "string"
                        },

                        "category": {
                            "type": "string"
                        },

                        "happiness": {
                            "type": "integer"
                        },

                        "hope": {
                            "type": "integer"
                        },

                        "general_interest": {
                            "type": "integer"
                        },

                        "surprise": {
                            "type": "integer"
                        },

                        "duck_visual": {
                            "type": "integer"
                        },

                        "source_quality": {
                            "type": "integer"
                        },

                        "freshness": {
                            "type": "integer"
                        },

                        "broad_appeal": {
                            "type": "integer"
                        },

                        "novelty_vs_archive": {
                            "type": "integer"
                        },

                        "total_score": {
                            "type": "integer"
                        },

                        "reason": {
                            "type": "string"
                        },
                    },

                    "required": [
                        "id",
                        "title",
                        "source",
                        "url",
                        "category",
                        "happiness",
                        "hope",
                        "general_interest",
                        "surprise",
                        "duck_visual",
                        "source_quality",
                        "freshness",
                        "broad_appeal",
                        "novelty_vs_archive",
                        "total_score",
                        "reason",
                    ],
                },
            },
        },

        "required": [
            "recommended_id",
            "recommended_reason",
            "top_five",
        ],
    }


# ============================================================
# Gemini/OpenAI request with fallback
#
# scripts/llm_provider.py is the sole retry/fallback owner for this
# operation (see PHASE_A_RETRY_NORMALIZATION_AND_RANKING_FALLBACK_PLAN.md).
# This module must not retry on its own.
# ============================================================


# ============================================================
# Validate ranking result (provider-independent: applies identically
# regardless of whether Gemini or the OpenAI fallback produced it)
# ============================================================

# Bounds for the diagnostic surfaced on a wrong-shape ranking result.
# Deliberately small: enough to tell "missing top_five" apart from
# "wrong item count" apart from "wrapped in a different key" without ever
# logging full news content or the full raw response.
_DIAGNOSTIC_MAX_TOP_LEVEL_KEYS = 10
_DIAGNOSTIC_MAX_KEY_LENGTH = 40


def _safe_top_level_keys(result):
    if not isinstance(result, dict):
        return []

    keys = sorted(str(key) for key in result.keys())[:_DIAGNOSTIC_MAX_TOP_LEVEL_KEYS]

    return [key[:_DIAGNOSTIC_MAX_KEY_LENGTH] for key in keys]


def validate_result(
    result,
    candidates,
    archive,
):
    if not isinstance(
        result,
        dict,
    ):
        raise RuntimeError(
            "Ranking result must be a JSON object."
        )

    top_five = result.get(
        "top_five"
    )

    if (
        not isinstance(top_five, list)
        or len(top_five) != 5
    ):
        actual_count = (
            len(top_five)
            if isinstance(top_five, list)
            else "UNKNOWN"
        )

        raise RuntimeError(
            "Ranking result must contain exactly five stories. "
            "EXPECTED_STORY_COUNT=5 "
            f"ACTUAL_STORY_COUNT={actual_count} "
            f"TOP_LEVEL_KEYS={_safe_top_level_keys(result)}"
        )

    valid_ids = set(
        range(
            1,
            len(candidates) + 1,
        )
    )

    selected_ids = []
    selected_urls = set()

    published_urls = (
        build_published_url_set(
            archive
        )
    )

    for story in top_five:

        story_id = story.get(
            "id"
        )

        if story_id not in valid_ids:

            raise RuntimeError(
                "Ranking result returned invalid "
                f"candidate id: {story_id}"
            )

        if story_id in selected_ids:

            raise RuntimeError(
                "Ranking result selected the same "
                "candidate more than once."
            )

        selected_ids.append(
            story_id
        )

        url = normalize_url(
            story.get(
                "url"
            )
        )

        if (
            url
            and url in published_urls
        ):

            raise RuntimeError(
                "Ranking result selected an already "
                "published URL: "
                f"{story.get('url')}"
            )

        if (
            url
            and url in selected_urls
        ):

            raise RuntimeError(
                "Ranking result selected duplicate "
                "URLs inside today's TOP 5."
            )

        if url:
            selected_urls.add(
                url
            )

        category = text(
            story.get(
                "category"
            )
        )

        for pattern in PROHIBITED_CATEGORY_PATTERNS:

            if pattern.search(category):

                raise RuntimeError(
                    "Selected story violates the sad/tragic-story "
                    f"guard (category: {category!r}): {story_id}"
                )

    recommended_id = result.get(
        "recommended_id"
    )

    if (
        recommended_id
        not in selected_ids
    ):

        raise RuntimeError(
            "recommended_id must be one "
            "of the TOP FIVE candidate ids."
        )


# ============================================================
# Main
# ============================================================

def main():

    print()
    print(
        "THE DAILY DUCK AI RANKING"
    )

    print(
        "=" * 60
    )

    # --------------------------------------------------------
    # Load
    # --------------------------------------------------------

    candidates = load_candidates()

    archive = load_archive()

    print(
        f"Raw candidates: "
        f"{len(candidates)}"
    )

    print(
        f"Published archive entries: "
        f"{len(archive)}"
    )

    # --------------------------------------------------------
    # Remove already-published URLs
    # --------------------------------------------------------

    (
        candidates,
        published_removed,
    ) = remove_already_published_urls(
        candidates,
        archive,
    )

    print(
        "Already-published URLs removed: "
        f"{len(published_removed)}"
    )

    for story in published_removed:

        print(
            "  BLOCKED PUBLISHED: "
            f"{text(story.get('title'))}"
        )

        print(
            "    "
            f"{text(story.get('url'))}"
        )

    # --------------------------------------------------------
    # Remove duplicate current candidates
    # --------------------------------------------------------

    (
        candidates,
        duplicate_removed,
    ) = remove_same_day_duplicates(
        candidates
    )

    print(
        "Same-day duplicate candidates removed: "
        f"{len(duplicate_removed)}"
    )

    for story in duplicate_removed:

        print(
            "  BLOCKED DUPLICATE: "
            f"{text(story.get('title'))}"
        )

    # --------------------------------------------------------
    # Need at least 5
    # --------------------------------------------------------

    if len(candidates) < 5:

        raise RuntimeError(
            "Fewer than 5 eligible candidates remain "
            "after duplicate filtering. "
            f"Remaining: {len(candidates)}. "
            "Collect more news before AI ranking."
        )

    print(
        f"Candidates sent to Gemini: "
        f"{len(candidates)}"
    )

    # --------------------------------------------------------
    # Prompt
    # --------------------------------------------------------

    prompt = build_prompt(
        candidates,
        archive,
    )

    # --------------------------------------------------------
    # Gemini primary, OpenAI fallback (llm_provider.py owns all
    # retry/fallback attempts and calls validate_result() itself so the
    # same provider-independent checks apply to either provider's output)
    # --------------------------------------------------------

    result, provider = llm_provider.generate_ranking(
        prompt,
        build_schema(),
        validate=lambda candidate_result: validate_result(
            candidate_result,
            candidates,
            archive,
        ),
    )

    print(
        f"Ranking result provider: {provider}"
    )

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    with open(
        OUTPUT_FILE,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            result,
            file,
            ensure_ascii=False,
            indent=2,
        )

    # --------------------------------------------------------
    # Console report
    # --------------------------------------------------------

    print()
    print(
        "TOP 5 DAILY DUCK AI PICKS"
    )

    print(
        "=" * 60
    )

    recommended_id = (
        result[
            "recommended_id"
        ]
    )

    science_like = {
        "science",
        "space",
        "technology",
        "astronomy",
        "research",
        "neuroscience",
    }

    science_count = 0

    for index, story in enumerate(
        result[
            "top_five"
        ],
        start=1,
    ):

        marker = ""

        if (
            story["id"]
            == recommended_id
        ):

            marker = (
                "  <-- RECOMMENDED"
            )

        category = text(
            story.get(
                "category"
            )
        )

        if (
            category.lower()
            in science_like
        ):
            science_count += 1

        print()

        print(
            f"{index}. "
            f"[{story['total_score']}/100] "
            f"{story['title']}"
            f"{marker}"
        )

        print(
            f"   Category: "
            f"{category}"
        )

        print(
            f"   Source: "
            f"{story['source']}"
        )

        print(
            f"   Happy "
            f"{story['happiness']}/10 | "
            f"Hope "
            f"{story['hope']}/10 | "
            f"Interest "
            f"{story['general_interest']}/10"
        )

        print(
            f"   Surprise "
            f"{story['surprise']}/10 | "
            f"Duck Visual "
            f"{story['duck_visual']}/10"
        )

        print(
            f"   Broad Appeal "
            f"{story['broad_appeal']}/10 | "
            f"Archive Novelty "
            f"{story['novelty_vs_archive']}/10"
        )

        print(
            f"   Reason: "
            f"{story['reason']}"
        )

        print(
            f"   URL: "
            f"{story['url']}"
        )

    # --------------------------------------------------------
    # Diversity report
    # --------------------------------------------------------

    print()
    print(
        "CATEGORY CHECK"
    )

    print(
        "=" * 60
    )

    print(
        "Science / space / technology "
        f"TOP5 count: {science_count}"
    )

    if science_count <= 1:

        print(
            "Diversity target satisfied."
        )

    else:

        print(
            "NOTE: More than one science/technical "
            "story was selected."
        )

        print(
            "This is allowed only when the candidate "
            "pool did not contain enough strong "
            "non-science stories."
        )

    # --------------------------------------------------------
    # Recommendation
    # --------------------------------------------------------

    print()
    print(
        "TODAY'S RECOMMENDATION"
    )

    print(
        "=" * 60
    )

    print(
        result[
            "recommended_reason"
        ]
    )

    print()

    print(
        f"Saved to {OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()
