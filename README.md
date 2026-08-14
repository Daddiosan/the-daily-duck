# The Daily Duck — Gemini 3.6 Reviewed Complete Patch

Replace the matching files in GitHub.

## scripts/
- model_config.py
- generate_image_concepts.py
- send_image_concept_email.py
- check_image_concept_selection.py
- generate_image_candidates.py
- send_image_selection_email.py
- check_image_selection.py
- build_x_card.py
- model_audit.py

## .github/workflows/
- image-concepts.yml
- image-concept-selection.yml
- image-generation.yml
- image-selection-check.yml
- x-publish.yml
- model-audit.yml

## repository root
- requirements-phase2.txt

## Flow
Story approval
→ 3 image concepts + 3 duck-themed titles
→ reply like `2 1`
→ 5 actual images from that ONE selected concept/title
→ reply `1`–`5`, or `NEXT 5`
→ website canonical image
→ website publish
→ X card 1500×1200 (5:4)
→ X publish

## Test order
1. Upload/replace the files.
2. Actions → The Daily Duck - Model Audit → Run workflow.
3. Confirm green.
4. Actions → Daily Duck Design Options → Run workflow.
5. Confirm the email has exactly 3 image concepts + 3 title ideas.
6. Do NOT run X Publish during this test.
