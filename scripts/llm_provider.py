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

# No cap was set on the OpenAI fallback call before this constant existed.
# Ranking's own output shape (5 stories x 16 fields incl. free-text
# "reason") is roughly 800-2500 visible tokens (see the cost estimate in
# LLM_PROVIDER_AUDIT_AND_FALLBACK_PLAN.md); this budget adds generous
# headroom on top of that so a model that spends part of its completion
# budget on internal reasoning before emitting the JSON is not forced to
# truncate the visible answer. Configurable since this is a judgment call,
# not a measured value.
DEFAULT_OPENAI_MAX_COMPLETION_TOKENS = 8000


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


def _log_detail(provider: str, attempt: int, detail: str) -> None:
    # Bounded, non-sensitive diagnostic: exception type + a short safe
    # message (HTTP error text, JSONDecodeError's own position/msg, or
    # validate_result()'s short structural message -- none of which include
    # secrets, full prompts, or full model output) plus response length.
    # Without this, only the taxonomy category (e.g. INVALID_RESPONSE) was
    # ever visible in logs, which is not enough to tell a JSON parse
    # failure apart from a schema/semantic validation failure.
    print(f"LLM_PROVIDER={provider} LLM_ATTEMPT={attempt} LLM_DETAIL={detail}", file=sys.stderr)


def _describe_content_failure(exc: Exception, raw_text: str | None) -> str:
    length = len(raw_text) if raw_text else 0
    safe_message = str(exc).replace("\n", " ")[:300]
    return f"{type(exc).__name__} response_length={length} message={safe_message!r}"


# ============================================================
# Gemini HTTPError body diagnostics (bounded, no secrets)
#
# Gemini's error responses follow the standard Google API error shape:
#   {"error": {"code": ..., "message": ..., "status": ...,
#              "details": [{"reason": ..., "domain": ...,
#                            "metadata": {"service": ..., "method": ...}}]}}
# This is read defensively -- a differently-shaped or unparseable body
# degrades to all-None fields rather than raising. Never reads/logs the
# request URL (it carries the API key), headers, or the key itself --
# only fields Google's own error body already contains about the error.
# ============================================================

GEMINI_ERROR_BODY_MAX_BYTES = 8192
GEMINI_ERROR_MESSAGE_MAX_CHARS = 300


def _extract_gemini_error_diagnostic(http_error: urllib.error.HTTPError) -> dict[str, Any]:
    diagnostic: dict[str, Any] = {
        "GEMINI_HTTP_STATUS": http_error.code,
        "GEMINI_ERROR_STATUS": None,
        "GEMINI_ERROR_MESSAGE": None,
        "GEMINI_ERROR_REASON": None,
        "GEMINI_ERROR_SERVICE": None,
        "GEMINI_ERROR_METHOD": None,
    }

    try:
        raw_body = http_error.read(GEMINI_ERROR_BODY_MAX_BYTES)
    except Exception:
        return diagnostic

    if not raw_body:
        return diagnostic

    try:
        parsed = json.loads(raw_body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return diagnostic

    if not isinstance(parsed, dict):
        return diagnostic

    error = parsed.get("error")
    if not isinstance(error, dict):
        return diagnostic

    status = error.get("status")
    if isinstance(status, str):
        diagnostic["GEMINI_ERROR_STATUS"] = status

    message = error.get("message")
    if isinstance(message, str):
        diagnostic["GEMINI_ERROR_MESSAGE"] = (
            message.replace("\n", " ")[:GEMINI_ERROR_MESSAGE_MAX_CHARS]
        )

    details = error.get("details")
    if isinstance(details, list):
        for entry in details:
            if not isinstance(entry, dict):
                continue

            reason = entry.get("reason")
            if isinstance(reason, str) and diagnostic["GEMINI_ERROR_REASON"] is None:
                diagnostic["GEMINI_ERROR_REASON"] = reason

            metadata = entry.get("metadata")
            if isinstance(metadata, dict):
                service = metadata.get("service")
                if isinstance(service, str) and diagnostic["GEMINI_ERROR_SERVICE"] is None:
                    diagnostic["GEMINI_ERROR_SERVICE"] = service

                method = metadata.get("method")
                if isinstance(method, str) and diagnostic["GEMINI_ERROR_METHOD"] is None:
                    diagnostic["GEMINI_ERROR_METHOD"] = method

            method_direct = entry.get("method")
            if isinstance(method_direct, str) and diagnostic["GEMINI_ERROR_METHOD"] is None:
                diagnostic["GEMINI_ERROR_METHOD"] = method_direct

    return diagnostic


def _describe_gemini_failure(exc: Exception) -> str:
    diagnostic = getattr(exc, "gemini_diagnostic", None)

    if not diagnostic:
        return _describe_content_failure(exc, None)

    fields = " ".join(f"{key}={value!r}" for key, value in diagnostic.items())
    return f"HTTPError {fields}"


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
    if isinstance(exc, ValueError):
        # Raised by _call_openai_once() itself for a locally-detected
        # response-quality problem (e.g. finish_reason=length truncation),
        # not a transport/HTTP failure.
        return INVALID_RESPONSE
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

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as http_error:
        http_error.gemini_diagnostic = _extract_gemini_error_diagnostic(http_error)
        raise

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

    max_completion_tokens = int(
        os.environ.get("OPENAI_MAX_COMPLETION_TOKENS", "").strip()
        or DEFAULT_OPENAI_MAX_COMPLETION_TOKENS
    )

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        # JSON mode: a standard, model-independent Chat Completions
        # contract (not a gpt-5.6-luna-specific assumption) that makes the
        # API itself reject/repair non-JSON output, rather than relying
        # solely on the prompt's own "Return only the requested JSON"
        # instruction the way the un-schema'd editorial/design-option
        # prompts elsewhere in this repo already do.
        response_format={"type": "json_object"},
        max_completion_tokens=max_completion_tokens,
    )

    if not response.choices:
        return ""

    choice = response.choices[0]
    finish_reason = getattr(choice, "finish_reason", None)

    if finish_reason == "length":
        raise ValueError(
            "OpenAI response was truncated before completion "
            f"(finish_reason=length, max_completion_tokens={max_completion_tokens})."
        )

    return choice.message.content or ""


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
            detail = _describe_gemini_failure(exc)
            _log_attempt(operation, GEMINI, gemini_attempt, category)
            _log_detail(GEMINI, gemini_attempt, detail)
            provider_errors["gemini"] = detail

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
                detail = _describe_content_failure(exc, raw_text)
                _log_attempt(operation, GEMINI, gemini_attempt, category)
                _log_detail(GEMINI, gemini_attempt, detail)
                provider_errors["gemini"] = detail

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
            detail = _describe_content_failure(exc, None)
            _log_attempt(operation, OPENAI, openai_attempt, category)
            _log_detail(OPENAI, openai_attempt, detail)
            provider_errors["openai"] = detail

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
                detail = _describe_content_failure(exc, raw_text)
                _log_attempt(operation, OPENAI, openai_attempt, category)
                _log_detail(OPENAI, openai_attempt, detail)
                provider_errors["openai"] = detail
                # No same-provider ping-pong retry for malformed output on
                # the fallback provider -- one attempt is enough here.
                break
            else:
                _log_attempt(operation, OPENAI, openai_attempt, "SUCCESS")
                return parsed, OPENAI

    print(
        f"LLM_ALL_PROVIDERS_FAILED provider_errors={provider_errors}",
        file=sys.stderr,
    )

    raise ProviderFailure(
        "Both Gemini and OpenAI failed to produce a valid ranking result.",
        category=UNKNOWN_PROVIDER_ERROR,
        provider_errors=provider_errors,
    )
