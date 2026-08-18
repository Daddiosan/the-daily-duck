import os
import json
import re
import time
import socket
import urllib.request
import urllib.error

from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import (
    parse_qsl,
    urlencode,
    urlsplit,
    urlunsplit,
)


INPUT_FILE = "news_candidates.json"
OUTPUT_FILE = "ai_ranked_news.json"
ARCHIVE_FILE = Path("data/archive.json")

MODEL = "gemini-3.6-flash"

# ------------------------------------------------------------
# Gemini retry settings
# ------------------------------------------------------------

MAX_GEMINI_ATTEMPTS = 4

# Retry after:
# attempt 1 -> 10 sec
# attempt 2 -> 30 sec
# attempt 3 -> 60 sec
RETRY_DELAYS = [
    10,
    30,
    60,
]

RETRYABLE_HTTP_CODES = {
    429,
    500,
    502,
    503,
    504,
}

# ------------------------------------------------------------
# Duplicate/history settings
# ------------------------------------------------------------

MAX_HISTORY_ITEMS = 60
CURRENT_TITLE_SIMILARITY = 0.92


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
# Single Gemini request
# ============================================================

def call_gemini_once(
    prompt,
):
    api_key = os.environ.get(
        "GEMINI_API_KEY"
    )

    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not configured."
        )

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{MODEL}:generateContent"
        f"?key={api_key}"
    )

    body = {

        "contents": [
            {
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ],

        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "responseJsonSchema": build_schema(),
        },
    }

    request = urllib.request.Request(
        url,

        data=json.dumps(
            body
        ).encode(
            "utf-8"
        ),

        headers={
            "Content-Type": "application/json"
        },

        method="POST",
    )

    with urllib.request.urlopen(
        request,
        timeout=120,
    ) as response:

        response_data = json.loads(
            response.read().decode(
                "utf-8"
            )
        )

    try:
        result_text = (
            response_data[
                "candidates"
            ][0][
                "content"
            ][
                "parts"
            ][0][
                "text"
            ]
        )

    except (
        KeyError,
        IndexError,
        TypeError,
    ) as exc:

        raise RuntimeError(
            "Unexpected Gemini response structure: "
            f"{json.dumps(response_data, ensure_ascii=False)[:1500]}"
        ) from exc

    return json.loads(
        result_text
    )


# ============================================================
# Gemini automatic retry
# ============================================================

def call_gemini(
    prompt,
):
    """
    Retry temporary Gemini/API/network failures.

    Attempt 1
      fail -> wait 10 sec

    Attempt 2
      fail -> wait 30 sec

    Attempt 3
      fail -> wait 60 sec

    Attempt 4
      fail -> raise error

    Permanent HTTP errors such as 400/401/403 are not retried.
    """

    last_error = None

    for attempt in range(
        1,
        MAX_GEMINI_ATTEMPTS + 1,
    ):

        print()
        print(
            f"Gemini request attempt "
            f"{attempt}/{MAX_GEMINI_ATTEMPTS}"
        )

        try:

            result = call_gemini_once(
                prompt
            )

            if attempt > 1:

                print(
                    "Gemini retry succeeded."
                )

            return result

        # ----------------------------------------------------
        # HTTP errors
        # ----------------------------------------------------

        except urllib.error.HTTPError as error:

            details = error.read().decode(
                "utf-8",
                errors="replace",
            )

            last_error = RuntimeError(
                f"Gemini API HTTP "
                f"{error.code}: {details}"
            )

            print(
                f"Gemini HTTP error: "
                f"{error.code}"
            )

            if (
                error.code
                not in RETRYABLE_HTTP_CODES
            ):

                print(
                    "This HTTP error is not retryable."
                )

                raise last_error

            print(
                "Temporary Gemini/API error detected."
            )

        # ----------------------------------------------------
        # Network errors
        # ----------------------------------------------------

        except (
            urllib.error.URLError,
            TimeoutError,
            socket.timeout,
        ) as error:

            last_error = error

            print(
                "Temporary network/API error:"
            )

            print(
                str(error)
            )

        # ----------------------------------------------------
        # Stop after final attempt
        # ----------------------------------------------------

        if (
            attempt
            >= MAX_GEMINI_ATTEMPTS
        ):
            break

        delay = RETRY_DELAYS[
            attempt - 1
        ]

        print(
            f"Retrying in {delay} seconds..."
        )

        time.sleep(
            delay
        )

    raise RuntimeError(
        "Gemini ranking failed after "
        f"{MAX_GEMINI_ATTEMPTS} attempts. "
        f"Last error: {last_error}"
    )


# ============================================================
# Validate Gemini result
# ============================================================

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
            "Gemini result must be an object."
        )

    top_five = result.get(
        "top_five"
    )

    if (
        not isinstance(top_five, list)
        or len(top_five) != 5
    ):
        raise RuntimeError(
            "Gemini must return exactly five stories."
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
                "Gemini returned invalid "
                f"candidate id: {story_id}"
            )

        if story_id in selected_ids:

            raise RuntimeError(
                "Gemini selected the same "
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
                "Gemini selected an already "
                "published URL: "
                f"{story.get('url')}"
            )

        if (
            url
            and url in selected_urls
        ):

            raise RuntimeError(
                "Gemini selected duplicate "
                "URLs inside today's TOP 5."
            )

        if url:
            selected_urls.add(
                url
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
    # Gemini + automatic retry
    # --------------------------------------------------------

    result = call_gemini(
        prompt
    )

    # --------------------------------------------------------
    # Validate
    # --------------------------------------------------------

    validate_result(
        result,
        candidates,
        archive,
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
