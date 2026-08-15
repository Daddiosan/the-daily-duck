#!/usr/bin/env python3
"""
The Daily Duck - X publisher

Final production version.

Features:
- Uses ONLY canonical_x_image_path for X.
- Supports JPEG / PNG / WebP X cards.
- Checks website publication before X posting.
- Prevents duplicate X posts locally and remotely.
- Calculates X weighted character length.
- Automatically shortens JP/EN copy only when needed.
- Always preserves the Daily Duck page URL.
- Does NOT send made_with_ai.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
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


X_MAX_WEIGHTED_LENGTH = 250


URL_RE = re.compile(
    r"https?://[^\s]+",
    flags=re.IGNORECASE,
)


# ============================================================
# Basic utilities
# ============================================================

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
            "action":
                action,

            "at":
                now_iso(),

            **extra,
        },
    )


def first_text(
    *values: Any,
) -> str:

    for value in values:

        if (
            isinstance(
                value,
                str,
            )
            and value.strip()
        ):
            return value.strip()

    return ""


# ============================================================
# X weighted character counting
# ============================================================

def is_cjk_codepoint(
    cp: int,
) -> bool:

    ranges = (
        (
            0x3040,
            0x309F,
        ),
        (
            0x30A0,
            0x30FF,
        ),
        (
            0x31F0,
            0x31FF,
        ),
        (
            0x3400,
            0x4DBF,
        ),
        (
            0x4E00,
            0x9FFF,
        ),
        (
            0xF900,
            0xFAFF,
        ),
        (
            0xAC00,
            0xD7AF,
        ),
    )

    return any(
        start <= cp <= end
        for start, end in ranges
    )


def char_weight(
    ch: str,
) -> int:

    cp = ord(
        ch
    )

    if is_cjk_codepoint(
        cp
    ):
        return 2

    if cp > 0xFFFF:
        return 2

    return 1


def x_weighted_length(
    text: str,
) -> int:

    text = unicodedata.normalize(
        "NFC",
        text,
    )

    total = 0
    position = 0

    for match in URL_RE.finditer(
        text
    ):

        before = text[
            position:
            match.start()
        ]

        total += sum(
            char_weight(
                ch
            )
            for ch in before
        )

        # X/t.co fixed URL weight.
        total += 23

        position = (
            match.end()
        )

    remaining = text[
        position:
    ]

    total += sum(
        char_weight(
            ch
        )
        for ch in remaining
    )

    return total


def trim_to_weight(
    text: str,
    max_weight: int,
    add_ellipsis: bool = True,
) -> str:

    text = text.strip()

    if (
        x_weighted_length(
            text
        )
        <= max_weight
    ):
        return text

    suffix = (
        "…"
        if add_ellipsis
        else ""
    )

    suffix_weight = (
        x_weighted_length(
            suffix
        )
    )

    allowed = max(
        0,
        max_weight
        - suffix_weight,
    )

    output: list[str] = []

    used = 0

    for ch in text:

        weight = (
            char_weight(
                ch
            )
        )

        if (
            used
            + weight
            > allowed
        ):
            break

        output.append(
            ch
        )

        used += weight

    trimmed = (
        "".join(
            output
        )
        .rstrip()
    )

    if not trimmed:
        return ""

    if add_ellipsis:
        trimmed += suffix

    return trimmed


# ============================================================
# Build post text
# ============================================================

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

    original = (
        f"{jp}\n\n"
        f"{en}\n\n"
        f"{page_url}"
    )

    original_weight = (
        x_weighted_length(
            original
        )
    )

    print(
        "Original X weighted length:",
        original_weight,
    )

    if (
        original_weight
        <= X_MAX_WEIGHTED_LENGTH
    ):

        print(
            "X text is within "
            "the 280 weighted-character limit."
        )

        return original

    print(
        "X text exceeds "
        "280 weighted characters."
    )

    print(
        "Automatically shortening "
        "JP/EN copy."
    )

    separator_weight = (
        x_weighted_length(
            "\n\n"
        )
        * 2
    )

    url_weight = 23

    available_for_copy = (
        X_MAX_WEIGHTED_LENGTH
        - separator_weight
        - url_weight
    )

    # Slightly favor EN in raw character capacity,
    # while preserving both languages.
    jp_budget = int(
        available_for_copy
        * 0.46
    )

    en_budget = (
        available_for_copy
        - jp_budget
    )

    jp_short = trim_to_weight(
        jp,
        jp_budget,
    )

    en_short = trim_to_weight(
        en,
        en_budget,
    )

    post_text = (
        f"{jp_short}\n\n"
        f"{en_short}\n\n"
        f"{page_url}"
    )

    final_weight = (
        x_weighted_length(
            post_text
        )
    )

    while (
        final_weight
        > X_MAX_WEIGHTED_LENGTH
        and en_short
    ):

        current_en_weight = (
            x_weighted_length(
                en_short
            )
        )

        en_short = trim_to_weight(
            en_short.rstrip(
                "…"
            ),
            max(
                1,
                current_en_weight
                - 2,
            ),
        )

        post_text = (
            f"{jp_short}\n\n"
            f"{en_short}\n\n"
            f"{page_url}"
        )

        final_weight = (
            x_weighted_length(
                post_text
            )
        )

    while (
        final_weight
        > X_MAX_WEIGHTED_LENGTH
        and jp_short
    ):

        current_jp_weight = (
            x_weighted_length(
                jp_short
            )
        )

        jp_short = trim_to_weight(
            jp_short.rstrip(
                "…"
            ),
            max(
                1,
                current_jp_weight
                - 2,
            ),
        )

        post_text = (
            f"{jp_short}\n\n"
            f"{en_short}\n\n"
            f"{page_url}"
        )

        final_weight = (
            x_weighted_length(
                post_text
            )
        )

    if (
        final_weight
        > X_MAX_WEIGHTED_LENGTH
    ):

        raise ValueError(
            "Unable to reduce X post "
            "below 280 weighted characters. "
            f"Final weight: {final_weight}"
        )

    print(
        "Final X weighted length:",
        final_weight,
    )

    print(
        "Final X post text:"
    )

    print(
        "-" * 60
    )

    print(
        post_text
    )

    print(
        "-" * 60
    )

    return post_text


# ============================================================
# X image
# ============================================================

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


# ============================================================
# OAuth
# ============================================================

def oauth() -> OAuth1:

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


# ============================================================
# Remote duplicate protection
# ============================================================

def find_existing_post(
    issue_date: str,
    auth: OAuth1,
) -> tuple[str, str] | None:

    if not issue_date:

        raise ValueError(
            "issue_date is missing."
        )

    me = requests.get(
        ME_URL,
        auth=auth,
        timeout=30,
    )

    if (
        me.status_code
        != 200
    ):

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

    page_url = (
        "https://www.thedailyduck.ai/"
        f"ducks/{issue_date}/"
    )

    recent = requests.get(
        (
            "https://api.x.com/2/users/"
            f"{user_id}/tweets"
        ),
        auth=auth,
        params={
            "max_results":
                20,

            "exclude":
                "retweets,replies",

            "tweet.fields":
                "created_at",
        },
        timeout=30,
    )

    if (
        recent.status_code
        != 200
    ):

        raise RuntimeError(
            "X duplicate preflight "
            "recent-post lookup failed: "
            f"HTTP {recent.status_code}: "
            f"{recent.text[:1000]}"
        )

    payload = (
        recent.json()
    )

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


# ============================================================
# Media Upload
# ============================================================

def upload_image(
    image_path: Path,
    auth: OAuth1,
) -> str:

    suffix = (
        image_path
        .suffix
        .lower()
    )

    if suffix in (
        ".jpg",
        ".jpeg",
    ):

        mime = (
            "image/jpeg"
        )

    elif suffix == ".png":

        mime = (
            "image/png"
        )

    elif suffix == ".webp":

        mime = (
            "image/webp"
        )

    else:

        raise ValueError(
            "Unsupported X image format: "
            f"{suffix}"
        )

    file_size = (
        image_path
        .stat()
        .st_size
    )

    print(
        "Uploading X image:",
        image_path,
    )

    print(
        "Image bytes:",
        file_size,
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
        "Media upload HTTP:",
        response.status_code,
    )

    if (
        response.status_code
        not in (
            200,
            201,
        )
    ):

        write_result(
            "MEDIA_UPLOAD_FAILED",

            image_path=
                image_path.as_posix(),

            http_status=
                response.status_code,

            response_text=
                response.text[:2000],
        )

        raise RuntimeError(
            "X media upload failed: "
            f"HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )

    payload = (
        response.json()
    )

    data = payload.get(
        "data"
    )

    if not isinstance(
        data,
        dict,
    ):

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

        raise RuntimeError(
            "X media upload response "
            f"missing media ID: {payload}"
        )

    print(
        "X media upload succeeded."
    )

    print(
        "Media ID:",
        media_id,
    )

    return media_id


# ============================================================
# Create X post
# ============================================================

def create_post(
    text: str,
    media_id: str,
    auth: OAuth1,
) -> tuple[
    str,
    dict[str, Any],
]:

    weighted_length = (
        x_weighted_length(
            text
        )
    )

    print(
        "Create Post weighted length:",
        weighted_length,
    )

    if (
        weighted_length
        > X_MAX_WEIGHTED_LENGTH
    ):

        raise ValueError(
            "Refusing X Create Post "
            "because weighted length "
            "exceeds 280: "
            f"{weighted_length}"
        )

    request_payload = {
        "text":
            text,

        "media": {
            "media_ids": [
                media_id
            ]
        },
    }

    print(
        "Creating X post..."
    )

    response = requests.post(
        CREATE_POST_URL,
        auth=auth,
        json=request_payload,
        timeout=60,
    )

    print(
        "Create Post HTTP:",
        response.status_code,
    )

    if (
        response.status_code
        not in (
            200,
            201,
        )
    ):

        write_result(
            "CREATE_POST_FAILED",

            media_id=
                media_id,

            weighted_length=
                weighted_length,

            http_status=
                response.status_code,

            response_text=
                response.text[:2000],

            post_text=
                text,
        )

        raise RuntimeError(
            "X Create Post failed: "
            f"HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )

    response_payload = (
        response.json()
    )

    data = response_payload.get(
        "data"
    )

    if not isinstance(
        data,
        dict,
    ):

        raise RuntimeError(
            "X Create Post response "
            f"missing data: {response_payload}"
        )

    post_id = first_text(
        data.get(
            "id"
        )
    )

    if not post_id:

        raise RuntimeError(
            "X Create Post response "
            f"missing post ID: {response_payload}"
        )

    return (
        post_id,
        response_payload,
    )


# ============================================================
# Main
# ============================================================

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

    # --------------------------------------------------------
    # Website must already be published.
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # READY state must have advanced to PUBLISHED.
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Local duplicate protection.
    # --------------------------------------------------------

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

    final_weight = (
        x_weighted_length(
            post_text
        )
    )

    print(
        "Final post weighted length:",
        final_weight,
    )

    auth = oauth()

    # --------------------------------------------------------
    # Remote duplicate protection.
    # --------------------------------------------------------

    existing = (
        find_existing_post(
            issue_date,
            auth,
        )
    )

    if (
        existing
        is not None
    ):

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
            "an existing post for "
            "this issue was found on X."
        )

        return 0

    # --------------------------------------------------------
    # Upload X card.
    # --------------------------------------------------------

    media_id = (
        upload_image(
            image_path,
            auth,
        )
    )

    # --------------------------------------------------------
    # Create X post.
    #
    # IMPORTANT:
    # Do NOT include made_with_ai here.
    # The exact same payload shape has been
    # verified successfully in the 3-pattern
    # production diagnostic test.
    # --------------------------------------------------------

    (
        post_id,
        response_payload,
    ) = create_post(
        post_text,
        media_id,
        auth,
    )

    post_url = (
        "https://x.com/i/web/"
        f"status/{post_id}"
    )

    # --------------------------------------------------------
    # Persist successful X state.
    # --------------------------------------------------------

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

        weighted_length=
            final_weight,

        post_text=
            post_text,

        response=
            response_payload,
    )

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
