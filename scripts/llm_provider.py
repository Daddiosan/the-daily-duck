#!/usr/bin/env python3
"""Sole retry/fallback owner for the news-ranking LLM operation.

Primary: Gemini. Fallback: OpenAI. See
PHASE_A_RETRY_NORMALIZATION_AND_RANKING_FALLBACK_PLAN.md for the approved
design this module implements. No caller of generate_ranking() may retry
on its own -- every physical provider attempt happens in this file only.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from typing import Any, Callable

import openai
from openai import OpenAI

# ============================================================
# Error taxonomy
# ============================================================

RATE_LIMIT_QUOTA = "RATE_LIMIT_QUOTA"
TEMPORARY_UNAVAILABLE = "TEMPORARY_UNAVAILABLE"
NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
AUTH_FAILURE = "AUTH_FAILURE"
PERMISSION_FAILURE = "PERMISSION_FAILURE"
INVALID_REQUEST = "INVALID_REQUEST"
INVALID_RESPONSE = "INVALID_RESPONSE"
UNKNOWN_PROVIDER_ERROR = "UNKNOWN_PROVIDER_ERROR"
CONFIG_MISSING = "CONFIG_MISSING"

# Categories that stop all further attempts (on either provider)
# immediately: these are configuration/request bugs, not transient
# provider trouble, and silently switching provider would hide them.
FAIL_CLOSED_CATEGORIES = {AUTH_FAILURE, PERMISSION_FAILURE, INVALID_REQUEST}

# Transport-layer categories eligible for a same-provider bounded retry.
GEMINI_RETRYABLE_CATEGORIES = {TEMPORARY_UNAVAILABLE, NETWORK_TIMEOUT}

# OpenAI is the last provider in the chain, so (unlike Gemini) its own
# rate limiting is worth one bounded retry rather than an instant give-up --
# there is no third provider to fall back to. Gemini's quota errors are not
# retried because the same run cannot make the free-tier quota refill.
OPENAI_RETRYABLE_CATEGORIES = {TEMPORARY_UNAVAILABLE, NETWORK_TIMEOUT, RATE_LIMIT_QUOTA}

GEMINI = "gemini"
OPENAI = "openai"

GEMINI_MAX_ATTEMPTS = 2
OPENAI_MAX_ATTEMPTS = 2
HARD_MAX_PROVIDER_CALLS = GEMINI_MAX_ATTEMPTS + OPENAI_MAX_ATTEMPTS

DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"
DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"


class ProviderFailure(RuntimeError):
    """Raised when the ranking operation must fail closed.

    `category` is the taxonomy category that caused the final failure.
    `provider_errors` records what each attempted provider reported, for
    diagnostics, without ever including a secret value.
    """

    def __init__(self, message: str, *, category: str, provider_errors: dict[str, str] | None = None):
        super().__init__(message)
        self.category = category
        self.provider_errors = provider_errors or {}


# ============================================================
# Observability (no secrets are ever passed to these helpers)
# ============================================================

def _log_attempt(operation: str, provider: str, attempt: int, result: str) -> None:
    stream = sys.stderr if result not in ("SUCCESS",) else sys.stdout
    print(
        f"LLM_OPERATION={operation} LLM_PROVIDER={provider} "
        f"LLM_ATTEMPT={attempt} LLM_RESULT={result}",
        file=stream,
    )


def _log_fallback(reason: str) -> None:
    print(
        f"LLM_FALLBACK_TRIGGERED=true LLM_FALLBACK_REASON={reason}",
        file=sys.stderr,
    )


# ============================================================
# Error classification (structured, not string-matching)
# ============================================================

def _classify_http_status(status: int | None) -> str:
    if status == 429:
        return RATE_LIMIT_QUOTA
    if status == 401:
        return AUTH_FAILURE
    if status == 403:
        return PERMISSION_FAILURE
    if status == 400:
        return INVALID_REQUEST
    if status in (500, 502, 503, 504):
        return TEMPORARY_UNAVAILABLE
    return UNKNOWN_PROVIDER_ERROR


def classify_gemini_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return _classify_http_status(exc.code)
    if isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError)):
        return NETWORK_TIMEOUT
    if isinstance(exc, RuntimeError):
        # Raised by _call_gemini_once() when a 200 OK response did not
        # contain the expected candidates/content/parts/text shape
        # (e.g. safety-filtered). Treated as a response-quality problem,
        # not a transport problem.
        return INVALID_RESPONSE
    return UNKNOWN_PROVIDER_ERROR


def classify_openai_error(exc: Exception) -> str:
    if isinstance(exc, openai.RateLimitError):
        return RATE_LIMIT_QUOTA
    if isinstance(exc, openai.AuthenticationError):
        return AUTH_FAILURE
    if isinstance(exc, openai.PermissionDeniedError):
        return PERMISSION_FAILURE
    if isinstance(exc, openai.BadRequestError):
        return INVALID_REQUEST
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return NETWORK_TIMEOUT
    if isinstance(exc, openai.InternalServerError):
        return TEMPORARY_UNAVAILABLE
    if isinstance(exc, openai.APIStatusError):
        return _classify_http_status(getattr(exc, "status_code", None))
    return UNKNOWN_PROVIDER_ERROR


# ============================================================
# JSON extraction (shared by both providers)
# ============================================================

def _clean_json_text(value: str) -> str:
    cleaned = value.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _parse_json(raw_text: str | None) -> Any:
    if not raw_text or not raw_text.strip():
        raise ValueError("Provider returned an empty response.")
    return json.loads(_clean_json_text(raw_text))


# ============================================================
# Gemini transport (raw REST, matching the existing ranking call site)
# ============================================================

def _call_gemini_once(prompt: str, schema: dict, *, api_key: str, model: str) -> str:
    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{model}:generateContent"
        f"?key={api_key}"
    )

    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
        },
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=120) as response:
        response_data = json.loads(response.read().decode("utf-8"))

    try:
        return response_data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            "Unexpected Gemini response structure: "
            f"{json.dumps(response_data, ensure_ascii=False)[:1500]}"
        ) from exc


# ============================================================
# OpenAI transport (fallback)
# ============================================================

def _call_openai_once(prompt: str, *, api_key: str, model: str) -> str:
    client = OpenAI(api_key=api_key)

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )

    if not response.choices:
        return ""

    return response.choices[0].message.content or ""


# ============================================================
# Retry owner
# ============================================================

def generate_ranking(
    prompt: str,
    schema: dict,
    *,
    validate: Callable[[Any], None],
    operation: str = "news_ranking",
    gemini_model: str | None = None,
    openai_model: str | None = None,
) -> tuple[Any, str]:
    """Return (validated_result, provider_name).

    `validate` must raise on an invalid/unacceptable result and must be
    provider-independent (the exact same callable is used for both a
    Gemini-sourced and an OpenAI-sourced response).

    Never retries more than GEMINI_MAX_ATTEMPTS Gemini calls or
    OPENAI_MAX_ATTEMPTS OpenAI calls; total physical provider calls for one
    invocation never exceed HARD_MAX_PROVIDER_CALLS.
    """
    gemini_model = gemini_model or os.environ.get("GEMINI_TEXT_MODEL", "").strip() or DEFAULT_GEMINI_MODEL
    openai_model = openai_model or os.environ.get("OPENAI_TEXT_MODEL", "").strip() or DEFAULT_OPENAI_MODEL

    provider_errors: dict[str, str] = {}

    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        raise ProviderFailure(
            "GEMINI_API_KEY is not configured.",
            category=CONFIG_MISSING,
            provider_errors={"gemini": "GEMINI_API_KEY is not configured."},
        )

    gemini_attempt = 0
    while gemini_attempt < GEMINI_MAX_ATTEMPTS:
        gemini_attempt += 1
        try:
            raw_text = _call_gemini_once(prompt, schema, api_key=gemini_key, model=gemini_model)
        except Exception as exc:
            category = classify_gemini_error(exc)
            _log_attempt(operation, GEMINI, gemini_attempt, category)
            provider_errors["gemini"] = f"{category}: {exc}"

            if category in FAIL_CLOSED_CATEGORIES:
                raise ProviderFailure(
                    f"Gemini failed with non-retryable error: {category}",
                    category=category,
                    provider_errors=provider_errors,
                ) from exc

            if category in GEMINI_RETRYABLE_CATEGORIES and gemini_attempt < GEMINI_MAX_ATTEMPTS:
                continue

            _log_fallback(category)
            break
        else:
            try:
                parsed = _parse_json(raw_text)
                validate(parsed)
            except Exception as exc:
                category = INVALID_RESPONSE
                _log_attempt(operation, GEMINI, gemini_attempt, category)
                provider_errors["gemini"] = f"{category}: {exc}"

                if gemini_attempt < GEMINI_MAX_ATTEMPTS:
                    continue

                _log_fallback(category)
                break
            else:
                _log_attempt(operation, GEMINI, gemini_attempt, "SUCCESS")
                return parsed, GEMINI

    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not openai_key:
        provider_errors["openai"] = "OPENAI_API_KEY is not configured."
        raise ProviderFailure(
            "OpenAI fallback required but OPENAI_API_KEY is not configured.",
            category=CONFIG_MISSING,
            provider_errors=provider_errors,
        )

    openai_attempt = 0
    while openai_attempt < OPENAI_MAX_ATTEMPTS:
        openai_attempt += 1
        try:
            raw_text = _call_openai_once(prompt, api_key=openai_key, model=openai_model)
        except Exception as exc:
            category = classify_openai_error(exc)
            _log_attempt(operation, OPENAI, openai_attempt, category)
            provider_errors["openai"] = f"{category}: {exc}"

            if category in FAIL_CLOSED_CATEGORIES:
                raise ProviderFailure(
                    f"OpenAI failed with non-retryable error: {category}",
                    category=category,
                    provider_errors=provider_errors,
                ) from exc

            if category in OPENAI_RETRYABLE_CATEGORIES and openai_attempt < OPENAI_MAX_ATTEMPTS:
                continue

            break
        else:
            try:
                parsed = _parse_json(raw_text)
                validate(parsed)
            except Exception as exc:
                category = INVALID_RESPONSE
                _log_attempt(operation, OPENAI, openai_attempt, category)
                provider_errors["openai"] = f"{category}: {exc}"
                # No same-provider ping-pong retry for malformed output on
                # the fallback provider -- one attempt is enough here.
                break
            else:
                _log_attempt(operation, OPENAI, openai_attempt, "SUCCESS")
                return parsed, OPENAI

    raise ProviderFailure(
        "Both Gemini and OpenAI failed to produce a valid ranking result.",
        category=UNKNOWN_PROVIDER_ERROR,
        provider_errors=provider_errors,
    )
