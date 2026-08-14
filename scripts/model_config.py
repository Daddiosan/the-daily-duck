from __future__ import annotations
import os

TEXT_MODEL = (
    os.getenv("GEMINI_TEXT_MODEL") or ""
).strip() or "gemini-3.6-flash"

OPENAI_IMAGE_MODEL = (
    os.getenv("OPENAI_IMAGE_MODEL") or ""
).strip() or "gpt-image-2"

OPENAI_IMAGE_SIZE = (
    os.getenv("OPENAI_IMAGE_SIZE") or ""
).strip() or "1536x1024"

OPENAI_IMAGE_QUALITY = (
    os.getenv("OPENAI_IMAGE_QUALITY") or ""
).strip() or "medium"
