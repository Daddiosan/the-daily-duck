import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import json
import re

# ============================================================
# The Daily Duck - Phase 1
# Free RSS-based happy-news candidate collector
# ============================================================


# ------------------------------------------------------------
# RSS sources
# ------------------------------------------------------------

RSS_FEEDS = [
    {
        "name": "NASA",
        "url": "https://www.nasa.gov/feed/",
    },
    {
        "name": "NASA JPL",
        "url": "https://www.jpl.nasa.gov/feeds/news/",
    },
    {
        "name": "ScienceDaily All",
        "url": "https://www.sciencedaily.com/rss/all.xml",
    },
    {
        "name": "ScienceDaily Society",
        "url": "https://www.sciencedaily.com/rss/top/society.xml",
    },
    {
        "name": "ScienceDaily Environment",
        "url": "https://www.sciencedaily.com/rss/top/environment.xml",
    },
    {
        "name": "ScienceDaily Technology",
        "url": "https://www.sciencedaily.com/rss/top/technology.xml",
    },
    {
        "name": "ScienceDaily Offbeat",
        "url": "https://www.sciencedaily.com/rss/strange_offbeat.xml",
    },
    {
        "name": "Phys.org",
        "url": "https://phys.org/rss-feed/",
    },
]


# ------------------------------------------------------------
# Positive / uplifting words
# ------------------------------------------------------------

HAPPY_WORDS = {
    "happy": 5,
    "happiness": 5,
    "joy": 5,
    "joyful": 5,
    "celebrate": 4,
    "celebrates": 4,
    "celebration": 4,
    "success": 4,
    "successful": 4,
    "rescue": 4,
    "rescued": 4,
    "recovery": 4,
    "recover": 4,
    "recovered": 4,
    "saved": 4,
    "save": 3,
    "hope": 4,
    "hopeful": 5,
    "breakthrough": 4,
    "discovery": 3,
    "discover": 3,
    "discovered": 3,
    "new": 1,
    "first": 2,
    "record": 2,
    "returns": 2,
    "returned": 2,
    "restored": 4,
    "restoration": 4,
    "protect": 3,
    "protected": 3,
    "conservation": 4,
    "improves": 3,
    "improved": 3,
    "improvement": 3,
    "helps": 3,
    "helping": 3,
    "friendship": 5,
    "community": 3,
    "baby": 4,
    "babies": 4,
    "born": 4,
    "birth": 4,
    "cute": 4,
    "play": 2,
    "playing": 2,
    "reunited": 5,
    "reunion": 5,
    "award": 2,
    "wins": 3,
    "winner": 3,
    "achievement": 3,
    "achieves": 3,
    "inspiring": 4,
    "inspiration": 4,
    "kindness": 5,
    "kind": 3,
    "gift": 3,
    "donation": 3,
    "volunteer": 3,
    "volunteers": 3,
    "adopted": 4,
    "adoption": 4,
}


# ------------------------------------------------------------
# Negative / tragedy-heavy words
# ------------------------------------------------------------

NEGATIVE_WORDS = {
    "dead": -10,
    "death": -10,
    "dies": -10,
    "died": -10,
    "killed": -10,
    "killing": -10,
    "war": -10,
    "attack": -9,
    "attacks": -9,
    "murder": -10,
    "shooting": -10,
    "bomb": -10,
    "explosion": -8,
    "crash": -8,
    "disaster": -8,
    "victims": -8,
    "victim": -8,
    "tragedy": -10,
    "tragic": -10,
    "disease": -5,
    "cancer": -5,
    "outbreak": -7,
    "famine": -10,
    "crisis": -6,
    "threat": -5,
    "danger": -5,
    "violent": -8,
    "violence": -8,
    "injured": -7,
    "injury": -5,
    "funeral": -10,
}


# ------------------------------------------------------------
# Visual potential
# These words make it easier to create a fun Daily Duck image.
# ------------------------------------------------------------

VISUAL_WORDS = {
    "animal": 2,
    "animals": 2,
    "bird": 2,
    "birds": 2,
    "dog": 3,
    "dogs": 3,
    "puppy": 3,
    "cat": 3,
    "cats": 3,
    "kitten": 3,
    "penguin": 3,
    "whale": 3,
    "dolphin": 3,
    "shark": 3,
    "turtle": 3,
    "bear": 3,
    "elephant": 3,
    "panda": 3,
    "space": 2,
    "moon": 2,
    "mars": 2,
    "ocean": 2,
    "forest": 2,
    "flower": 2,
    "flowers": 2,
    "robot": 2,
    "robots": 2,
    "food": 2,
    "music": 2,
    "festival": 3,
    "art": 2,
    "sports": 2,
    "school": 2,
    "children": 2,
    "kids": 2,
}


# ------------------------------------------------------------
# Download RSS
# ------------------------------------------------------------

def download_feed(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "TheDailyDuck/1.0"
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=20
    ) as response:
        return response.read()


# ------------------------------------------------------------
# Clean HTML / whitespace
# ------------------------------------------------------------

def clean_text(text):
    text = re.sub(
        r"<[^>]+>",
        " ",
        text or ""
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


# ------------------------------------------------------------
# Collect stories from one feed
# ------------------------------------------------------------

def collect_feed(source):
    print(
        f"Checking: {source['name']}"
    )

    try:
        data = download_feed(
            source["url"]
        )

        root = ET.fromstring(data)

        stories = []

        for item in root.findall(".//item")[:15]:

            title = clean_text(
                item.findtext(
                    "title",
                    ""
                )
            )

            link = clean_text(
                item.findtext(
                    "link",
                    ""
                )
            )

            published = clean_text(
                item.findtext(
                    "pubDate",
                    ""
                )
            )

            description = clean_text(
                item.findtext(
                    "description",
                    ""
                )
            )

            if title and link:

                stories.append(
                    {
                        "source": source["name"],
                        "title": title,
                        "url": link,
                        "published": published,
                        "description": description,
                    }
                )

        print(
            f"  Found {len(stories)} stories"
        )

        return stories

    except Exception as error:

        print(
            f"  ERROR: {error}"
        )

        # One broken RSS feed must not stop
        # the entire Daily Duck automation.
        return []


# ------------------------------------------------------------
# Score one story
# ------------------------------------------------------------

def score_story(story):

    text = (
        f"{story['title']} "
        f"{story.get('description', '')}"
    ).lower()

    happy_score = 0
    negative_score = 0
    visual_score = 0

    matched_happy = []
    matched_negative = []
    matched_visual = []

    # Positive words
    for word, value in HAPPY_WORDS.items():

        if re.search(
            rf"\b{re.escape(word)}\b",
            text
        ):
            happy_score += value
            matched_happy.append(word)

    # Negative words
    for word, value in NEGATIVE_WORDS.items():

        if re.search(
            rf"\b{re.escape(word)}\b",
            text
        ):
            negative_score += value
            matched_negative.append(word)

    # Visual potential
    for word, value in VISUAL_WORDS.items():

        if re.search(
            rf"\b{re.escape(word)}\b",
            text
        ):
            visual_score += value
            matched_visual.append(word)

    # Reliable-source bonus.
    # This is deliberately small:
    # source should not overpower "happy" quality.

    source_bonus = {
        "NASA": 3,
        "NASA JPL": 3,
        "ScienceDaily All": 2,
        "ScienceDaily Society": 2,
        "ScienceDaily Environment": 2,
        "ScienceDaily Technology": 2,
        "ScienceDaily Offbeat": 2,
        "Phys.org": 2,
    }.get(
        story["source"],
        0
    )

    total_score = (
        happy_score
        + negative_score
        + visual_score
        + source_bonus
    )

    story["score"] = total_score

    story["happy_score"] = (
        happy_score
    )

    story["negative_score"] = (
        negative_score
    )

    story["visual_score"] = (
        visual_score
    )

    story["source_bonus"] = (
        source_bonus
    )

    story["matched_happy"] = (
        matched_happy
    )

    story["matched_negative"] = (
        matched_negative
    )

    story["matched_visual"] = (
        matched_visual
    )

    return story


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main():

    candidates = []

    print()
    print(
        "THE DAILY DUCK NEWS COLLECTION"
    )

    print(
        "=" * 50
    )

    # Collect all RSS feeds
    for source in RSS_FEEDS:

        candidates.extend(
            collect_feed(source)
        )

    # --------------------------------------------------------
    # Remove duplicate URLs
    # --------------------------------------------------------

    unique = {}

    for story in candidates:

        unique[
            story["url"]
        ] = story

    candidates = list(
        unique.values()
    )

    # --------------------------------------------------------
    # Score all stories
    # --------------------------------------------------------

    scored_candidates = [
        score_story(story)
        for story in candidates
    ]

    # --------------------------------------------------------
    # Remove strongly negative stories
    #
    # -8 or below means tragedy / violence etc.
    # --------------------------------------------------------

    filtered_candidates = [

        story

        for story
        in scored_candidates

        if story["negative_score"] > -8
    ]

    # --------------------------------------------------------
    # Highest Daily Duck score first
    # --------------------------------------------------------

    ranked_candidates = sorted(
        filtered_candidates,
        key=lambda story: story["score"],
        reverse=True,
    )

    # --------------------------------------------------------
    # Top 5 for editorial review
    # --------------------------------------------------------

    shortlist = (
        ranked_candidates[:5]
    )

    # --------------------------------------------------------
    # JSON output
    # --------------------------------------------------------

    output = {

        "generated_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "candidate_count":
            len(candidates),

        "filtered_count":
            len(filtered_candidates),

        "shortlist":
            shortlist,

        "all_candidates":
            ranked_candidates,
    }

    with open(
        "news_candidates.json",
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            output,
            file,
            ensure_ascii=False,
            indent=2
        )

    # --------------------------------------------------------
    # GitHub Actions log
    # --------------------------------------------------------

    print()

    print(
        f"Total candidates: "
        f"{len(candidates)}"
    )

    print(
        f"After filtering: "
        f"{len(filtered_candidates)}"
    )

    print()

    print(
        "TOP 5 DAILY DUCK CANDIDATES"
    )

    print(
        "=" * 50
    )

    if not shortlist:

        print(
            "No suitable candidates found."
        )

    else:

        for index, story in enumerate(
            shortlist,
            start=1
        ):

            print()

            print(
                f"{index}. "
                f"[Score {story['score']}] "
                f"{story['title']}"
            )

            print(
                f"   Source: "
                f"{story['source']}"
            )

            print(
                f"   Happy: "
                f"{story['happy_score']} | "
                f"Negative: "
                f"{story['negative_score']} | "
                f"Visual: "
                f"{story['visual_score']}"
            )

            print(
                f"   URL: "
                f"{story['url']}"
            )

    print()

    print(
        "Saved to news_candidates.json"
    )


# ------------------------------------------------------------
# Run
# ------------------------------------------------------------

if __name__ == "__main__":
    main()
