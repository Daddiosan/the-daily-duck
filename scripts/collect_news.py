import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import json
import re

# The Daily Duck - Phase 1
# Free RSS-based happy-news candidate collector

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

HAPPY_WORDS = {
    "happy": 5,
    "joy": 5,
    "celebrate": 4,
    "celebrates": 4,
    "celebration": 4,
    "success": 4,
    "successful": 4,
    "rescue": 4,
    "rescued": 4,
    "recovery": 4,
    "recover": 4,
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
    "reunited": 5,
    "reunion": 5,
    "award": 2,
    "wins": 3,
    "winner": 3,
    "achievement": 3,
}

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
}

VISUAL_WORDS = {
    "animal": 2,
    "animals": 2,
    "bird": 2,
    "birds": 2,
    "dog": 3,
    "dogs": 3,
    "cat": 3,
    "cats": 3,
    "penguin": 3,
    "whale": 3,
    "dolphin": 3,
    "shark": 3,
    "turtle": 3,
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
}


def download_feed(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "TheDailyDuck/1.0"},
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def clean_text(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def collect_feed(source):
    print(f"Checking: {source['name']}")

    try:
        data = download_feed(source["url"])
        root = ET.fromstring(data)

        stories = []

        for item in root.findall(".//item")[:15]:
            title = clean_text(item.findtext("title", ""))
            link = clean_text(item.findtext("link", ""))
            published = clean_text(item.findtext("pubDate", ""))
            description = clean_text(item.findtext("description", ""))

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

        print(f"  Found {len(stories)} stories")
        return stories

    except Exception as error:
        print(f"  ERROR: {error}")
        return []


def score_story(story):
    text = f"{story['title']} {story.get('description', '')}".lower()

    happy_score = 0
    negative_score = 0
    visual_score = 0

    matched_happy = []
    matched_negative = []
    matched_visual = []

    for word, value in HAPPY_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", text):
            happy_score += value
            matched_happy.append(word)

    for word, value in NEGATIVE_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", text):
            negative_score += value
            matched_negative.append(word)

    for word, value in VISUAL_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", text):
            visual_score += value
            matched_visual.append(word)

source_bonus = {
    "NASA": 3,
    "NASA JPL": 3,
    "ScienceDaily All": 2,
    "ScienceDaily Society": 2,
    "ScienceDaily Environment": 2,
    "ScienceDaily Technology": 2,
    "ScienceDaily Offbeat": 2,
    "Phys.org": 2,
}.get(story["source"], 0)

    total_score = happy_score + negative_score + visual_score + source_bonus

    story["score"] = total_score
    story["happy_score"] = happy_score
    story["negative_score"] = negative_score
    story["visual_score"] = visual_score
    story["matched_happy"] = matched_happy
    story["matched_negative"] = matched_negative
    story["matched_visual"] = matched_visual

    return story


def main():
    candidates = []

    for source in RSS_FEEDS:
        candidates.extend(collect_feed(source))

    unique = {}

    for story in candidates:
        unique[story["url"]] = story

    candidates = list(unique.values())

    scored_candidates = [score_story(story) for story in candidates]

    filtered_candidates = [
        story
        for story in scored_candidates
        if story["negative_score"] > -8
    ]

    ranked_candidates = sorted(
        filtered_candidates,
        key=lambda story: story["score"],
        reverse=True,
    )

    shortlist = ranked_candidates[:5]

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(candidates),
        "filtered_count": len(filtered_candidates),
        "shortlist": shortlist,
        "all_candidates": ranked_candidates,
    }

    with open("news_candidates.json", "w", encoding="utf-8") as file:
        json.dump(output, file, ensure_ascii=False, indent=2)

    print()
    print(f"Total candidates: {len(candidates)}")
    print(f"After filtering: {len(filtered_candidates)}")
    print()
    print("TOP 5 DAILY DUCK CANDIDATES")
    print("=" * 50)

    for index, story in enumerate(shortlist, start=1):
        print(
            f"{index}. [{story['score']}] "
            f"{story['title']} — {story['source']}"
        )

    print()
    print("Saved to news_candidates.json")


if __name__ == "__main__":
    main()
