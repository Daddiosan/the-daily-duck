import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import json

# The Daily Duck - Phase 1
# Free RSS-based news candidate collector

RSS_FEEDS = [
    {
        "name": "NASA",
        "url": "https://www.nasa.gov/feed/",
    },
    {
        "name": "ScienceDaily",
        "url": "https://www.sciencedaily.com/rss/top/science.xml",
    },
    {
        "name": "Phys.org",
        "url": "https://phys.org/rss-feed/",
    },
]


def download_feed(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "TheDailyDuck/1.0"
        },
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def collect_feed(source):
    print(f"Checking: {source['name']}")

    try:
        data = download_feed(source["url"])
        root = ET.fromstring(data)

        stories = []

        for item in root.findall(".//item")[:10]:
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            published = item.findtext("pubDate", "").strip()

            if title and link:
                stories.append(
                    {
                        "source": source["name"],
                        "title": title,
                        "url": link,
                        "published": published,
                    }
                )

        print(f"  Found {len(stories)} stories")
        return stories

    except Exception as error:
        print(f"  ERROR: {error}")
        return []


def main():
    candidates = []

    for source in RSS_FEEDS:
        candidates.extend(collect_feed(source))

    # Remove duplicate URLs
    unique = {}

    for story in candidates:
        unique[story["url"]] = story

    candidates = list(unique.values())

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }

    with open("news_candidates.json", "w", encoding="utf-8") as file:
        json.dump(output, file, ensure_ascii=False, indent=2)

    print()
    print(f"Total candidates: {len(candidates)}")
    print("Saved to news_candidates.json")


if __name__ == "__main__":
    main()
