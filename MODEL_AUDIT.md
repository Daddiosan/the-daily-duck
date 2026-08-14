# The Daily Duck — Gemini Model Audit

Reviewed: 2026-08-14

Active model policy:
- Text / concepts / titles: `gemini-3.6-flash`
- Hero image generation: `gemini-3.1-flash-lite-image`
- X card: local Pillow render, exactly 1500×1200 (5:4)

Findings:
- A saved story/editorial implementation already used Gemini 3.6 Flash.
- An older Gate-B workflow still contained the obsolete Gemini 2.5 Flash text-model setting.
- The first version of the new Design Options workflow also accidentally contained Gemini 2.5 Flash.
- Those active Phase-2 paths are corrected in this package.
- A model-audit workflow is included so the active paths can be checked before the design test.

The image model is intentionally not changed to 3.6 Flash: 3.6 Flash is the text/reasoning model.
Image generation uses the current Gemini 3.1 Flash Lite Image family.
