from __future__ import annotations
import os

TEXT_MODEL = (os.getenv("GEMINI_TEXT_MODEL") or "").strip() or "gemini-3.6-flash"
IMAGE_MODEL = (os.getenv("GEMINI_IMAGE_MODEL") or "").strip() or "gemini-3.1-flash-lite-image"
IMAGE_ASPECT_RATIO = (os.getenv("GEMINI_IMAGE_ASPECT_RATIO") or "").strip() or "16:9"
IMAGE_SIZE = (os.getenv("GEMINI_IMAGE_SIZE") or "").strip() or "1K"
