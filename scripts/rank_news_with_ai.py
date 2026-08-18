import os
import json
import re
import urllib.request
import urllib.error
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import (
    urlsplit,
    urlunsplit,
    parse_qsl,
    urlencode,
)

INPUT_FILE = "news_candidates.json"
OUTPUT_FILE = "ai_ranked_news.json"
ARCHIVE_FILE = Path("data/archive.json")

MODEL = "gemini-3.6-flash"

# Geminiへ渡す過去掲載履歴の最大件数。
# archive.jsonは新しい記事が先頭なので、
# 最近の記事を優先して比較する。
MAX_HISTORY_ITEMS = 60

# 同じ候補集合内で、ほぼ同一タイトルを
# Python側で重複とみなす基準。
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

    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip()


def normalize_url(value):
    """
    Normalize URLs so tracking parameters do not make
    the same article look different.
    """

    value = text(value)

    if not value:
        return ""

    try:
        parts = urlsplit(value)

        scheme = (
            parts.scheme.lower()
            or "https"
        )

        netloc = (
            parts.netloc.lower()
        )

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
# Load today's candidates
# ============================================================

def load_candidates():
    with open(
        INPUT_FILE,
        "r",
        encoding="utf-8",
    ) as file:
        data = json.load(file)

    # Current Daily Duck collector format
    if "candidates" in data:
        candidates = data["candidates"]

    # Compatibility with previous collector versions
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
# Load published Daily Duck history
# ============================================================

def load_archive():
    if not ARCHIVE_FILE.exists():
        print(
            "WARNING: data/archive.json does not exist."
        )
        print(
            "Duplicate history checking will use "
            "today's candidates only."
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

    result = []

    for item in archive:
        if not isinstance(
            item,
            dict,
        ):
            continue

        # Only real published stories become exclusion history.
        if item.get(
            "published",
            True,
        ) is False:
            continue

        result.append(item)

    return result


# ============================================================
# Exact published URL exclusion
# ============================================================

def build_published_url_set(
    archive,
):
    urls = set()

    for item in archive:
        url = normalize_url(
            item.get("sourceUrl")
        )

        if url:
            urls.add(url)

    return urls


def remove_already_published_urls(
    candidates,
    archive,
):
    """
    Hard exclusion.

    If the source URL has already been published by
    The Daily Duck, do not even send it to Gemini.
    """

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
            story.get("url")
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
# Same-day duplicate cleanup
# ============================================================

def remove_same_day_duplicates(
    candidates,
):
    """
    Prevent today's candidate pool itself from containing
    several versions of essentially the same headline.

    URL identity is checked first.
    Strong title similarity is checked second.

    Semantic duplicates from different publishers are
    handled later by Gemini.
    """

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
            story.get("url")
        )

        title = normalize_title(
            story.get("title")
        )

        if url and url in seen_urls:
            removed.append(
                story
            )
            continue

        is_duplicate_title = False

        if title:
            for previous_title in seen_titles:

                ratio = SequenceMatcher(
                    None,
                    title,
                    previous_title,
                ).ratio()

                if (
                    ratio
                    >= CURRENT_TITLE_SIMILARITY
                ):
                    is_duplicate_title = True
                    break

        if is_duplicate_title:
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
# Build publication history for Gemini
# ============================================================

def build_history_for_prompt(
    archive,
):
    """
    archive.json contains Daily Duck titles rather than
    necessarily the original publisher headline.

    Therefore semantic duplicate detection gets:
      - source URL
      - publisher/source
      - English/Japanese story summary
      - archive summary
      - Daily Duck title
    """

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
# Gemini prompt
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
                    story.get("source")
                ),
                "title": text(
                    story.get("title")
                ),
                "description": text(
                    story.get(
                        "description"
                    )
                )[:700],
                "url": text(
                    story.get("url")
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
THE DAILY DUCK MISSION
============================================================

Choose news that leaves an ordinary reader feeling a little:

- happier
- warmer
- more hopeful
- amused
- delighted
- inspired
- pleasantly surprised
- positively curious

The Daily Duck is NOT a science publication.

It is NOT a technology publication.

It is NOT a research-news publication.

It is a cheerful general-interest daily publication.

There is NO preferred subject category.

============================================================
MOST IMPORTANT EDITORIAL CHANGE
============================================================

Recent editions have contained too many:

- science stories
- research papers
- neuroscience stories
- space stories
- astronomy stories
- technical discoveries

Correct that bias.

Scientific importance or academic novelty alone is NOT
a Daily Duck quality.

A major scientific breakthrough can rank BELOW:

- a delightful animal story
- an amusing everyday story
- an inspiring human story
- a surprising food story
- a fun cultural story
- a heartwarming community story
- an unusual sports story
- a travel or place story
- a creativity story
- a charming conservation success
- a quirky world event

if the non-science story is more enjoyable for ordinary readers.

============================================================
CATEGORY DIVERSITY
============================================================

The TOP FIVE should feel like a fun mixed front page,
not five variations of the same subject.

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
- funny / quirky news
- positive environment
- conservation
- nature
- children / family
- achievements
- science
- space
- technology
- other positive general-interest stories

IMPORTANT:

Normally select NO MORE THAN ONE story from the combined
science / academic research / space / astronomy /
technology category in the TOP FIVE.

Only exceed that limit if there are genuinely not enough
suitable high-quality non-science candidates to create
five good Daily Duck choices.

Do not artificially choose a poor story merely for diversity.

Quality still matters.

But when two stories are similarly good, strongly prefer
the category that is NOT already represented in the TOP FIVE.

============================================================
NO REPEATS
============================================================

Below you will receive PUBLISHED HISTORY from
The Daily Duck archive.

A previously published story MUST NOT be selected again.

Reject a candidate if it is:

1. the same article,
2. the same source URL,
3. the same underlying event reported by another publisher,
4. a rewritten version of an already-used story,
5. a minor update that does not create a genuinely new story.

A genuine major new development in an old subject MAY be used,
but only if the new development itself is clearly the story.

Example:

Yesterday:
"A zoo welcomes a baby panda."

Today:
"Another website reports that the zoo welcomed a baby panda."

=> DUPLICATE. Reject.

But:

Three months later:
"The panda takes its first steps in public."

=> Can be a genuinely new story.

============================================================
NO DUPLICATES INSIDE TODAY'S TOP FIVE
============================================================

Different publishers may report the same event.

Do NOT put multiple versions of the same event in the TOP FIVE.

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

A recovery or conservation story may still qualify if its
dominant emotional effect is clearly hopeful and uplifting.

============================================================
WHAT SHOULD WIN
============================================================

Prefer stories that have:

- immediate emotional appeal
- broad accessibility
- a strong "I want to tell someone this" quality
- charm
- warmth
- surprise
- playful visual potential
- a clear story that needs little specialist knowledge
- freshness

Penalize:

- technical significance without emotional appeal
- specialist-only interest
- press releases whose main value is academic importance
- stories that require lengthy explanation before becoming fun
- repetitive science / space / research themes
- events already published by The Daily Duck

============================================================
SCORING
============================================================

Score each selected story:

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

The exact arithmetic does not have to equal a simple sum.
Use total_score as the overall Daily Duck editorial score.

============================================================
CATEGORY FIELD
============================================================

Assign ONE concise category to every selected story.

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
SELECTION RULES
============================================================

Return exactly the BEST FIVE suitable stories.

The five should be meaningfully varied when the candidate pool
allows it.

Choose exactly ONE recommended story from those five.

Today's recommended story should usually be the story that is
most:

- instantly enjoyable
- broadly appealing
- memorable
- visually fun

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
# Gemini API
# ============================================================

def call_gemini(
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

    schema = {
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
            "responseJsonSchema": schema,
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

    try:
        with urllib.request.urlopen(
            request,
            timeout=120,
        ) as response:

            response_data = json.loads(
                response.read().decode(
                    "utf-8"
                )
            )

    except urllib.error.HTTPError as error:

        details = error.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"Gemini API HTTP "
            f"{error.code}: {details}"
        )

    text_result = (
        response_data["candidates"][0]
        ["content"]["parts"][0]["text"]
    )

    return json.loads(
        text_result
    )


# ============================================================
# Result validation
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
            "Gemini must return exactly "
            "five stories."
        )

    valid_ids = set(
        range(
            1,
            len(candidates) + 1,
        )
    )

    selected_ids = []

    published_urls = (
        build_published_url_set(
            archive
        )
    )

    selected_urls = set()

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
            story.get("url")
        )

        # Second safety block:
        # even if Gemini somehow returned a published URL,
        # stop the ranking result here.
        if (
            url
            and url in published_urls
        ):
            raise RuntimeError(
                "Gemini selected an already "
                "published source URL: "
                f"{story.get('url')}"
            )

        if url and url in selected_urls:
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
            "of the TOP FIVE story ids."
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
    # Hard block: exact published URLs
    # --------------------------------------------------------

    candidates, published_removed = (
        remove_already_published_urls(
            candidates,
            archive,
        )
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
    # Same-day exact/near-exact cleanup
    # --------------------------------------------------------

    candidates, same_day_removed = (
        remove_same_day_duplicates(
            candidates
        )
    )

    print(
        "Same-day duplicate candidates removed: "
        f"{len(same_day_removed)}"
    )

    for story in same_day_removed:

        print(
            "  BLOCKED DUPLICATE: "
            f"{text(story.get('title'))}"
        )

    # --------------------------------------------------------
    # We need at least five eligible candidates
    # --------------------------------------------------------

    if len(candidates) < 5:
        raise RuntimeError(
            "Fewer than 5 eligible news candidates remain "
            "after duplicate filtering. "
            f"Remaining: {len(candidates)}. "
            "Collect more news before AI ranking."
        )

    print(
        f"Candidates sent to Gemini: "
        f"{len(candidates)}"
    )

    # --------------------------------------------------------
    # Gemini ranking
    # --------------------------------------------------------

    prompt = build_prompt(
        candidates,
        archive,
    )

    result = call_gemini(
        prompt
    )

    validate_result(
        result,
        candidates,
        archive,
    )

    # --------------------------------------------------------
    # Save output
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
        result["recommended_id"]
    )

    science_like = {
        "science",
        "space",
        "technology",
        "astronomy",
        "research",
    }

    science_count = 0

    for index, story in enumerate(
        result["top_five"],
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
            story.get("category")
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

    print()
    print(
        "CATEGORY CHECK"
    )
    print(
        "=" * 60
    )

    print(
        "Science/space/technology "
        f"TOP5 count: {science_count}"
    )

    if science_count > 1:

        print(
            "NOTE: More than one technical/science "
            "story was selected."
        )

        print(
            "This is permitted only when Gemini judged "
            "that there were not enough suitable "
            "non-science candidates."
        )

    else:

        print(
            "Diversity target satisfied."
        )

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
