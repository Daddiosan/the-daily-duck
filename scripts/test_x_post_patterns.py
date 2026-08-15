#!/usr/bin/env python3
"""
The Daily Duck - X 3-pattern diagnostic test

Tests:

A. Real Daily Duck image + short plain text
B. Real Daily Duck image + Daily Duck article URL only
C. Real Daily Duck image + actual JP/EN copy, without URL

This script does NOT modify ready_to_publish.json.
It does NOT mark the Daily Duck issue as X_POSTED.

Successful posts are recorded so they can be deleted afterward.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests_oauthlib import OAuth1


READY_PATH = Path(
    "automation_state/ready_to_publish.json"
)

RESULT_PATH = Path(
    "automation_state/x_pattern_test_result.json"
)

MEDIA_UPLOAD_URL = (
    "https://api.x.com/2/media/upload"
)

CREATE_POST_URL = (
    "https://api.x.com/2/tweets"
)


def required_env(name: str) -> str:
    value = os.getenv(
        name,
        "",
    ).strip()

    if not value:
        raise RuntimeError(
            f"Missing environment variable: {name}"
        )

    return value


def first_text(*values: Any) -> str:
    for value in values:
        if (
            isinstance(value, str)
            and value.strip()
        ):
            return value.strip()

    return ""


def now_iso() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def load_ready() -> dict[str, Any]:
    if not READY_PATH.exists():
        raise FileNotFoundError(
            f"Missing {READY_PATH}"
        )

    data = json.loads(
        READY_PATH.read_text(
            encoding="utf-8"
        )
    )

    if not isinstance(data, dict):
        raise ValueError(
            "ready_to_publish.json "
            "must contain an object."
        )

    return data


def save_result(
    data: dict[str, Any],
) -> None:

    RESULT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    RESULT_PATH.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def oauth() -> OAuth1:
    return OAuth1(
        required_env("X_API_KEY"),
        required_env("X_API_SECRET"),
        required_env("X_ACCESS_TOKEN"),
        required_env("X_ACCESS_TOKEN_SECRET"),
    )


def resolve_image(
    ready: dict[str, Any],
) -> Path:

    value = first_text(
        ready.get(
            "canonical_x_image_path"
        )
    )

    if not value:
        raise ValueError(
            "canonical_x_image_path "
            "is missing."
        )

    path = Path(value)

    if not path.exists():
        raise FileNotFoundError(
            f"X image does not exist: {path}"
        )

    return path


def image_mime(
    path: Path,
) -> str:

    suffix = path.suffix.lower()

    if suffix in (
        ".jpg",
        ".jpeg",
    ):
        return "image/jpeg"

    if suffix == ".png":
        return "image/png"

    if suffix == ".webp":
        return "image/webp"

    raise ValueError(
        f"Unsupported image format: {suffix}"
    )


def upload_image(
    image_path: Path,
    auth: OAuth1,
    label: str,
) -> tuple[str | None, dict[str, Any]]:

    mime = image_mime(
        image_path
    )

    print()
    print("=" * 70)
    print(
        f"{label}: MEDIA UPLOAD"
    )
    print("=" * 70)

    print(
        "Image:",
        image_path,
    )

    print(
        "Bytes:",
        image_path.stat().st_size,
    )

    print(
        "MIME:",
        mime,
    )

    with image_path.open(
        "rb"
    ) as handle:

        response = requests.post(
            MEDIA_UPLOAD_URL,
            auth=auth,
            files={
                "media": (
                    image_path.name,
                    handle,
                    mime,
                )
            },
            data={
                "media_category":
                    "tweet_image"
            },
            timeout=90,
        )

    print(
        "HTTP:",
        response.status_code,
    )

    print(
        "BODY:",
        response.text[:3000],
    )

    result: dict[str, Any] = {
        "http_status":
            response.status_code,

        "response":
            response.text[:3000],
    }

    if response.status_code not in (
        200,
        201,
    ):
        return None, result

    try:
        payload = response.json()

    except Exception:
        return None, result

    data = payload.get(
        "data"
    )

    if not isinstance(
        data,
        dict,
    ):
        return None, result

    media_id = first_text(
        data.get("id"),
        data.get("media_id"),
        data.get("media_id_string"),
    )

    result[
        "media_id"
    ] = media_id

    result[
        "json"
    ] = payload

    print(
        "MEDIA ID:",
        media_id,
    )

    if not media_id:
        return None, result

    return media_id, result


def create_test_post(
    text: str,
    media_id: str,
    auth: OAuth1,
    label: str,
) -> dict[str, Any]:

    request_json = {
        "text":
            text,

        "media": {
            "media_ids": [
                media_id
            ]
        },
    }

    print()
    print("=" * 70)
    print(
        f"{label}: CREATE POST"
    )
    print("=" * 70)

    print(
        "TEXT:"
    )

    print(
        text
    )

    print(
        "Python len:",
        len(text),
    )

    print(
        "Media ID:",
        media_id,
    )

    response = requests.post(
        CREATE_POST_URL,
        auth=auth,
        json=request_json,
        timeout=60,
    )

    print(
        "HTTP:",
        response.status_code,
    )

    print(
        "BODY:",
        response.text[:3000],
    )

    result: dict[str, Any] = {
        "http_status":
            response.status_code,

        "response":
            response.text[:3000],

        "text":
            text,

        "media_id":
            media_id,
    }

    if response.status_code not in (
        200,
        201,
    ):
        result[
            "success"
        ] = False

        return result

    try:
        payload = response.json()

    except Exception:
        result[
            "success"
        ] = False

        return result

    data = payload.get(
        "data"
    )

    if not isinstance(
        data,
        dict,
    ):
        result[
            "success"
        ] = False

        return result

    post_id = first_text(
        data.get(
            "id"
        )
    )

    result[
        "success"
    ] = bool(post_id)

    result[
        "post_id"
    ] = post_id

    if post_id:

        result[
            "post_url"
        ] = (
            "https://x.com/i/web/"
            f"status/{post_id}"
        )

        print(
            "SUCCESS:"
        )

        print(
            result[
                "post_url"
            ]
        )

    return result


def shorten_plain(
    text: str,
    max_chars: int,
) -> str:

    text = (
        text
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )

    while "  " in text:
        text = text.replace(
            "  ",
            " ",
        )

    if len(text) <= max_chars:
        return text

    return (
        text[:max_chars - 1]
        .rstrip()
        + "…"
    )


def run_pattern(
    *,
    name: str,
    text: str,
    image_path: Path,
    auth: OAuth1,
) -> dict[str, Any]:

    media_id, upload_result = (
        upload_image(
            image_path,
            auth,
            name,
        )
    )

    result: dict[str, Any] = {
        "name":
            name,

        "text":
            text,

        "upload":
            upload_result,
    }

    if not media_id:

        result[
            "success"
        ] = False

        result[
            "stage"
        ] = "MEDIA_UPLOAD"

        return result

    # Small pause between upload and Create Post.
    time.sleep(2)

    post_result = create_test_post(
        text=text,
        media_id=media_id,
        auth=auth,
        label=name,
    )

    result[
        "post"
    ] = post_result

    result[
        "success"
    ] = bool(
        post_result.get(
            "success"
        )
    )

    result[
        "stage"
    ] = "CREATE_POST"

    return result


def main() -> int:

    ready = load_ready()

    image_path = resolve_image(
        ready
    )

    approved = ready.get(
        "gate_a_approved_story"
    )

    if not isinstance(
        approved,
        dict,
    ):
        raise ValueError(
            "gate_a_approved_story "
            "is missing."
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

    jp = first_text(
        approved.get(
            "x_jp"
        )
    )

    en = first_text(
        approved.get(
            "x_en"
        )
    )

    if not jp or not en:
        raise ValueError(
            "x_jp / x_en is missing."
        )

    article_url = (
        "https://www.thedailyduck.ai/"
        f"ducks/{issue_date}/"
    )

    # Pattern A:
    # Same actual Daily Duck image,
    # extremely simple text.
    text_a = (
        "The Daily Duck image API test "
        "— delete me"
    )

    # Pattern B:
    # Same image, URL only.
    text_b = article_url

    # Pattern C:
    # Same image, real JP/EN content,
    # but NO Daily Duck URL.
    #
    # Intentionally conservative length so
    # character-count limits cannot confuse
    # this diagnostic.
    jp_test = shorten_plain(
        jp,
        60,
    )

    en_test = shorten_plain(
        en,
        100,
    )

    text_c = (
        f"{jp_test}\n\n"
        f"{en_test}"
    )

    auth = oauth()

    print()
    print("=" * 70)
    print(
        "THE DAILY DUCK"
    )
    print(
        "X THREE-PATTERN DIAGNOSTIC"
    )
    print("=" * 70)

    print(
        "Issue:",
        issue_date,
    )

    print(
        "Image:",
        image_path,
    )

    print("=" * 70)

    patterns = [
        (
            "PATTERN_A_IMAGE_PLUS_SHORT_TEXT",
            text_a,
        ),
        (
            "PATTERN_B_IMAGE_PLUS_URL_ONLY",
            text_b,
        ),
        (
            "PATTERN_C_IMAGE_PLUS_REAL_COPY_NO_URL",
            text_c,
        ),
    ]

    results: list[
        dict[str, Any]
    ] = []

    for index, (
        name,
        text,
    ) in enumerate(
        patterns,
        start=1,
    ):

        print()
        print()
        print(
            "#" * 70
        )

        print(
            f"TEST {index}/3:"
        )

        print(
            name
        )

        print(
            "#" * 70
        )

        result = run_pattern(
            name=name,
            text=text,
            image_path=image_path,
            auth=auth,
        )

        results.append(
            result
        )

        if index < 3:
            print()
            print(
                "Waiting 5 seconds "
                "before next test..."
            )

            time.sleep(5)

    output = {
        "created_at":
            now_iso(),

        "issue_date":
            issue_date,

        "image_path":
            image_path.as_posix(),

        "results":
            results,
    }

    save_result(
        output
    )

    print()
    print()
    print("=" * 70)
    print(
        "FINAL DIAGNOSTIC SUMMARY"
    )
    print("=" * 70)

    for result in results:

        name = result[
            "name"
        ]

        success = result.get(
            "success"
        )

        print()
        print(
            name
        )

        print(
            "RESULT:",
            (
                "SUCCESS"
                if success
                else "FAILED"
            ),
        )

        post = result.get(
            "post"
        )

        if isinstance(
            post,
            dict,
        ):

            if post.get(
                "post_url"
            ):
                print(
                    "POST URL:",
                    post[
                        "post_url"
                    ],
                )

            print(
                "CREATE POST HTTP:",
                post.get(
                    "http_status"
                ),
            )

    print()
    print(
        "Result JSON:"
    )

    print(
        RESULT_PATH
    )

    print("=" * 70)

    # Diagnostic workflow itself succeeds even when
    # one or more X tests return 403.
    # The purpose is to compare the results.
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
