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

APPROVED_IMAGE_PATH = Path(
    "automation_state/approved_image_concept.json"
)

IMAGE_CONCEPTS_PATH = Path(
    "automation_state/image_concepts.json"
)

READY_PATH = Path(
    "automation_state/ready_to_publish.json"
)

CANONICAL_DIR = Path(
    "automation_state/canonical"
)


# ============================================================
# JSON helpers
# ============================================================

def load_json(
    path: Path,
) -> dict[str, Any]:

    if not path.exists():
        raise FileNotFoundError(
            f"Required file not found: {path}"
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


def now_iso() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


# ============================================================
# Locate the CURRENT selected concept
# ============================================================

def resolve_selected_concept(
    approved_image: dict[str, Any],
    image_concepts: dict[str, Any],
) -> tuple[int, dict[str, Any]]:

    try:
        selected_number = int(
            approved_image.get(
                "selected_image_concept_number",
                0,
            )
        )

    except Exception:
        selected_number = 0

    if selected_number not in (
        1,
        2,
        3,
    ):
        raise ValueError(
            "selected_image_concept_number "
            "must be 1, 2, or 3."
        )

    concepts = image_concepts.get(
        "concepts"
    )

    if (
        not isinstance(concepts, list)
        or len(concepts) != 3
    ):
        raise ValueError(
            "Current image_concepts.json must "
            "contain exactly 3 concepts."
        )

    # IMPORTANT:
    # Use the CURRENT image_concepts.json as source of truth.
    #
    # This avoids stale approved_image_concept.json data
    # from the older 5-concept / preview-only workflow.

    current_concept = None

    for concept in concepts:

        if not isinstance(
            concept,
            dict,
        ):
            continue

        try:
            number = int(
                concept.get(
                    "number",
                    0,
                )
            )

        except Exception:
            continue

        if number == selected_number:
            current_concept = dict(
                concept
            )
            break

    if current_concept is None:
        raise ValueError(
            f"Current concept #{selected_number} "
            "was not found."
        )

    return (
        selected_number,
        current_concept,
    )


# ============================================================
# Resolve WEB and X source images
# ============================================================

def resolve_source_images(
    approved_image: dict[str, Any],
    current_concept: dict[str, Any],
) -> tuple[Path, Path]:

    # Prefer current 3-set assets.
    web_image = first_text(
        current_concept.get(
            "web_image_path"
        ),
        approved_image.get(
            "selected_web_image_path"
        ),
    )

    x_image = first_text(
        current_concept.get(
            "x_image_path"
        ),
        approved_image.get(
            "selected_x_image_path"
        ),
    )

    if not web_image:
        raise ValueError(
            "Selected image set has no WEB image path."
        )

    if not x_image:
        raise ValueError(
            "Selected image set has no X image path."
        )

    web_path = Path(
        web_image
    )

    x_path = Path(
        x_image
    )

    if not web_path.exists():
        raise FileNotFoundError(
            f"Selected WEB image not found: {web_path}"
        )

    if not x_path.exists():
        raise FileNotFoundError(
            f"Selected X image not found: {x_path}"
        )

    return (
        web_path,
        x_path,
    )


# ============================================================
# Main
# ============================================================

def main() -> int:

    approved_story = load_json(
        APPROVED_STORY_PATH
    )

    approved_image = load_json(
        APPROVED_IMAGE_PATH
    )

    image_concepts = load_json(
        IMAGE_CONCEPTS_PATH
    )

    # --------------------------------------------------------
    # Validate story
    # --------------------------------------------------------

    if (
        approved_story.get(
            "state"
        )
        != "APPROVED_STORY"
    ):
        raise ValueError(
            "approved_story.json must have "
            "state APPROVED_STORY."
        )

    # --------------------------------------------------------
    # Validate image review state
    # --------------------------------------------------------

    concepts_state = str(
        image_concepts.get(
            "state",
            "",
        )
    ).strip().upper()

    if (
        concepts_state
        != "IMAGE_CONCEPT_REVIEW"
    ):
        raise ValueError(
            "image_concepts.json must have "
            "state IMAGE_CONCEPT_REVIEW; "
            f"got {concepts_state!r}."
        )

    # --------------------------------------------------------
    # Determine issue date
    # --------------------------------------------------------

    issue_date = first_text(
        image_concepts.get(
            "issue_date"
        ),
        approved_image.get(
            "issue_date"
        ),
        approved_story.get(
            "issue_date"
        ),
        approved_story.get(
            "date"
        ),
    )

    if not issue_date:
        raise ValueError(
            "issue_date is missing."
        )

    # --------------------------------------------------------
    # Resolve selected number + CURRENT concept
    # --------------------------------------------------------

    (
        selected_number,
        selected_concept,
    ) = resolve_selected_concept(
        approved_image,
        image_concepts,
    )

    print(
        f"Selected image set: {selected_number}"
    )

    # --------------------------------------------------------
    # Resolve WEB + X source images
    # --------------------------------------------------------

    (
        web_source,
        x_source,
    ) = resolve_source_images(
        approved_image,
        selected_concept,
    )

    print(
        f"WEB source: {web_source}"
    )

    print(
        f"X source: {x_source}"
    )

    # --------------------------------------------------------
    # Freeze canonical assets
    # --------------------------------------------------------

    CANONICAL_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    web_extension = (
        web_source.suffix.lower()
        or ".png"
    )

    x_extension = (
        x_source.suffix.lower()
        or ".png"
    )

    canonical_web = (
        CANONICAL_DIR
        / f"{issue_date}-web{web_extension}"
    )

    canonical_x = (
        CANONICAL_DIR
        / f"{issue_date}-x{x_extension}"
    )

    shutil.copy2(
        web_source,
        canonical_web,
    )

    shutil.copy2(
        x_source,
        canonical_x,
    )

    # --------------------------------------------------------
    # Upgrade approved_image_concept.json to CURRENT schema
    # --------------------------------------------------------

    approved_image[
        "state"
    ] = "APPROVED_IMAGE_CONCEPT"

    approved_image[
        "issue_date"
    ] = issue_date

    approved_image[
        "date"
    ] = issue_date

    approved_image[
        "selected_image_concept_number"
    ] = selected_number

    approved_image[
        "selected_image_concept"
    ] = selected_concept

    approved_image[
        "selected_web_image_path"
    ] = web_source.as_posix()

    approved_image[
        "selected_x_image_path"
    ] = x_source.as_posix()

    approved_image[
        "schema_upgraded_at"
    ] = now_iso()

    write_json(
        APPROVED_IMAGE_PATH,
        approved_image,
    )

    # --------------------------------------------------------
    # READY_TO_PUBLISH
    # --------------------------------------------------------

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
            selected_number,

        "selected_image_concept":
            selected_concept,

        # publish_website.py compatibility
        "selected_candidate":
            selected_concept,

        # Existing website compatibility:
        # Website uses this image.
        "canonical_image_path":
            canonical_web.as_posix(),

        # Explicit new paths
        "canonical_web_image_path":
            canonical_web.as_posix(),

        "canonical_x_image_path":
            canonical_x.as_posix(),

        "canonical_web_source_path":
            web_source.as_posix(),

        "canonical_x_source_path":
            x_source.as_posix(),

        "canonical_selection_method":
            "WEB_X_IMAGE_SET_SELECTION",

        "canonical_selection_reply":
            str(
                selected_number
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

    # --------------------------------------------------------
    # Report
    # --------------------------------------------------------

    print(
        "Selected WEB/X image set promoted "
        "to canonical assets."
    )

    print(
        f"Selected image set: {selected_number}"
    )

    print(
        f"Canonical WEB image: {canonical_web}"
    )

    print(
        f"Canonical X image: {canonical_x}"
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
