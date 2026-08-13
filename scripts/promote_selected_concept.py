#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APPROVED_STORY_PATH = Path(
    "automation_state/approved_story.json"
)

APPROVED_CONCEPT_PATH = Path(
    "automation_state/approved_image_concept.json"
)

READY_PATH = Path(
    "automation_state/ready_to_publish.json"
)

CANONICAL_DIR = Path(
    "automation_state/canonical"
)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Required file not found: {path}"
        )

    data = json.loads(
        path.read_text(encoding="utf-8")
    )

    if not isinstance(data, dict):
        raise ValueError(
            f"{path} must contain a JSON object."
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


def main() -> int:
    approved_story = load_json(
        APPROVED_STORY_PATH
    )

    approved_concept = load_json(
        APPROVED_CONCEPT_PATH
    )

    if (
        approved_story.get("state")
        != "APPROVED_STORY"
    ):
        raise ValueError(
            "approved_story.json must have "
            "state APPROVED_STORY."
        )

    if (
        approved_concept.get("state")
        != "APPROVED_IMAGE_CONCEPT"
    ):
        raise ValueError(
            "approved_image_concept.json must have "
            "state APPROVED_IMAGE_CONCEPT."
        )

    issue_date = first_text(
        approved_concept.get("issue_date"),
        approved_concept.get("date"),
        approved_story.get("issue_date"),
        approved_story.get("date"),
    )

    if not issue_date:
        raise ValueError(
            "issue_date is missing."
        )

    concept_number = int(
        approved_concept.get(
            "selected_image_concept_number",
            0,
        )
    )

    if concept_number not in (
        1, 2, 3, 4, 5
    ):
        raise ValueError(
            "selected_image_concept_number "
            "must be 1-5."
        )

    selected_concept = (
        approved_concept.get(
            "selected_image_concept"
        )
    )

    if not isinstance(
        selected_concept,
        dict,
    ):
        raise ValueError(
            "selected_image_concept is missing."
        )

    preview_image_path = first_text(
        selected_concept.get(
            "preview_image_path"
        )
    )

    if not preview_image_path:
        raise ValueError(
            "Selected concept has no "
            "preview_image_path."
        )

    preview_path = Path(
        preview_image_path
    )

    if not preview_path.exists():
        raise FileNotFoundError(
            "Selected preview image does not exist: "
            f"{preview_path}"
        )

    # --------------------------------------------------------
    # Freeze selected preview as the canonical image.
    # --------------------------------------------------------

    CANONICAL_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    extension = (
        preview_path.suffix.lower()
        or ".png"
    )

    canonical_path = (
        CANONICAL_DIR
        / f"{issue_date}-canonical{extension}"
    )

    shutil.copy2(
        preview_path,
        canonical_path,
    )

    ready = {
        "date":
            issue_date,

        "issue_date":
            issue_date,

        "state":
            "READY_TO_PUBLISH",

        "ready_at":
            now_iso(),

        "gate_a_approved_story":
            approved_story,

        "selected_image_concept_number":
            concept_number,

        "selected_image_concept":
            selected_concept,

        # publish_website.py uses this as selected_candidate
        # for alt-text/concept information.
        "selected_candidate":
            selected_concept,

        "canonical_image_path":
            canonical_path.as_posix(),

        "canonical_source_image_path":
            preview_path.as_posix(),

        "canonical_selection_method":
            "IMAGE_CONCEPT_PREVIEW_SELECTION",

        "canonical_selection_reply":
            approved_concept.get(
                "selection_reply"
            ),

        "canonical_selected_at":
            approved_concept.get(
                "selected_at"
            ),

        "website_published":
            False,

        "x_posted":
            False,
    }

    write_json(
        READY_PATH,
        ready,
    )

    print(
        "Selected image concept promoted "
        "directly to canonical image."
    )

    print(
        f"Selected concept: {concept_number}"
    )

    print(
        f"Canonical image: {canonical_path}"
    )

    print(
        "STATE: READY_TO_PUBLISH"
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
