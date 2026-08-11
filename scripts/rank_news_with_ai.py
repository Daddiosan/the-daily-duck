import os
import json
import urllib.request
import urllib.error

INPUT_FILE = "news_candidates.json"
OUTPUT_FILE = "ai_ranked_news.json"
MODEL = "gemini-3.6-flash"


def load_candidates():
    with open(INPUT_FILE, "r", encoding="utf-8") as file:
        data = json.load(file)

    # Current Daily Duck collector format
    if "candidates" in data:
        return data["candidates"]

    # Compatibility with previous collector versions
    if "all_candidates" in data:
        return data["all_candidates"]

    if "shortlist" in data:
        return data["shortlist"]

    if "filtered" in data:
        return data["filtered"]

    raise RuntimeError(
        "No candidate list found in news_candidates.json. "
        f"Available keys: {list(data.keys())}"
    )


def build_prompt(candidates):
    stories = []

    for index, story in enumerate(candidates, start=1):
        stories.append(
            {
                "id": index,
                "source": story["source"],
                "title": story["title"],
                "description": story.get("description", "")[:700],
                "url": story["url"],
                "published": story.get("published", ""),
            }
        )

    candidate_json = json.dumps(
        stories,
        ensure_ascii=False,
        indent=2,
    )

    return f"""
You are the editorial ranking assistant for The Daily Duck.

MISSION:
Choose news that leaves ordinary readers feeling a little
happier, more hopeful, amused, delighted, inspired, or warmly curious.

There is NO preferred subject category.

A great Daily Duck story can be about people, animals, communities,
culture, food, sport, nature, science, technology, children,
kindness, creativity, conservation, unusual achievements,
funny events, discoveries, or anything else.

IMPORTANT:
Scientific or technical importance alone is NOT a reason to rank
a story highly.

A simple heartwarming story should beat a major technical
breakthrough when the heartwarming story is more enjoyable
for ordinary readers.

Avoid stories whose main emotional focus is:
death, tragedy, war, crime, political conflict, disaster,
fear, severe illness, suffering, or outrage.

Evaluate candidates using:

- happiness: 0-10
- hope: 0-10
- general_interest: 0-10
- surprise: 0-10
- duck_visual: 0-10
- source_quality: 0-10
- freshness: 0-10

Return the BEST FIVE stories.

total_score must be 0-100.

Also choose exactly ONE recommended story as today's
best Daily Duck candidate.

Return only the requested JSON structure.

CANDIDATES:

{candidate_json}
"""


def call_gemini(prompt):
    api_key = os.environ.get("GEMINI_API_KEY")

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
                        "id": {"type": "integer"},
                        "title": {"type": "string"},
                        "source": {"type": "string"},
                        "url": {"type": "string"},
                        "happiness": {"type": "integer"},
                        "hope": {"type": "integer"},
                        "general_interest": {"type": "integer"},
                        "surprise": {"type": "integer"},
                        "duck_visual": {"type": "integer"},
                        "source_quality": {"type": "integer"},
                        "freshness": {"type": "integer"},
                        "total_score": {"type": "integer"},
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "id",
                        "title",
                        "source",
                        "url",
                        "happiness",
                        "hope",
                        "general_interest",
                        "surprise",
                        "duck_visual",
                        "source_quality",
                        "freshness",
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
        data=json.dumps(body).encode("utf-8"),
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
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as error:
        details = error.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"Gemini API HTTP {error.code}: {details}"
        )

    text = (
        response_data["candidates"][0]
        ["content"]["parts"][0]["text"]
    )

    return json.loads(text)


def main():
    print()
    print("THE DAILY DUCK AI RANKING")
    print("=" * 50)

    candidates = load_candidates()

    print(
        f"Candidates sent to Gemini: {len(candidates)}"
    )

    prompt = build_prompt(candidates)

    result = call_gemini(prompt)

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

    print()
    print("TOP 5 DAILY DUCK AI PICKS")
    print("=" * 50)

    recommended_id = result["recommended_id"]

    for index, story in enumerate(
        result["top_five"],
        start=1,
    ):
        marker = ""

        if story["id"] == recommended_id:
            marker = "  <-- RECOMMENDED"

        print()
        print(
            f"{index}. [{story['total_score']}/100] "
            f"{story['title']}{marker}"
        )

        print(
            f"   Source: {story['source']}"
        )

        print(
            f"   Happy {story['happiness']}/10 | "
            f"Hope {story['hope']}/10 | "
            f"Interest {story['general_interest']}/10"
        )

        print(
            f"   Surprise {story['surprise']}/10 | "
            f"Duck Visual {story['duck_visual']}/10"
        )

        print(
            f"   Reason: {story['reason']}"
        )

        print(
            f"   URL: {story['url']}"
        )

    print()
    print("TODAY'S RECOMMENDATION")
    print("=" * 50)

    print(
        result["recommended_reason"]
    )

    print()
    print(
        f"Saved to {OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()
