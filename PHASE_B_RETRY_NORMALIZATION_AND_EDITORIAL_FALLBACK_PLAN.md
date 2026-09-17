# The Daily Duck — Phase B: Retry Normalization & Editorial-Generation Fallback

Status: **IMPLEMENTED** (this document was written as the approved plan and is
now updated to record what was actually built; see Implementation Summary at
the end). Not pushed. Human review/push is a separate, later step.

Planned/implemented: 2026-09-17 / 2026-09-18
Builds on: `PHASE_A_RETRY_NORMALIZATION_AND_RANKING_FALLBACK_PLAN.md` and
`scripts/llm_provider.py` (the module that plan introduced).
Human decision (approved before implementation): confirmed
`PROJECT_ACCESS_DENIED` uses ONE shared classifier in `llm_provider.py` and
therefore also changes `news_ranking`'s 403 handling for this one narrow,
confirmed condition — not a send_email-specific duplicate.

---

## Problem restated

`scripts/send_email.py`'s editorial-generation call had two independent,
un-owned retry layers:

- Outer semantic-validation retry: `EDITORIAL_MAX_ATTEMPTS = 3` (re-runs the
  entire generation, including a fresh Gemini call, on invalid/incomplete
  output).
- Inner Gemini transport retry: `GEMINI_API_MAX_ATTEMPTS = 5` (429/5xx/timeout
  backoff), invoked once per outer attempt.

Physical ceiling: **3 × 5 = 15** Gemini requests for one logical
`editorial_generation` operation, with no fallback provider and no
quota-aware short-circuit (429 was retried like a transient 503). Confirmed
in production: a real run hit 503 retries, an outer re-attempt, then 429
against the `generate_content_free_tier_requests` quota (limit 20), then
final failure. Separately, Gemini has also returned a confirmed
project-denied 403 (`PERMISSION_DENIED` + "Your project has been denied
access. Please contact support.").

The Google GenAI SDK itself was verified (via `google-genai` 2.20.0's
`_api_client.py`, `retry_args()`/`HttpOptions` defaults) to add **no**
internal retry when `genai.Client()` is constructed without `http_options`,
which is exactly how `send_email.py` used it — so 15 was the true, exact
physical ceiling, not an underestimate. Phase B removes the SDK from this
call path entirely (moves to the same raw-REST transport
`scripts/llm_provider.py` already uses for ranking), which makes the
SDK-internal-retry question structurally moot rather than something to
configure defensively.

---

## Design implemented

### One retry owner: `scripts/llm_provider.py`

`generate_ranking()` (news_ranking) and the new `generate_editorial()`
(editorial_generation) are now both thin wrappers around one shared
implementation, `_generate_with_fallback()`. `scripts/send_email.py` no
longer contains any retry loop, backoff, or Gemini-SDK client construction
of its own — it builds a prompt/schema/validator once and calls
`llm_provider.generate_editorial()` exactly once per logical operation.

```
Gemini (max 2 application-level attempts)
  |
  +-- SUCCESS -> validate -> return
  |
  +-- 503/network/timeout -> bounded retry (same provider, once) -> then fallback
  |
  +-- 429 RESOURCE_EXHAUSTED -> zero further Gemini attempts -> immediate OpenAI fallback
  |
  +-- confirmed PROJECT_ACCESS_DENIED -> zero further Gemini attempts -> immediate OpenAI fallback
  |
  +-- generic 401/403/400 -> FAIL CLOSED (no fallback)
  |
  +-- invalid/incomplete JSON output -> same-provider bounded retry, then fallback
       (folds the old "semantic validation retry" into the SAME attempt
       budget as transport retries -- no second hidden loop)

OpenAI (max 2 application-level attempts)
  |
  +-- valid success -> SAME validator as Gemini's path -> return
  |
  +-- transient/timeout/rate-limit -> bounded retry (once)
  |
  +-- invalid/incomplete JSON output -> no same-provider ping-pong, fail closed
  |
  +-- exhausted -> ProviderFailure (fail closed, no email/package written)
```

### PROJECT_ACCESS_DENIED classification (shared taxonomy)

Added to `scripts/llm_provider.py`'s error taxonomy as a narrow subset of
`PERMISSION_FAILURE`, detected by `_is_confirmed_project_access_denied()`
using the diagnostic fields `_extract_gemini_error_diagnostic()` already
extracts (shipped in commit `cdbd186`). Requires **all** of:

- HTTP status `403`
- `GEMINI_ERROR_STATUS == "PERMISSION_DENIED"`
- `GEMINI_ERROR_MESSAGE` contains both `"denied access"` and
  `"contact support"` (case-insensitive)

Any 403 not matching all three conditions (e.g. `API_KEY_SERVICE_BLOCKED`, an
ordinary restricted key/model binding) stays `PERMISSION_FAILURE` and fails
closed exactly as before. Because the classifier lives in the shared
`classify_gemini_error()`, this human-approved policy change applies to
**both** `news_ranking` and `editorial_generation` — proven by dedicated
regression tests in `tests/test_llm_provider.py`
(`ProjectAccessDeniedEndToEndTests`) showing `generate_ranking()` now falls
back on the confirmed condition while an ordinary 403 still fails closed.

### Output contract (unchanged, now provider-independent)

`scripts/send_email.py` still expects exactly the pre-existing contract:
`{"stories": [5 objects]}`, original story IDs preserved in original order,
11 required non-empty string fields per story (`title_en`, `reason_en`,
`en_copy`, `duck_name`, `duck_en`, `x_en`, `title_ja`, `reason_ja`,
`jp_copy`, `duck_jp`, `x_jp`). The inline validation that used to live inside
`generate_five_editorial_packages()` was extracted, unchanged in substance,
into `validate_editorial_result()` and is applied identically regardless of
which provider produced the JSON. `combine_editorial_output()` (also
extracted, unchanged in substance) merges the validated editorial fields
back onto the original ranked-story dict, exactly as before.

### OpenAI fallback sizing

Editorial output (5 stories × 11 free-text fields, English master + full
Japanese translation) is larger than ranking's mostly-numeric output —
estimated 3,000–5,000 visible tokens versus ranking's 800–2,500. Added
`DEFAULT_OPENAI_EDITORIAL_MAX_COMPLETION_TOKENS = 16000` (vs. ranking's
`8000`), threaded through a new optional `max_completion_tokens` parameter on
`_call_openai_once()` (defaults preserve ranking's exact prior behavior when
not passed). Still one logical request containing all five stories — not
split into five calls. Reuses the existing `OPENAI_API_KEY` secret and the
existing `gpt-5.6-luna` model; no model change.

---

## Hard request budget

| | Before | After |
|---|---|---|
| Gemini max application-level attempts | 5 (× 3 outer = 15 physical) | **2** |
| OpenAI max application-level attempts | n/a (no fallback existed) | **2** |
| Hard total provider-call ceiling | 15 (Gemini only) | **4** |
| Normal-path calls | 1 | 1 |
| Worst-case failure calls | 15 | ≤4 (exactly 4 for transport-retry exhaustion; 3 when the fallback provider's failure is a same-attempt invalid-output rejection, since that path takes no second OpenAI attempt) |

---

## Tests

`tests/test_llm_provider.py`: 6 new tests (52 → 58) —
`ProjectAccessDeniedClassificationTests` (4) and
`ProjectAccessDeniedEndToEndTests` (2, covering task items 21–22: confirmed
condition falls back through `generate_ranking()`, ordinary 403 still fails
closed).

`tests/test_send_email.py` (new file, 22 tests) covers task items 1–20 against
`generate_five_editorial_packages()`/`main()`: first-attempt success; bounded
503 retry then success; repeated 503 fallback; immediate 429 fallback;
confirmed `PROJECT_ACCESS_DENIED` immediate fallback; generic 403/401/400 fail
closed; OpenAI-sourced output normalized to the identical contract; malformed
JSON and schema-invalid OpenAI output both fail safely; the hard ceiling holds
under semantic (invalid-output) failure on both providers; the editorial
completion-token budget is confirmed larger than ranking's; validator-level
coverage of missing fields / ID reordering / wrong story count; `main()`
performs zero file/SMTP side effects when both providers fail and exactly one
package write + one email-text write + one SMTP send on success; missing
`OPENAI_API_KEY` behaves safely both when Gemini succeeds and when fallback
would be required.

All 99 repo tests pass (`python -m unittest discover -s tests`): 71 pre-existing
+ 6 + 22. No live Gemini/OpenAI call was made anywhere in this suite.

---

## Workflow wiring

`.github/workflows/daily-duck.yml`: added `OPENAI_API_KEY:
${{ secrets.OPENAI_API_KEY }}` to the "Send Daily Duck email" step's `env:`
only. No other step, cron, schedule, trigger, permission, or concurrency
setting was touched. The Gate A cron A/B experiment, Design Selection cron,
and Automation Chain Audit thresholds are unchanged.

---

## Idempotency / side effects (unchanged ordering)

LLM generation (including all retry/fallback attempts) still completes,
successfully or not, entirely before `build_package()`, the Gate A package
write, the email-text write, and the SMTP send — proven by
`MainSideEffectOrderingTests` in `tests/test_send_email.py`. A full
provider-exhaustion failure produces zero file writes and zero SMTP calls. A
success run produces exactly one package write, one email-text write, and one
SMTP send. Manual `workflow_dispatch` re-runs for the same date remain a
pre-existing, out-of-scope duplicate-send possibility that Phase B neither
introduces nor fixes.

---

## Files changed

- `scripts/llm_provider.py` — added `PROJECT_ACCESS_DENIED` category and its
  narrow detector; generalized `generate_ranking()`'s body into
  `_generate_with_fallback()`; added `generate_editorial()`; added
  `DEFAULT_OPENAI_EDITORIAL_MAX_COMPLETION_TOKENS`; `_call_openai_once()`
  gained an optional `max_completion_tokens` parameter (default-compatible).
- `scripts/send_email.py` — removed `EDITORIAL_MAX_ATTEMPTS`,
  `GEMINI_API_MAX_ATTEMPTS`, `GEMINI_RETRY_BASE_SECONDS`,
  `is_retryable_gemini_error()`, `call_gemini_with_retry()`,
  `clean_json_text()`, and the `google.genai` SDK import/client; added
  `build_editorial_schema()`, `validate_editorial_result()`,
  `combine_editorial_output()`; `generate_five_editorial_packages()` now
  delegates to `llm_provider.generate_editorial()`.
- `tests/test_llm_provider.py` — 6 new tests (PROJECT_ACCESS_DENIED
  classification + end-to-end regression on `generate_ranking()`).
- `tests/test_send_email.py` — new file, 22 tests.
- `.github/workflows/daily-duck.yml` — one line added (`OPENAI_API_KEY` env
  on the "Send Daily Duck email" step).
- `PHASE_B_RETRY_NORMALIZATION_AND_EDITORIAL_FALLBACK_PLAN.md` — this file.

Not modified (explicitly out of scope for Phase B): `scripts/generate_image_concepts.py`,
`scripts/regenerate_titles.py`, `scripts/gemini_retry.py`,
`scripts/gemini_retry_audit.py`.

---

## Remaining technical debt (Phase C candidates)

- `scripts/generate_image_concepts.py` has the identical 3×5 nested-retry
  defect (`EDITORIAL_MAX_ATTEMPTS`/`CONCEPT_MAX_ATTEMPTS` ×
  `GEMINI_API_MAX_ATTEMPTS`), no fallback provider.
- `scripts/regenerate_titles.py` has an unbounded single-layer 5× retry, no
  fallback provider.
- `scripts/gemini_retry_audit.py` currently fails (independently confirmed by
  running it) against all three of the above files plus (pre-Phase-B)
  `send_email.py`, because it checks for a different, unused retry helper
  (`scripts/gemini_retry.py`) than the one actually adopted
  (`scripts/llm_provider.py`). This pre-existing, already-broken audit should
  be reconciled with the `llm_provider.py` pattern or retired in a later
  phase; Phase B did not touch it.

Recommended next order: `generate_image_concepts.py` (same defect, next most
likely to fail the same way) → `regenerate_titles.py` (smaller blast radius)
→ `gemini_retry_audit.py`/`gemini_retry.py` cleanup.

---

## Implementation Summary

Implemented exactly the approved `GO_WITH_CHANGES` design with no scope
creep: only the six authorized files were touched, no live provider calls
were made, all 99 offline tests pass, the hard provider-call ceiling is now
enforced and tested at ≤4 (down from 15), and the human-approved shared
`PROJECT_ACCESS_DENIED` policy was verified to apply correctly to both
`news_ranking` and `editorial_generation` with regression tests proving an
ordinary 403 still fails closed on both. Not committed to git history at the
time this file was written to disk — see the calling task's final report for
commit status.
