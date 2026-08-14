#!/usr/bin/env python3
from pathlib import Path

required = [
    Path("automation_state/approved_story.json"),
    Path("scripts/model_config.py"),
    Path("scripts/reset_after_approved_story.py"),
    Path("scripts/generate_image_concepts.py"),
    Path("scripts/generate_design_previews.py"),
    Path("scripts/send_design_approval_email.py"),
    Path(".github/workflows/design-options.yml"),
]

errors = []
for p in required:
    if not p.exists():
        errors.append(f"MISSING: {p}")

combined = "\n".join(
    p.read_text(encoding="utf-8")
    for p in required[1:]
    if p.exists()
)

if "gemini-3.1-flash-lite-image" in combined:
    errors.append("Old Gemini image generator is still wired into recovery flow.")
if "gemini-3.6-flash" not in combined:
    errors.append("Gemini 3.6 Flash text model not found.")
if "gpt-image-2" not in combined:
    errors.append("GPT Image 2 not found.")
if "OPENAI_API_KEY" not in combined:
    errors.append("OPENAI_API_KEY is not wired into recovery flow.")

if errors:
    print("RECOVERY AUDIT FAILED")
    for e in errors:
        print("-", e)
    raise SystemExit(1)

print("RECOVERY AUDIT PASSED")
print("Start state: APPROVED_STORY")
print("Text: Gemini 3.6 Flash")
print("Images: OpenAI GPT Image 2")
print("Target: one email with 3 images + 3 titles")
