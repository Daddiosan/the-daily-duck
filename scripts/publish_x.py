#!/usr/bin/env python3
"""
The Daily Duck - X publisher

Safety rules:
- Post only after website publication succeeded.
- Require automation_state/website_publish_result.json action == PUBLISHED.
- Require automation_state/ready_to_publish.json state == PUBLISHED.
- Never post the same issue twice.
- Use the already-selected canonical/published image.
- Save X post ID and URL after success.

Authentication:
OAuth 1.0a user context using:
X_API_KEY
X_API_SECRET
X_ACCESS_TOKEN
X_ACCESS_TOKEN_SECRET
"""

from __future__ import annotations

import json
import os
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


def build_post_text(ready: dict[str, Any]) -> str:
    approved = ready.get("gate_a_approved_story")
    if not isinstance(approved, dict):
        raise ValueError("gate_a_approved_story is missing.")

    issue_date = first_text(
        ready.get("issue_date"),
        approved.get("issue_date"),
        approved.get("date"),
    )
    if not issue_date:
        raise ValueError("issue_date is missing.")

    jp = first_text(approved.get("x_jp"))
    en = first_text(approved.get("x_en"))
    if not jp or not en:
        raise ValueError("x_jp or x_en is missing from approved story.")

    page_url = f"https://www.thedailyduck.ai/ducks/{issue_date}/"

    # A URL-bearing Create Post is billed differently by X.
    # Keep one canonical Daily Duck URL in the final post.
    post_text = f"{jp}\n\n{en}\n\n{page_url}"

    # X supports long posts for eligible accounts, but Daily Duck should stay concise.
    if len(post_text) > 1000:
        raise ValueError(
            f"Generated X post is unexpectedly long ({len(post_text)} characters)."
        )
    return post_text


def resolve_image_path(ready: dict[str, Any]) -> Path:
    # Prefer the image path copied into assets/ducks by website publishing.
    published_image = first_text(ready.get("published_image_path"))
    if published_image:
        path = Path(published_image)
        if path.exists():
            return path

    canonical = first_text(ready.get("canonical_image_path"))
    if canonical:
        path = Path(canonical)
        if path.exists():
            return path

    raise FileNotFoundError(
        "Neither published_image_path nor canonical_image_path points to an existing file."
    )


def oauth() -> OAuth1:
    return OAuth1(
        required_env("X_API_KEY"),
        required_env("X_API_SECRET"),
        required_env("X_ACCESS_TOKEN"),
        required_env("X_ACCESS_TOKEN_SECRET"),
    )


def upload_image(image_path: Path, auth: OAuth1) -> str:
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
            files={"media": (image_path.name, f, mime)},
            data={"media_category": "tweet_image"},
            timeout=90,
        )

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"X media upload failed: HTTP {response.status_code}: {response.text[:1000]}"
        )

    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"X media upload response missing data: {payload}")

    media_id = first_text(data.get("id"), data.get("media_id"))
    if not media_id:
        raise RuntimeError(f"X media upload response missing media ID: {payload}")

    return media_id


def create_post(text: str, media_id: str, auth: OAuth1) -> tuple[str, dict[str, Any]]:
    response = requests.post(
        CREATE_POST_URL,
        auth=auth,
        json={
            "text": text,
            "media": {
                "media_ids": [media_id],
            },
            # The image is AI-generated; disclose this to X where supported.
            "made_with_ai": True,
        },
        timeout=60,
    )

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"X Create Post failed: HTTP {response.status_code}: {response.text[:1000]}"
        )

    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"X Create Post response missing data: {payload}")

    post_id = first_text(data.get("id"))
    if not post_id:
        raise RuntimeError(f"X Create Post response missing post ID: {payload}")

    return post_id, payload


def main() -> int:
    ready = load_json(READY_PATH)
    website = load_json(WEBSITE_RESULT_PATH)

    issue_date = first_text(ready.get("issue_date"))

    # Safety gate 1: website must really have been published.
    if website.get("action") != "PUBLISHED":
        write_result(
            "WEBSITE_NOT_PUBLISHED_BLOCKED",
            issue_date=issue_date,
            website_action=website.get("action"),
            message="X posting was blocked because website publication did not succeed.",
        )
        print(
            f"X POST BLOCKED: website action is {website.get('action')!r}, not PUBLISHED."
        )
        print("No X API call was made.")
        print("STATE: WEBSITE_NOT_PUBLISHED_BLOCKED")
        return 0

    # Safety gate 2: READY state must have advanced to PUBLISHED.
    if ready.get("state") != "PUBLISHED":
        write_result(
            "WEBSITE_STATE_NOT_PUBLISHED_BLOCKED",
            issue_date=issue_date,
            ready_state=ready.get("state"),
        )
        print(f"X POST BLOCKED: ready state is {ready.get('state')!r}.")
        print("No X API call was made.")
        return 0

    # Safety gate 3: idempotency / no duplicate post.
    if ready.get("x_posted") is True or first_text(ready.get("x_post_id")):
        write_result(
            "ALREADY_POSTED_BLOCKED",
            issue_date=issue_date,
            x_post_id=ready.get("x_post_id"),
            x_post_url=ready.get("x_post_url"),
        )
        print("X POST BLOCKED: this issue is already marked as posted.")
        print("No X API call was made.")
        return 0

    image_path = resolve_image_path(ready)
    post_text = build_post_text(ready)

    auth = oauth()

    print(f"Uploading image to X: {image_path}")
    media_id = upload_image(image_path, auth)
    print(f"Media uploaded. media_id={media_id}")

    print("Creating X post...")
    post_id, response_payload = create_post(post_text, media_id, auth)

    post_url = f"https://x.com/i/web/status/{post_id}"

    ready["x_posted"] = True
    ready["x_posted_at"] = now_iso()
    ready["x_post_id"] = post_id
    ready["x_post_url"] = post_url
    ready["x_media_id"] = media_id
    ready["state"] = "X_POSTED"
    write_json(READY_PATH, ready)

    write_result(
        "X_POSTED",
        issue_date=issue_date,
        x_post_id=post_id,
        x_post_url=post_url,
        x_media_id=media_id,
        image_path=image_path.as_posix(),
        post_text=post_text,
        response=response_payload,
    )

    print(f"X POSTED: {post_id}")
    print(f"URL: {post_url}")
    print("STATE: X_POSTED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
