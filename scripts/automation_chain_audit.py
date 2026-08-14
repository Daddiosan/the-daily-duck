#!/usr/bin/env python3
from pathlib import Path

checks = {
    ".github/workflows/daily-duck.yml": [
        "workflow_dispatch",
        'cron: "0 22 * * *"',
    ],
    ".github/workflows/approval-check-phase2.yml": [
        "workflow_dispatch",
        "*/15 * * * *",
        "Trigger Design Options automatically",
        "gh workflow run design-options.yml",
    ],
    ".github/workflows/design-options.yml": [
        "workflow_dispatch",
        "force_regenerate",
        "Generate exactly 3 real images with OpenAI",
    ],
    ".github/workflows/design-selection-check.yml": [
        "workflow_dispatch",
        "*/15 * * * *",
        "Trigger Website Publish automatically",
        "gh workflow run website-publish.yml",
    ],
    ".github/workflows/website-publish.yml": [
        "workflow_dispatch",
        "Trigger X Publish",
        "gh workflow run x-publish.yml",
    ],
    ".github/workflows/x-publish.yml": [
        "workflow_dispatch",
        "daily-duck-x-publish",
    ],
    "scripts/check_design_selection.py": [
        "ALREADY_SELECTED",
        "----- 引用元メッセージ -----",
    ],
    "scripts/publish_x.py": [
        "find_existing_post",
        "ALREADY_POSTED_REMOTE_BLOCKED",
    ],
}

errors = []
for name, needles in checks.items():
    p = Path(name)
    if not p.exists():
        errors.append(f"MISSING: {name}")
        continue
    text = p.read_text(encoding="utf-8")
    for needle in needles:
        if needle not in text:
            errors.append(f"{name}: missing {needle!r}")

if errors:
    print("AUTOMATION CHAIN AUDIT FAILED")
    for e in errors:
        print("-", e)
    raise SystemExit(1)

print("AUTOMATION CHAIN AUDIT PASSED")
print("Automatic path:")
print("07:00 JST Daily Duck -> Gate A checker -> Design Options ->")
print("Design Selection checker -> Website Publish -> X Publish")
print("Manual workflow_dispatch remains on every major workflow.")
