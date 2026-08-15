#!/usr/bin/env python3
"""
The Daily Duck - X publisher
DIAGNOSTIC VERSION

Purpose:
- Upload the generated 5:4 X card.
- Print safe diagnostic information from X API responses.
- Never print API keys, access tokens, or secrets.
- Preserve diagnostic result even when X Create Post fails.

Uses ONLY canonical_x_image_path for X.
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

READY_PATH = (
    STATE_DIR
    / "ready_to_publish.json"
)

WEBSITE_RESULT_PATH = (
    STATE_DIR
    / "website_publish_result.json"
)

X_RESULT_PATH = (
    STATE_DIR
    / "x_publish_result.json"
)


MEDIA_UPLOAD_URL = (
    "https://api.x.com/2/media/upload"
)

CREATE_POST_URL = (
    "https://api.x.com/2/tweets"
)

ME_URL = (
    "https://api.x.com/2/users/me"
)


def required_env(
    name: str,
) -> str:

    value = os.getenv(
        name,
        "",
    ).strip()

    if not value:
        raise RuntimeError(
            "Missing required environment "
            f"variable: {name}"
        )

    return value


def load_json(
    path: Path,
) -> dict[str, Any]:

    if not path.exists():
        raise FileNotFoundError(
            f"Missing required file: {path}"
        )

    data = json.loads(
        path.read_text(
            encoding="utf-8"
        )
    )

    if not isinstance(
        data,
        dict,
    ):
        raise ValueError(
            f"{path} must contain "
            "a JSON object."
        )

    return data


def write_json(
    path: Path,
    data: dict[str, Any],
) -> None:

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def now_iso() -> str:

    return datetime.now(
        timezone.utc
    ).isoformat()


def write_result(
    action: str,
    **extra: Any,
) -> None:

    write_json(
        X_RESULT_PATH,
        {
            "action": action,
            "at": now_iso(),
            **extra,
        },
    )


def first_text(
    *values: Any,
) -> str:

    for value in values:

        if (
            isinstance(value, str)
            and value.strip()
        ):
            return value.strip()

    return ""


def safe_response_headers(
    response: requests.Response,
) -> dict[str, str]:

    wanted = {
        "content-type",
        "content-length",
        "x-rate-limit-limit",
        "x-rate-limit-remaining",
        "x-rate-limit-reset",
        "x-transaction-id",
        "x-response-time",
    }

    output: dict[str, str] = {}

    for key, value in response.headers.items():

        if key.lower() in wanted:
            output[key] = value

    return output


def print_response_debug(
    label: str,
    response: requests.Response,
) -> None:

    print()
    print("=" * 70)
    print(label)
    print("=" * 70)

    print(
        "URL:",
        response.request.url,
    )

    print(
        "HTTP status:",
        response.status_code,
    )

    print(
        "Response headers:"
    )

    headers = safe_response_headers(
        response
    )

    if headers:

        for key, value in headers.items():
            print(
                f"  {key}: {value}"
            )

    else:
        print(
            "  (no selected diagnostic headers)"
        )

    print(
        "Response body:"
    )

    if response.text:

        print(
            response.text[:5000]
        )

    else:

        print(
            "(empty response body)"
        )

    print("=" * 70)
    print()


def build_post_text(
    ready: dict[str, Any],
) -> str:

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
        ready.get(
            "issue_date"
        ),
        approved.get(
            "issue_date"
        ),
        approved.get(
            "date"
        ),
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
            "x_jp or x_en is missing "
            "from approved story."
        )

    page_url = (
        "https://www.thedailyduck.ai/"
        f"ducks/{issue_date}/"
    )

    post_text = (
        f"{jp}\n\n"
        f"{en}\n\n"
        f"{page_url}"
    )

    if len(post_text) > 1000:
        raise ValueError(
            "Generated X post is "
            "unexpectedly long "
            f"({len(post_text)} characters)."
        )

    return post_text


def resolve_image_path(
    ready: dict[str, Any],
) -> Path:

    x_image = first_text(
        ready.get(
            "canonical_x_image_path"
        )
    )

    if x_image:

        path = Path(
            x_image
        )

        if path.exists():
            return path

    raise FileNotFoundError(
        "canonical_x_image_path does not "
        "point to an existing X image. "
        "Run scripts/build_x_card.py "
        "before publish_x.py."
    )


def oauth() -> OAuth1:

    # IMPORTANT:
    # Never print any of these values.
    return OAuth1(
        required_env(
            "X_API_KEY"
        ),
        required_env(
            "X_API_SECRET"
        ),
        required_env(
            "X_ACCESS_TOKEN"
        ),
        required_env(
            "X_ACCESS_TOKEN_SECRET"
        ),
    )


def find_existing_post(
    issue_date: str,
    auth: OAuth1,
) -> tuple[str, str] | None:

    if not issue_date:
        raise ValueError(
            "issue_date is missing."
        )

    print(
        "Running X duplicate-post preflight..."
    )

    me = requests.get(
        ME_URL,
        auth=auth,
        timeout=30,
    )

    print(
        "GET /2/users/me:",
        me.status_code,
    )

    if me.status_code != 200:

        print_response_debug(
            "X /users/me FAILURE",
            me,
        )

        raise RuntimeError(
            "X duplicate preflight "
            "/users/me failed: "
            f"HTTP {me.status_code}: "
            f"{me.text[:1000]}"
        )

    me_data = (
        me.json().get(
            "data"
        )
    )

    if (
        not isinstance(
            me_data,
            dict,
        )
        or not first_text(
            me_data.get(
                "id"
            )
        )
    ):
        raise RuntimeError(
            "X duplicate preflight "
            "could not resolve user ID."
        )

    user_id = first_text(
        me_data.get(
            "id"
        )
    )

    print(
        "Authenticated X user ID:",
        user_id,
    )

    page_url = (
        "https://www.thedailyduck.ai/"
        f"ducks/{issue_date}/"
    )

    recent_url = (
        "https://api.x.com/2/users/"
        f"{user_id}/tweets"
    )

    recent = requests.get(
        recent_url,
        auth=auth,
        params={
            "max_results": 20,
            "exclude":
                "retweets,replies",
            "tweet.fields":
                "created_at",
        },
        timeout=30,
    )

    print(
        "GET recent posts:",
        recent.status_code,
    )

    if recent.status_code != 200:

        print_response_debug(
            "X recent-post lookup FAILURE",
            recent,
        )

        raise RuntimeError(
            "X duplicate preflight "
            "recent-post lookup failed: "
            f"HTTP {recent.status_code}: "
            f"{recent.text[:1000]}"
        )

    payload = recent.json()

    rows = payload.get(
        "data"
    )

    if not isinstance(
        rows,
        list,
    ):
        return None

    for row in rows:

        if not isinstance(
            row,
            dict,
        ):
            continue

        text = first_text(
            row.get(
                "text"
            )
        )

        post_id = first_text(
            row.get(
                "id"
            )
        )

        if (
            post_id
            and page_url in text
        ):

            return (
                post_id,
                "https://x.com/i/web/"
                f"status/{post_id}",
            )

    return None


def upload_image(
    image_path: Path,
    auth: OAuth1,
) -> tuple[
    str,
    dict[str, Any],
]:

    if not image_path.exists():
        raise FileNotFoundError(
            image_path
        )

    file_size = (
        image_path.stat().st_size
    )

    print()
    print("=" * 70)
    print("X MEDIA UPLOAD DIAGNOSTICS")
    print("=" * 70)

    print(
        "Image path:",
        image_path,
    )

    print(
        "Filename:",
        image_path.name,
    )

    print(
        "File size:",
        file_size,
        "bytes",
    )

    print(
        "File size MB:",
        f"{file_size / 1024 / 1024:.3f}",
    )

    mime = "image/png"

    suffix = (
        image_path
        .suffix
        .lower()
    )

    if suffix in (
        ".jpg",
        ".jpeg",
    ):

        mime = "image/jpeg"

    elif suffix == ".webp":

        mime = "image/webp"

    print(
        "Detected MIME type:",
        mime,
    )

    print(
        "Media category:",
        "tweet_image",
    )

    print(
        "Upload endpoint:",
        MEDIA_UPLOAD_URL,
    )

    print("=" * 70)
    print()

    with image_path.open(
        "rb"
    ) as f:

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

    print_response_debug(
        "X MEDIA UPLOAD RESPONSE",
        response,
    )

    if response.status_code not in (
        200,
        201,
    ):

        write_result(
            "MEDIA_UPLOAD_FAILED",
            image_path=
                image_path.as_posix(),
            image_bytes=
                file_size,
            media_mime=
                mime,
            http_status=
                response.status_code,
            response_text=
                response.text[:5000],
            response_headers=
                safe_response_headers(
                    response
                ),
        )

        raise RuntimeError(
            "X media upload failed: "
            f"HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )

    try:

        payload = (
            response.json()
        )

    except Exception as exc:

        write_result(
            "MEDIA_UPLOAD_INVALID_JSON",
            http_status=
                response.status_code,
            response_text=
                response.text[:5000],
        )

        raise RuntimeError(
            "X media upload returned "
            "invalid JSON."
        ) from exc

    print(
        "Parsed media upload JSON:"
    )

    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        )
    )

    data = payload.get(
        "data"
    )

    if not isinstance(
        data,
        dict,
    ):

        write_result(
            "MEDIA_UPLOAD_MISSING_DATA",
            response=payload,
        )

        raise RuntimeError(
            "X media upload response "
            f"missing data: {payload}"
        )

    media_id = first_text(
        data.get(
            "id"
        ),
        data.get(
            "media_id"
        ),
        data.get(
            "media_id_string"
        ),
    )

    if not media_id:

        write_result(
            "MEDIA_UPLOAD_MISSING_ID",
            response=payload,
        )

        raise RuntimeError(
            "X media upload response "
            f"missing media ID: {payload}"
        )

    print()
    print(
        "MEDIA ID SELECTED:",
        media_id,
    )

    print(
        "media_key:",
        data.get(
            "media_key"
        ),
    )

    print(
        "expires_after_secs:",
        data.get(
            "expires_after_secs"
        ),
    )

    print(
        "processing_info:",
        json.dumps(
            data.get(
                "processing_info"
            ),
            ensure_ascii=False,
        ),
    )

    print()

    return (
        media_id,
        payload,
    )


def create_post(
    text: str,
    media_id: str,
    auth: OAuth1,
    upload_payload: dict[str, Any],
    image_path: Path,
) -> tuple[
    str,
    dict[str, Any],
]:

    request_payload = {
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
    print("X CREATE POST DIAGNOSTICS")
    print("=" * 70)

    print(
        "Endpoint:",
        CREATE_POST_URL,
    )

    print(
        "Media ID being attached:",
        media_id,
    )

    print(
        "Post text length:",
        len(text),
    )

    print(
        "Request JSON:"
    )

    print(
        json.dumps(
            request_payload,
            ensure_ascii=False,
            indent=2,
        )
    )

    print("=" * 70)
    print()

    response = requests.post(
        CREATE_POST_URL,
        auth=auth,
        json=request_payload,
        timeout=60,
    )

    print_response_debug(
        "X CREATE POST RESPONSE",
        response,
    )

    if response.status_code not in (
        200,
        201,
    ):

        write_result(
            "CREATE_POST_FAILED",
            image_path=
                image_path.as_posix(),
            media_id=
                media_id,
            media_upload_response=
                upload_payload,
            create_post_http_status=
                response.status_code,
            create_post_response=
                response.text[:5000],
            create_post_headers=
                safe_response_headers(
                    response
                ),
            post_text=
                text,
        )

        raise RuntimeError(
            "X Create Post failed: "
            f"HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )

    try:

        payload = (
            response.json()
        )

    except Exception as exc:

        write_result(
            "CREATE_POST_INVALID_JSON",
            media_id=media_id,
            http_status=
                response.status_code,
            response_text=
                response.text[:5000],
        )

        raise RuntimeError(
            "X Create Post returned "
            "invalid JSON."
        ) from exc

    data = payload.get(
        "data"
    )

    if not isinstance(
        data,
        dict,
    ):

        write_result(
            "CREATE_POST_MISSING_DATA",
            media_id=media_id,
            response=payload,
        )

        raise RuntimeError(
            "X Create Post response "
            f"missing data: {payload}"
        )

    post_id = first_text(
        data.get(
            "id"
        )
    )

    if not post_id:

        write_result(
            "CREATE_POST_MISSING_ID",
            media_id=media_id,
            response=payload,
        )

        raise RuntimeError(
            "X Create Post response "
            f"missing post ID: {payload}"
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
        ready.get(
            "issue_date"
        )
    )

    print()
    print("=" * 70)
    print("THE DAILY DUCK X PUBLISH - DIAGNOSTIC MODE")
    print("=" * 70)

    print(
        "Issue date:",
        issue_date,
    )

    print(
        "Ready state:",
        ready.get(
            "state"
        ),
    )

    print(
        "Website action:",
        website.get(
            "action"
        ),
    )

    print("=" * 70)
    print()

    if (
        website.get(
            "action"
        )
        != "PUBLISHED"
    ):

        write_result(
            "WEBSITE_NOT_PUBLISHED_BLOCKED",
            issue_date=
                issue_date,
            website_action=
                website.get(
                    "action"
                ),
        )

        print(
            "X POST BLOCKED: "
            "website publication "
            "did not succeed."
        )

        return 0

    if (
        ready.get(
            "state"
        )
        != "PUBLISHED"
    ):

        write_result(
            "WEBSITE_STATE_NOT_PUBLISHED_BLOCKED",
            issue_date=
                issue_date,
            ready_state=
                ready.get(
                    "state"
                ),
        )

        print(
            "X POST BLOCKED: "
            "ready state is "
            f"{ready.get('state')!r}."
        )

        return 0

    if (
        ready.get(
            "x_posted"
        )
        is True
        or first_text(
            ready.get(
                "x_post_id"
            )
        )
    ):

        write_result(
            "ALREADY_POSTED_BLOCKED",
            issue_date=
                issue_date,
            x_post_id=
                ready.get(
                    "x_post_id"
                ),
        )

        print(
            "X POST BLOCKED: "
            "this issue is already "
            "marked as posted."
        )

        return 0

    image_path = (
        resolve_image_path(
            ready
        )
    )

    post_text = (
        build_post_text(
            ready
        )
    )

    auth = oauth()

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
            issue_date=
                issue_date,
            x_post_id=
                existing_id,
            x_post_url=
                existing_url,
        )

        print(
            "X POST BLOCKED: "
            "an existing post for this "
            "issue was found on X."
        )

        return 0

    print(
        "Uploading 5:4 Daily Duck "
        f"X card: {image_path}"
    )

    (
        media_id,
        upload_payload,
    ) = upload_image(
        image_path,
        auth,
    )

    print(
        "Media upload succeeded."
    )

    print(
        "Now attempting Create Post "
        "with media ID:",
        media_id,
    )

    (
        post_id,
        response_payload,
    ) = create_post(
        text=
            post_text,
        media_id=
            media_id,
        auth=
            auth,
        upload_payload=
            upload_payload,
        image_path=
            image_path,
    )

    post_url = (
        "https://x.com/i/web/"
        f"status/{post_id}"
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

    write_json(
        READY_PATH,
        ready,
    )

    write_result(
        "X_POSTED",
        issue_date=
            issue_date,
        x_post_id=
            post_id,
        x_post_url=
            post_url,
        x_media_id=
            media_id,
        image_path=
            image_path.as_posix(),
        post_text=
            post_text,
        media_upload_response=
            upload_payload,
        response=
            response_payload,
    )

    print()
    print(
        f"X POSTED: {post_id}"
    )

    print(
        f"URL: {post_url}"
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
