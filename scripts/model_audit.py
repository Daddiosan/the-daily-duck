#!/usr/bin/env python3
from pathlib import Path

ACTIVE = [
    Path("scripts/model_config.py"),
    Path("scripts/generate_image_concepts.py"),
    Path("scripts/generate_design_previews.py"),
    Path(".github/workflows/design-options.yml"),
    Path(".github/workflows/design-selection-check.yml"),
]

FORBIDDEN = [
    "gemini-" + "2.5-flash",
]

EXPECTED_TEXT = "gemini-3.6-flash"
EXPECTED_IMAGE = "gemini-3.1-flash-lite-image"

errors = []
all_text = ""

for path in ACTIVE:
    if not path.exists():
        errors.append(f"MISSING: {path}")
        continue

    text = path.read_text(encoding="utf-8")
    all_text += "\n" + text

    for model in FORBIDDEN:
        if model in text:
            errors.append(f"{path}: obsolete model {model}")

if EXPECTED_TEXT not in all_text:
    errors.append(f"Missing text model: {EXPECTED_TEXT}")

if EXPECTED_IMAGE not in all_text:
    errors.append(f"Missing image model: {EXPECTED_IMAGE}")

if errors:
    print("MODEL AUDIT FAILED")
    for error in errors:
        print("-", error)
    raise SystemExit(1)

print("MODEL AUDIT PASSED")
print("Text model:", EXPECTED_TEXT)
print("Preview image model:", EXPECTED_IMAGE)
