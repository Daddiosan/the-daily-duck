#!/usr/bin/env python3
"""
The Daily Duck - X publisher

English-first policy:
- English is the ONLY language posted to X.
- Japanese remains available on the website/email review flow, but is NOT
  included in the X post body.
- Uses ONLY canonical_x_image_path for X, so the website hero image can remain
  separate from the branded 5:4 X card.
- X post text is automatically kept within the standard 280-character limit.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests_oauthlib import OAuth1

STATE_DIR = Path("automation_state")
READY_PATH = STATE_DIR / "ready_to_publish.json"
WEBSITE_RESULT_PATH = STATE_DIR / "website_publish_result.json"
X_RESULT_PATH = STATE_DIR / "x_publish_result.json"

MEDIA_UPLOAD_URL = "https://api.x.com/2/media/upload"
CREATE_POST_URL = "https://api.x.com/2/tweets"
ME_URL = "https://api.x.com/2/users/me"

X_MAX_WEIGHTED_LENGTH = 280
X_TCO_URL_WEIGHT = 23
X_SAFETY_MARGIN = 4


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing required file: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_result(action: str, **extra: Any) -> None:
    write_json(
        X_RESULT_PATH,
        {
            "action": action,
            "at": now_iso(),
            **extra,
        },
    )


def first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def x_weighted_length(text: str) -> int:
    """
    Conservative X weighted-length estimate.

    URLs count as 23 characters. ASCII/common Latin counts as 1.
    CJK/full-width characters count as 2.
    """

    url_re = re.compile(r"https?://\S+")

    def char_weight(ch: str) -> int:
        code = ord(ch)

        if (
            0x1100 <= code <= 0x11FF
            or 0x2E80 <= code <= 0x9FFF
            or 0xAC00 <= code <= 0xD7AF
            or 0xF900 <= code <= 0xFAFF
            or 0xFF01 <= code <= 0xFF60
            or 0xFFE0 <= code <= 0xFFE6
        ):
            return 2

        return 1

    total = 0
    position = 0

    for match in url_re.finditer(text):
        before = text[position:match.start()]

        total += sum(
            char_weight(ch)
            for ch in before
        )

        total += X_TCO_URL_WEIGHT
        position = match.end()

    total += sum(
        char_weight(ch)
        for ch in text[position:]
    )

    return total


def trim_english_for_x(
    english_text: str,
    page_url: str,
) -> str:
    """
    Keep the article URL and shorten only the English body if necessary.
    """

    english_text = " ".join(
        english_text.split()
    ).strip()

    separator = "\n\n"

    safe_limit = (
        X_MAX_WEIGHTED_LENGTH
        - X_SAFETY_MARGIN
    )

    candidate = (
        f"{english_text}"
        f"{separator}"
        f"{page_url}"
    )

    if x_weighted_length(candidate) <= safe_limit:
        return candidate

    ellipsis = "…"
    words = english_text.split()
    kept: list[str] = []

    for word in words:
        trial_body = " ".join(
            kept + [word]
        ).strip()

        trial = (
            f"{trial_body}{ellipsis}"
            f"{separator}"
            f"{page_url}"
        )

        if x_weighted_length(trial) > safe_limit:
            break

        kept.append(word)

    if not kept:
        raise ValueError(
            "x_en cannot be shortened enough "
            "to fit the X post limit."
        )

    result = (
        f"{' '.join(kept).rstrip(' ,;:-')}{ellipsis}"
        f"{separator}"
        f"{page_url}"
    )

    if x_weighted_length(result) > safe_limit:
        raise ValueError(
            "Internal error: trimmed X post still exceeds safe limit."
        )

    return result


def build_post_text(ready: dict[str, Any]) -> str:
    """
    Build the final X post as:
        English master
        Daily Duck article URL

    Japanese is intentionally excluded from X.
    """

    approved = ready.get("gate_a_approved_story")

    if not isinstance(approved, dict):
        raise ValueError(
            "gate_a_approved_story is missing."
        )

    issue_date = first_text(
        ready.get("issue_date"),
        approved.get("issue_date"),
        approved.get("date"),
    )

    if not issue_date:
        raise ValueError(
            "issue_date is missing."
        )

    en = first_text(
        approved.get("x_en")
    )

    if not en:
        raise ValueError(
            "x_en is missing from approved story."
        )

    page_url = (
        "https://www.thedailyduck.ai/"
        f"ducks/{issue_date}/"
    )

    post_text = trim_english_for_x(
        en,
        page_url,
    )

    weighted = x_weighted_length(
        post_text
    )

    if weighted > X_MAX_WEIGHTED_LENGTH:
        raise ValueError(
            "Generated X post exceeds "
            f"{X_MAX_WEIGHTED_LENGTH} weighted characters: "
            f"{weighted}"
        )

    print("X post policy: ENGLISH ONLY")
    print(
        f"X weighted length: "
        f"{weighted}/{X_MAX_WEIGHTED_LENGTH}"
    )
    print("X post text:")
    print(post_text)

    return post_text

def resolve_image_path(ready: dict[str, Any]) -> Path:
    x_image = first_text(
        ready.get("canonical_x_image_path")
    )

    if x_image:
        path = Path(x_image)

        if path.exists():
            return path

    raise FileNotFoundError(
        "canonical_x_image_path does not point "
        "to an existing X image. "
        "Run scripts/build_x_card.py before publish_x.py."
    )


def oauth() -> OAuth1:
    return OAuth1(
        required_env("X_API_KEY"),
        required_env("X_API_SECRET"),
        required_env("X_ACCESS_TOKEN"),
        required_env("X_ACCESS_TOKEN_SECRET"),
    )


def find_existing_post(
    issue_date: str,
    auth: OAuth1,
) -> tuple[str, str] | None:
    """
    Check recent posts on X before creating a new one.

    This closes the important crash window where X accepted a previous post
    but GitHub did not get a chance to commit x_posted=True.
    """

    if not issue_date:
        raise ValueError("issue_date is missing.")

    me = requests.get(
        ME_URL,
        auth=auth,
        timeout=30,
    )

    if me.status_code != 200:
        raise RuntimeError(
            "X duplicate preflight /users/me failed: "
            f"HTTP {me.status_code}: {me.text[:1000]}"
        )

    me_data = me.json().get("data")

    if (
        not isinstance(me_data, dict)
        or not first_text(me_data.get("id"))
    ):
        raise RuntimeError(
            "X duplicate preflight could not resolve user ID: "
            f"{me.text[:1000]}"
        )

    user_id = first_text(me_data.get("id"))
    page_url = (
        f"https://www.thedailyduck.ai/ducks/{issue_date}/"
    )

    recent = requests.get(
        f"https://api.x.com/2/users/{user_id}/tweets",
        auth=auth,
        params={
            "max_results": 20,
            "exclude": "retweets,replies",
            "tweet.fields": "created_at",
        },
        timeout=30,
    )

    if recent.status_code != 200:
        raise RuntimeError(
            "X duplicate preflight recent-post lookup failed: "
            f"HTTP {recent.status_code}: "
            f"{recent.text[:1000]}"
        )

    payload = recent.json()
    rows = payload.get("data")

    if not isinstance(rows, list):
        return None

    for row in rows:
        if not isinstance(row, dict):
            continue

        text = first_text(row.get("text"))
        post_id = first_text(row.get("id"))

        if post_id and page_url in text:
            return (
                post_id,
                f"https://x.com/i/web/status/{post_id}",
            )

    return None


def upload_image(
    image_path: Path,
    auth: OAuth1,
) -> str:

    mime = "image/png"
    suffix = image_path.suffix.lower()

    if suffix in (".jpg", ".jpeg"):
        mime = "image/jpeg"

    elif suffix == ".webp":
        mime = "image/webp"

    with image_path.open("rb") as f:
        response = requests.post(
            MEDIA_UPLOAD_URL,
            auth=auth,
            files={
                "media": (
                    image_path.name,
                    f,
                    mime,
                )
            },
            data={
                "media_category":
                    "tweet_image"
            },
            timeout=90,
        )

    if response.status_code not in (200, 201):
        raise RuntimeError(
            "X media upload failed: "
            f"HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )

    payload = response.json()
    data = payload.get("data")

    if not isinstance(data, dict):
        raise RuntimeError(
            "X media upload response missing data: "
            f"{payload}"
        )

    media_id = first_text(
        data.get("id"),
        data.get("media_id"),
    )

    if not media_id:
        raise RuntimeError(
            "X media upload response missing media ID: "
            f"{payload}"
        )

    return media_id


def create_post(
    text: str,
    media_id: str,
    auth: OAuth1,
) -> tuple[str, dict[str, Any]]:

    response = requests.post(
        CREATE_POST_URL,
        auth=auth,
        json={
            "text": text,
            "media": {
                "media_ids": [
                    media_id
                ]
            },
            "made_with_ai": True,
        },
        timeout=60,
    )

    if response.status_code not in (200, 201):
        raise RuntimeError(
            "X Create Post failed: "
            f"HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )

    payload = response.json()
    data = payload.get("data")

    if not isinstance(data, dict):
        raise RuntimeError(
            "X Create Post response missing data: "
            f"{payload}"
        )

    post_id = first_text(
        data.get("id")
    )

    if not post_id:
        raise RuntimeError(
            "X Create Post response missing post ID: "
            f"{payload}"
        )

    return (
        post_id,
        payload,
    )


def main() -> int:

    ready = load_json(
        READY_PATH
    )

    website = load_json(
        WEBSITE_RESULT_PATH
    )

    issue_date = first_text(
        ready.get("issue_date")
    )

    if (
        website.get("action")
        != "PUBLISHED"
    ):
        write_result(
            "WEBSITE_NOT_PUBLISHED_BLOCKED",
            issue_date=issue_date,
            website_action=website.get(
                "action"
            ),
        )

        print(
            "X POST BLOCKED: "
            "website publication did not succeed."
        )

        return 0

    if (
        ready.get("state")
        != "PUBLISHED"
    ):
        write_result(
            "WEBSITE_STATE_NOT_PUBLISHED_BLOCKED",
            issue_date=issue_date,
            ready_state=ready.get(
                "state"
            ),
        )

        print(
            "X POST BLOCKED: "
            f"ready state is {ready.get('state')!r}."
        )

        return 0

    if (
        ready.get("x_posted") is True
        or first_text(
            ready.get("x_post_id")
        )
    ):
        write_result(
            "ALREADY_POSTED_BLOCKED",
            issue_date=issue_date,
            x_post_id=ready.get(
                "x_post_id"
            ),
        )

        print(
            "X POST BLOCKED: "
            "this issue is already marked as posted."
        )

        return 0

    image_path = resolve_image_path(
        ready
    )

    post_text = build_post_text(
        ready
    )

    auth = oauth()

    # Second duplicate guard:
    # verify X itself before uploading media.
    existing = find_existing_post(
        issue_date,
        auth,
    )

    if existing is not None:

        (
            existing_id,
            existing_url,
        ) = existing

        ready[
            "x_posted"
        ] = True

        ready[
            "x_post_id"
        ] = existing_id

        ready[
            "x_post_url"
        ] = existing_url

        ready[
            "state"
        ] = "X_POSTED"

        write_json(
            READY_PATH,
            ready,
        )

        write_result(
            "ALREADY_POSTED_REMOTE_BLOCKED",
            issue_date=issue_date,
            x_post_id=existing_id,
            x_post_url=existing_url,
        )

        print(
            "X POST BLOCKED: "
            "an existing post for this issue "
            "was found on X."
        )

        return 0

    print(
        "Uploading 5:4 Daily Duck X card: "
        f"{image_path}"
    )

    media_id = upload_image(
        image_path,
        auth,
    )

    (
        post_id,
        response_payload,
    ) = create_post(
        post_text,
        media_id,
        auth,
    )

    post_url = (
        f"https://x.com/i/web/status/{post_id}"
    )

    ready[
        "x_posted"
    ] = True

    ready[
        "x_posted_at"
    ] = now_iso()

    ready[
        "x_post_id"
    ] = post_id

    ready[
        "x_post_url"
    ] = post_url

    ready[
        "x_media_id"
    ] = media_id

    ready[
        "state"
    ] = "X_POSTED"

    # Record X-specific language and length policy.
    ready[
        "x_language_policy"
    ] = {
        "primary_language":
            "en",
        "canonical_language":
            "en",
        "secondary_language":
            None,
        "post_order":
            [
                "en",
                "url",
            ],
        "max_weighted_length":
            X_MAX_WEIGHTED_LENGTH,
        "weighted_length":
            x_weighted_length(post_text),
    }

    write_json(
        READY_PATH,
        ready,
    )

    write_result(
        "X_POSTED",
        issue_date=issue_date,
        x_post_id=post_id,
        x_post_url=post_url,
        x_media_id=media_id,
        image_path=image_path.as_posix(),
        post_text=post_text,
        language_policy={
            "primary_language":
                "en",
            "canonical_language":
                "en",
            "secondary_language":
                None,
            "post_order":
                [
                    "en",
                    "url",
                ],
            "max_weighted_length":
                X_MAX_WEIGHTED_LENGTH,
            "weighted_length":
                x_weighted_length(post_text),
        },
        response=response_payload,
    )

    print(
        f"X POSTED: {post_id}"
    )

    print(
        f"URL: {post_url}"
    )

    print(
        "LANGUAGE: "
        "ENGLISH ONLY"
    )

    print(
        "WEIGHTED LENGTH: "
        f"{x_weighted_length(post_text)}/"
        f"{X_MAX_WEIGHTED_LENGTH}"
    )

    print(
        "STATE: X_POSTED"
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
