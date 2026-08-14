from __future__ import annotations

import random
import re
import time
from typing import Callable, TypeVar

T = TypeVar("T")

_RETRY_PATTERNS = (
    re.compile(r"retry(?:Delay| in)[^0-9]*(\d+(?:\.\d+)?)\s*s", re.I),
    re.compile(r"Please retry in\s+(\d+(?:\.\d+)?)\s*s", re.I),
)

def _is_retryable(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return (
        "429" in text
        or "resource_exhausted" in text
        or "too_many_requests" in text
        or "rate limit" in text
        or "quota exceeded" in text
    )

def _server_delay(exc: Exception) -> float | None:
    text = str(exc)
    for pattern in _RETRY_PATTERNS:
        m = pattern.search(text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None

def call_with_retry(
    fn: Callable[[], T],
    *,
    label: str = "Gemini request",
    max_attempts: int = 4,
) -> T:
    """Retry temporary Gemini 429/resource-exhausted failures.

    Honors Gemini's suggested retry delay when present. Otherwise uses
    conservative exponential backoff. Permanent errors are raised immediately.
    """
    last: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if not _is_retryable(exc) or attempt >= max_attempts:
                raise

            suggested = _server_delay(exc)
            fallback = min(180.0, 45.0 * (2 ** (attempt - 1)))
            wait = max(suggested or 0.0, fallback) + random.uniform(1.0, 4.0)

            print(
                f"{label}: temporary Gemini rate limit "
                f"(attempt {attempt}/{max_attempts}). "
                f"Waiting {wait:.1f}s before retry."
            )
            time.sleep(wait)

    assert last is not None
    raise last
