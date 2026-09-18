#!/usr/bin/env python3
"""Sole retry/fallback owner for LLM operations in this repo: news_ranking,
editorial_generation, and design_options_generation.

Primary: Gemini. Fallback: OpenAI. See
PHASE_A_RETRY_NORMALIZATION_AND_RANKING_FALLBACK_PLAN.md (news_ranking),
PHASE_B_RETRY_NORMALIZATION_AND_EDITORIAL_FALLBACK_PLAN.md
(editorial_generation), and LLM_PROVIDER_AUDIT_AND_FALLBACK_PLAN.md's Phase D
(design_options_generation) for the approved designs this module implements.
generate_ranking(), generate_editorial(), and generate_design_options() are
thin, operation-specific wrappers around the same shared retry/fallback
loop -- no caller of any of them may retry on its own; every physical
provider attempt happens in this file only.
"""
from __future__ import annotations

import copy
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
# Narrow subset of PERMISSION_FAILURE: a *confirmed* Google project-denied
# condition (HTTP 403 + status PERMISSION_DENIED + the specific "denied
# access ... contact support" message), not every 403. See
# _is_confirmed_project_access_denied(). Human-approved policy (Phase B):
# this gets an immediate OpenAI fallback instead of failing closed, because
# unlike an ordinary permission error it is not a request/config bug that
# fallback would silently hide -- it is a known account-level condition that
# has already been diagnosed. Applies to both news_ranking and
# editorial_generation since both share this classifier.
PROJECT_ACCESS_DENIED = "PROJECT_ACCESS_DENIED"
INVALID_REQUEST = "INVALID_REQUEST"
INVALID_RESPONSE = "INVALID_RESPONSE"
UNKNOWN_PROVIDER_ERROR = "UNKNOWN_PROVIDER_ERROR"
CONFIG_MISSING = "CONFIG_MISSING"

# Categories that stop all further attempts (on either provider)
# immediately: these are configuration/request bugs, not transient
# provider trouble, and silently switching provider would hide them.
# PROJECT_ACCESS_DENIED is deliberately NOT included here -- see its
# definition above.
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

# Editorial generation's output is larger than ranking's: 5 stories x 11
# free-text fields (an English master copy plus a full Japanese
# translation per story) versus ranking's 5 stories x 16 mostly-numeric
# fields. Estimated at roughly 3000-5000 visible tokens (see
# PHASE_B_RETRY_NORMALIZATION_AND_EDITORIAL_FALLBACK_PLAN.md); this budget
# adds the same kind of generous reasoning headroom as
# DEFAULT_OPENAI_MAX_COMPLETION_TOKENS does for ranking. Configurable since
# this is a judgment call, not a measured value.
DEFAULT_OPENAI_EDITORIAL_MAX_COMPLETION_TOKENS = 16000

# Design-options output (3 concepts x 9 free-text fields, incl. a
# production-ready image-generation prompt per concept, plus 3 titles x 2
# free-text fields) is smaller in item count than editorial's 5 stories but
# has similarly long individual free-text fields (composition/generation
# prompt directions). Sized between ranking's and editorial's budgets on
# that basis. Configurable since this is a judgment call, not a measured
# value.
DEFAULT_OPENAI_DESIGN_OPTIONS_MAX_COMPLETION_TOKENS = 12000


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
    detail = f"{type(exc).__name__} response_length={length} message={safe_message!r}"

    # Best-effort: only _OpenAIResponseText instances (see _call_openai_once())
    # carry this attribute; a plain str (e.g. in tests, or a Gemini-sourced
    # raw_text) simply has none, so this never raises and never fabricates a
    # value for a provider that doesn't expose one.
    finish_reason = getattr(raw_text, "finish_reason", None)
    if finish_reason is not None:
        detail += f" finish_reason={finish_reason!r}"

    return detail


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


# Confirmed production evidence (Phase B): Google returns this exact text
# on a project denied access to the API, distinct from an ordinary
# permission error. Matched narrowly on both phrases (case-insensitively)
# so wording drift in either phrase alone does not cause a false match,
# while tolerating minor punctuation/formatting differences around them.
_PROJECT_ACCESS_DENIED_MESSAGE_MARKERS = ("denied access", "contact support")


def _is_confirmed_project_access_denied(exc: Exception) -> bool:
    """Narrow, evidence-based detector for the confirmed Google
    project-denied condition. Deliberately requires ALL of: HTTP 403,
    structured status PERMISSION_DENIED, AND the specific denied-access
    message text -- an ordinary 403 (e.g. API_KEY_SERVICE_BLOCKED, a
    restricted key/model binding) must NOT match this and must keep
    failing closed as PERMISSION_FAILURE.
    """
    diagnostic = getattr(exc, "gemini_diagnostic", None)
    if not diagnostic:
        return False

    if diagnostic.get("GEMINI_HTTP_STATUS") != 403:
        return False

    if diagnostic.get("GEMINI_ERROR_STATUS") != "PERMISSION_DENIED":
        return False

    message = (diagnostic.get("GEMINI_ERROR_MESSAGE") or "").lower()
    return all(marker in message for marker in _PROJECT_ACCESS_DENIED_MESSAGE_MARKERS)


def classify_gemini_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 403 and _is_confirmed_project_access_denied(exc):
            return PROJECT_ACCESS_DENIED
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

# JSON Schema keywords that are part of the schema Gemini's responseJsonSchema
# accepts (see _call_gemini_once()) but are NOT in OpenAI Structured Outputs'
# documented strict-mode subset. Sending an unsupported keyword risks the
# request itself being rejected (400 INVALID_REQUEST) -- worse than the bug
# this is fixing, since OpenAI is the last provider in the fallback chain.
# The "exactly N items" cardinality these keywords would have expressed is
# instead carried by the prompt's own explicit JSON-shape description (see
# rank_news_with_ai.build_prompt()'s "OUTPUT FORMAT" section) and enforced,
# as always, by the Python-side validate() callback -- never by the schema
# alone.
_OPENAI_UNSUPPORTED_SCHEMA_KEYWORDS = ("minItems", "maxItems")


def _to_openai_strict_schema(schema: dict) -> dict:
    """Adapts a schema authored for Gemini's responseJsonSchema into one
    safe to send as an OpenAI Structured Outputs strict-mode schema.

    Deep-copies first: Gemini keeps receiving the original, untransformed
    schema unchanged (see _call_gemini_once()); only OpenAI's copy differs.

    Recursively:
    1. Forces additionalProperties=False on every object node -- required
       by OpenAI strict mode, which Gemini's schema dialect does not
       require.
    2. Strips _OPENAI_UNSUPPORTED_SCHEMA_KEYWORDS (see above) from every
       node that has them.
    """
    strict_schema = copy.deepcopy(schema)

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
            for keyword in _OPENAI_UNSUPPORTED_SCHEMA_KEYWORDS:
                node.pop(keyword, None)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(strict_schema)
    return strict_schema


class _OpenAIResponseText(str):
    """str subclass carrying the completion's finish_reason for diagnostics
    (see _describe_content_failure()) without changing _call_openai_once()'s
    string-compatible return contract -- every existing str operation
    (equality, len, json.loads, ...) behaves identically."""

    finish_reason: str | None = None


def _call_openai_once(
    prompt: str,
    schema: dict,
    *,
    api_key: str,
    model: str,
    max_completion_tokens: int | None = None,
) -> str:
    client = OpenAI(api_key=api_key)

    effective_max_completion_tokens = max_completion_tokens or int(
        os.environ.get("OPENAI_MAX_COMPLETION_TOKENS", "").strip()
        or DEFAULT_OPENAI_MAX_COMPLETION_TOKENS
    )

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        # Structured Outputs (strict JSON Schema enforcement): a real
        # production run proved plain json_object mode plus the prompt's own
        # prose was insufficient -- OpenAI returned syntactically valid JSON
        # that nonetheless lacked a "top_five" list of exactly five items,
        # because unlike Gemini (which receives this same `schema` via
        # responseJsonSchema, see _call_gemini_once()), OpenAI was never told
        # the required field names or structure at all. This constrains the
        # response to the same schema Gemini already enforces -- field
        # names, types, and required-ness -- as a model-independent Chat
        # Completions contract, not a gpt-5.6-luna-specific assumption.
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "structured_output",
                "strict": True,
                "schema": _to_openai_strict_schema(schema),
            },
        },
        max_completion_tokens=effective_max_completion_tokens,
    )

    if not response.choices:
        return ""

    choice = response.choices[0]
    finish_reason = getattr(choice, "finish_reason", None)

    if finish_reason == "length":
        raise ValueError(
            "OpenAI response was truncated before completion "
            f"(finish_reason=length, max_completion_tokens={effective_max_completion_tokens})."
        )

    text = _OpenAIResponseText(choice.message.content or "")
    text.finish_reason = finish_reason
    return text


# ============================================================
# Retry owner
#
# _generate_with_fallback() is the single shared implementation behind
# both generate_ranking() (news_ranking) and generate_editorial()
# (editorial_generation). Keeping one implementation means a fix or policy
# change here (e.g. the PROJECT_ACCESS_DENIED classification above)
# automatically applies to both operations instead of being duplicated
# into a second, potentially-diverging retry framework.
# ============================================================

def _generate_with_fallback(
    prompt: str,
    schema: dict,
    *,
    validate: Callable[[Any], None],
    operation: str,
    gemini_model: str,
    openai_model: str,
    openai_max_completion_tokens: int | None = None,
) -> tuple[Any, str]:
    """Return (validated_result, provider_name).

    `validate` must raise on an invalid/unacceptable result and must be
    provider-independent (the exact same callable is used for both a
    Gemini-sourced and an OpenAI-sourced response).

    Never retries more than GEMINI_MAX_ATTEMPTS Gemini calls or
    OPENAI_MAX_ATTEMPTS OpenAI calls; total physical provider calls for one
    invocation never exceed HARD_MAX_PROVIDER_CALLS.
    """
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
            raw_text = _call_openai_once(
                prompt,
                schema,
                api_key=openai_key,
                model=openai_model,
                max_completion_tokens=openai_max_completion_tokens,
            )
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
        f"Both Gemini and OpenAI failed to produce a valid {operation} result.",
        category=UNKNOWN_PROVIDER_ERROR,
        provider_errors=provider_errors,
    )


def generate_ranking(
    prompt: str,
    schema: dict,
    *,
    validate: Callable[[Any], None],
    operation: str = "news_ranking",
    gemini_model: str | None = None,
    openai_model: str | None = None,
) -> tuple[Any, str]:
    """Thin news_ranking wrapper around _generate_with_fallback().

    See _generate_with_fallback() for the retry/fallback contract.
    """
    return _generate_with_fallback(
        prompt,
        schema,
        validate=validate,
        operation=operation,
        gemini_model=(
            gemini_model or os.environ.get("GEMINI_TEXT_MODEL", "").strip() or DEFAULT_GEMINI_MODEL
        ),
        openai_model=(
            openai_model or os.environ.get("OPENAI_TEXT_MODEL", "").strip() or DEFAULT_OPENAI_MODEL
        ),
        # None -> _call_openai_once() falls back to OPENAI_MAX_COMPLETION_TOKENS
        # env / DEFAULT_OPENAI_MAX_COMPLETION_TOKENS, unchanged from before
        # this function was split out of the shared implementation.
        openai_max_completion_tokens=None,
    )


def generate_editorial(
    prompt: str,
    schema: dict,
    *,
    validate: Callable[[Any], None],
    operation: str = "editorial_generation",
    gemini_model: str | None = None,
    openai_model: str | None = None,
) -> tuple[Any, str]:
    """Thin editorial_generation wrapper around _generate_with_fallback().

    Same retry/fallback policy as generate_ranking() (see
    _generate_with_fallback()), with a larger OpenAI completion-token
    budget sized for editorial's bigger output (see
    DEFAULT_OPENAI_EDITORIAL_MAX_COMPLETION_TOKENS).
    """
    openai_max_completion_tokens = int(
        os.environ.get("OPENAI_EDITORIAL_MAX_COMPLETION_TOKENS", "").strip()
        or DEFAULT_OPENAI_EDITORIAL_MAX_COMPLETION_TOKENS
    )

    return _generate_with_fallback(
        prompt,
        schema,
        validate=validate,
        operation=operation,
        gemini_model=(
            gemini_model or os.environ.get("GEMINI_TEXT_MODEL", "").strip() or DEFAULT_GEMINI_MODEL
        ),
        openai_model=(
            openai_model or os.environ.get("OPENAI_TEXT_MODEL", "").strip() or DEFAULT_OPENAI_MODEL
        ),
        openai_max_completion_tokens=openai_max_completion_tokens,
    )


def generate_design_options(
    prompt: str,
    schema: dict,
    *,
    validate: Callable[[Any], None],
    operation: str = "design_options_generation",
    gemini_model: str | None = None,
    openai_model: str | None = None,
) -> tuple[Any, str]:
    """Thin design_options_generation wrapper around _generate_with_fallback().

    Same retry/fallback policy as generate_ranking()/generate_editorial()
    (see _generate_with_fallback()), including the PROJECT_ACCESS_DENIED
    immediate-fallback classification and the ordinary-403/401/400
    fail-closed rule -- both live in the shared classify_gemini_error(), not
    here. Kept as its own function rather than an alias for
    generate_editorial() so the operation name, schema, and OpenAI
    completion-token budget are explicit to this call site (see
    DEFAULT_OPENAI_DESIGN_OPTIONS_MAX_COMPLETION_TOKENS) instead of being
    borrowed from an unrelated operation.
    """
    openai_max_completion_tokens = int(
        os.environ.get("OPENAI_DESIGN_OPTIONS_MAX_COMPLETION_TOKENS", "").strip()
        or DEFAULT_OPENAI_DESIGN_OPTIONS_MAX_COMPLETION_TOKENS
    )

    return _generate_with_fallback(
        prompt,
        schema,
        validate=validate,
        operation=operation,
        gemini_model=(
            gemini_model or os.environ.get("GEMINI_TEXT_MODEL", "").strip() or DEFAULT_GEMINI_MODEL
        ),
        openai_model=(
            openai_model or os.environ.get("OPENAI_TEXT_MODEL", "").strip() or DEFAULT_OPENAI_MODEL
        ),
        openai_max_completion_tokens=openai_max_completion_tokens,
    )
