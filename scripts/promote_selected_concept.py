#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


APPROVED_STORY = Path(
    "automation_state/approved_story.json"
)

APPROVED_IMAGE = Path(
    "automation_state/approved_image_concept.json"
)

READY = Path(
    "automation_state/ready_to_publish.json"
)

CANONICAL_DIR = Path(
    "automation_state/canonical"
)


def load(path: Path):
    return json.loads(
        path.read_text(encoding="utf-8")
    )


def main() -> None:
    story = load(
        APPROVED_STORY
    )

    selected = load(
        APPROVED_IMAGE
    )

    if selected.get("state") != "APPROVED_IMAGE_CONCEPT":
        raise ValueError(
            "Expected APPROVED_IMAGE_CONCEPT."
        )

    issue = str(
        selected.get("issue_date", "")
    ).strip()

    web_source = Path(
        selected["selected_web_image_path"]
    )

    x_source = Path(
        selected["selected_x_image_path"]
    )

    if not web_source.exists():
        raise FileNotFoundError(
            web_source
        )

    if not x_source.exists():
        raise FileNotFoundError(
            x_source
        )

    CANONICAL_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    web_final = (
        CANONICAL_DIR
        / f"{issue}-web.png"
    )

    x_final = (
        CANONICAL_DIR
        / f"{issue}-x.png"
    )

    shutil.copy2(
        web_source,
        web_final,
    )

    shutil.copy2(
        x_source,
        x_final,
    )

    ready = {
        "date": issue,
        "issue_date": issue,
        "state": "READY_TO_PUBLISH",

        "ready_at": datetime.now(
            timezone.utc
        ).isoformat(),

        "gate_a_approved_story":
            story,

        "selected_image_concept_number":
            selected[
                "selected_image_concept_number"
            ],

        "selected_image_concept":
            selected[
                "selected_image_concept"
            ],

        "selected_candidate":
            selected[
                "selected_image_concept"
            ],

        # Compatibility
        "canonical_image_path":
            web_final.as_posix(),

        # New explicit paths
        "canonical_web_image_path":
            web_final.as_posix(),

        "canonical_x_image_path":
            x_final.as_posix(),

        "x_posted": False,
    }

    READY.write_text(
        json.dumps(
            ready,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        f"WEB canonical: {web_final}"
    )

    print(
        f"X canonical: {x_final}"
    )

    print(
        "STATE: READY_TO_PUBLISH"
    )


if __name__ == "__main__":
    main()
