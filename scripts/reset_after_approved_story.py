#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
from pathlib import Path

APPROVED = Path("automation_state/approved_story.json")

STALE_FILES = [
    Path("automation_state/design_options.json"),
    Path("automation_state/approved_image_concept.json"),
    Path("automation_state/image_concepts.json"),
    Path("automation_state/image_candidates.json"),
    Path("automation_state/gate_b_result.json"),
    Path("automation_state/design_selection_result.json"),
    Path("automation_state/ready_to_publish.json"),
    Path("automation_state/website_publish_result.json"),
    Path("automation_state/x_publish_result.json"),
]

STALE_DIRS = [
    Path("automation_images/design_previews"),
    Path("automation_images/canonical"),
    Path("automation_images/x"),
]

def main() -> int:
    if not APPROVED.exists():
        raise FileNotFoundError("automation_state/approved_story.json is missing.")

    data = json.loads(APPROVED.read_text(encoding="utf-8"))
    if data.get("state") != "APPROVED_STORY":
        raise RuntimeError(
            f"Expected APPROVED_STORY, got {data.get('state')!r}"
        )

    for path in STALE_FILES:
        if path.exists():
            path.unlink()
            print("Removed stale file:", path)

    for path in STALE_DIRS:
        if path.exists():
            shutil.rmtree(path)
            print("Removed stale directory:", path)

    print("APPROVED_STORY preserved.")
    print("Downstream state reset complete.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
