# The Daily Duck — Phase A: Retry Normalization & News-Ranking Fallback (Implementation Plan)

Status: **PLAN / DESIGN ONLY — nothing in this document has been implemented.**
No production code, workflow YAML, cron, dependencies, tests, or audit
scripts were modified in producing this plan. No secret was inspected. No
live Gemini/OpenAI call was made. See compliance note at the end.

Planned: 2026-09-15
Builds on: `LLM_PROVIDER_AUDIT_AND_FALLBACK_PLAN.md` (untracked, not committed)
Verified against: current `HEAD` (`8029795`), working tree clean except the
untracked prior audit report — confirmed via `git status`/`git log` before
re-reading any code.

---

## STEP 1 — Verified retry structure at all four Gemini call sites

Re-read fresh from current `HEAD` (not assumed from the prior report). All
values below were re-confirmed by grepping the live constants
(`MAX_GEMINI_ATTEMPTS`, `EDITORIAL_MAX_ATTEMPTS`, `GEMINI_API_MAX_ATTEMPTS`,
`RETRYABLE_HTTP_CODES`, `retryable_markers`) directly in the four files. They
match the prior audit exactly — no drift.

| LOGICAL_OPERATION | CALL_SITE | INNER_RETRY | OUTER_RETRY | MAX_PHYSICAL_REQUESTS | ERRORS_RETRIED | CURRENT_BACKOFF | CURRENT_FAILURE_BEHAVIOR |
|---|---|---|---|---|---|---|---|
| News ranking | `scripts/rank_news_with_ai.py`, `call_gemini()`/`call_gemini_once()` | none (single flat loop) | `MAX_GEMINI_ATTEMPTS = 4` | **4** | HTTP `{429,500,502,503,504}` (set) + `URLError`/`TimeoutError`/`socket.timeout` | Flat schedule `RETRY_DELAYS = [10, 30, 60]` s | Uncaught exception propagates out of `main()` — **no** `try/except` wrapper in this file (unlike the other three), so failure is a raw Python traceback → non-zero exit. `validate_result()` (semantic/schema check) runs **outside** the retry loop entirely — a single bad Gemini response is never retried at all today. |
| Editorial copy | `scripts/send_email.py`, `generate_five_editorial_packages()` → `call_gemini_with_retry()` | `GEMINI_API_MAX_ATTEMPTS` (env, default **5**), exponential `10 × 2^(n-1)` s + jitter | `EDITORIAL_MAX_ATTEMPTS = 3` (hardcoded, not env) | **3 × 5 = 15** | Substring match on `429/500/502/503/504/resource_exhausted/internal/bad_gateway/unavailable/deadline_exceeded/high demand/temporarily unavailable/service unavailable/timeout/timed out` | Exponential, base env `GEMINI_RETRY_BASE_SECONDS` (default 10s) | `main()` wrapped in `try/except Exception: print("ERROR: …", file=sys.stderr); raise` → non-zero exit. Outer loop re-prompts Gemini on **malformed/invalid JSON output**, not just transport errors. |
| Design-options text (concepts + titles) | `scripts/generate_image_concepts.py`, `generate_options()` → `call_gemini_with_retry()` | `GEMINI_API_MAX_ATTEMPTS` (env, default **5**) | `EDITORIAL_MAX_ATTEMPTS` = env `CONCEPT_MAX_ATTEMPTS` (default **3**) | **3 × 5 = 15** | Same substring list as above | Same exponential pattern | Same `try/except` → stderr → re-raise → non-zero exit pattern. |
| Title regeneration (optional, human-triggered) | `scripts/regenerate_titles.py`, `generate_titles()` → `call_gemini()` | `GEMINI_API_MAX_ATTEMPTS` (env, default **5**) | **none** — a bad/invalid response is not re-prompted, it raises immediately | **5** | Same substring list as above | Same exponential pattern | Same `try/except` → stderr → re-raise pattern. |

**Confirmed unchanged from the prior audit.** Phase A's stated objective (Step
2 below) targets **news ranking only**; the other three call sites' retry
code is left untouched in this phase, per the task's explicit narrow scope
(Step 4) — they remain candidates for a later phase (Phase D in the original
audit's rollout plan).

---

## STEP 2 — Retry ownership model

**Root problem restated precisely:** today, retry exists at two independent
layers for 3 of the 4 call sites (an inner transport-retry loop *and* an
outer content-validation retry loop, each with its own attempt count), and a
fourth, structurally different implementation exists for ranking. No call
site currently distinguishes "quota exhausted" (429) from "temporarily
overloaded" (503) — `rank_news_with_ai.py`'s `RETRYABLE_HTTP_CODES` and the
other three files' substring lists both retry 429 exactly like 503, meaning
the code retries *into* a quota that cannot refill mid-run. This is the
amplification source (see MANDATORY FINAL DECISIONS §1).

### Design: one retry owner per logical operation

For news ranking specifically, Phase A introduces exactly one new function —
**the sole owner of every Gemini/OpenAI attempt** — and every existing
caller stops retrying:

- **Owner:** a new function (proposed name `get_ranking_result()` inside a
  new shared module `scripts/llm_provider.py`, see Step 13). It is the *only*
  place attempt counters, backoff, and provider selection exist.
- **Must NOT retry:** `rank_news_with_ai.py`'s `main()`, `build_prompt()`,
  `build_schema()`, `validate_result()`, `load_candidates()`,
  `load_archive()`, `remove_already_published_urls()`,
  `remove_same_day_duplicates()`. None of these currently retry anything
  (confirmed in Step 1), and the design must keep it that way — they call the
  owner exactly once per logical ranking operation and either get a
  validated result back or an exception.
- **Gemini attempts permitted:** capped **per error category**, not one flat
  number (see taxonomy below) — max 2 for transient/malformed-output
  categories, 1 (no retry) for quota/auth/invalid-request categories.
- **Retryable vs. immediately-stopping:** per Step 3's taxonomy.

This directly targets the task's own preferred direction, verified against
the actual code rather than adopted blindly:

- **503 / network / timeout:** Gemini gets one bounded retry (2 total
  attempts) → falls back to OpenAI if still failing. *Compatible* with
  existing behavior — `rank_news_with_ai.py` already treats these as
  retryable; this only shrinks the ceiling and adds a fallback where none
  exists today.
- **429 quota exhaustion:** **no** second Gemini attempt — immediate OpenAI
  fallback. *This changes existing behavior* — `rank_news_with_ai.py`
  currently retries 429 up to 4 times via the same flat loop as 503/network,
  which is the exact self-amplification the audit flagged. Verified: 429 is
  in `RETRYABLE_HTTP_CODES` today with no special-casing, so this is a real,
  intentional change, not a no-op.
- **401 / 403:** fail closed, alert, no automatic fallback. *Compatible* —
  neither is in `RETRYABLE_HTTP_CODES` or any of the substring lists today,
  so Gemini already fails these immediately; Phase A only needs to make sure
  the taxonomy classifies them explicitly rather than falling into a generic
  "not retryable → raise" bucket, and to make the "no fallback" decision
  explicit rather than implicit.
- **400:** fail closed, likely a bug in this repo's own request. *Compatible*
  — already not retried today.
- **Malformed response:** provider-independent validation
  (`validate_result()`, already provider-agnostic — confirmed in the prior
  audit and re-confirmed by inspecting it again in Step 5) with a small
  bounded recovery policy. **This is new** — `rank_news_with_ai.py` currently
  has *zero* retry on a validation failure (Step 1 finding); Phase A adds one
  bounded re-prompt attempt before falling back, rather than leaving it at
  "one bad JSON blob kills the whole run," which is arguably stricter/safer
  than what exists today, not a regression.

---

## STEP 3 — Provider-independent error taxonomy

| Category | GEMINI_RETRY_COUNT | OPENAI_FALLBACK_ALLOWED | HUMAN_ALERT | FAIL_CLOSED | LOG_LEVEL |
|---|---|---|---|---|---|
| `RATE_LIMIT_QUOTA` (429 / RESOURCE_EXHAUSTED) | **0** (no additional attempt beyond the first) | YES | only if OpenAI also fails | only if OpenAI also fails | WARNING on quota hit; ERROR only if both providers fail |
| `TEMPORARY_UNAVAILABLE` (503) | **1** (2 total attempts) | YES, after retry exhausted | only if OpenAI also fails | only if OpenAI also fails | WARNING → ERROR if both fail |
| `NETWORK_TIMEOUT` | **1** (2 total attempts) | YES | only if OpenAI also fails | only if OpenAI also fails | WARNING → ERROR if both fail |
| `AUTH_FAILURE` (401) | **0** | **NO** — silent fallback would mask an expired/misconfigured key indefinitely | YES, immediately | YES | ERROR |
| `PERMISSION_FAILURE` (403) | **0** | **NO** (same reasoning as auth) | YES, immediately | YES | ERROR |
| `INVALID_REQUEST` (400) | **0** | **NO** — a malformed request is a bug in this repo's prompt/schema code, not a provider problem; switching providers would hide the bug per the original audit's own instruction not to auto-fallback on bad requests | YES, immediately | YES | ERROR |
| `INVALID_RESPONSE` (schema/semantic validation failure, HTTP 200) | **1** (bounded re-prompt of the *same* provider) | YES, after exhausted — but OpenAI's result must **also** pass `validate_result()`; no further ping-pong retry against OpenAI within this category | only if both fail validation | only if both fail validation | WARNING → ERROR if both fail |
| `UNKNOWN_PROVIDER_ERROR` (unclassified) | **0** (conservative — never retry what the taxonomy doesn't recognize, to avoid exactly the kind of blind retry storm this phase exists to remove) | YES (worth trying a different provider once) | **YES, always** — an unclassified error is itself worth a human's attention regardless of whether fallback eventually succeeds | only if OpenAI also fails | ERROR (always, even on eventual success) |

This satisfies the task's "avoid retry storms" requirement structurally:
every category has an explicit, small, finite `GEMINI_RETRY_COUNT`, and no
category allows unbounded or repeated-fallback ping-pong.

---

## STEP 4 — News-ranking-only fallback control flow

Phase A production scope is intentionally limited to
`scripts/rank_news_with_ai.py`. No other call site is touched.

```
rank_news_with_ai.py :: main()
        |
        v
get_ranking_result(prompt, schema, validate_fn=validate_result)
   [ the ONE retry owner — scripts/llm_provider.py, new ]
        |
        v
   Gemini (gemini-3.6-flash) — existing prompt/schema, UNCHANGED
        |
        +-- success, passes validate_result() -------------> return, provider=gemini
        |
        +-- 503 / network / timeout, bounded retry (<=2) exhausted --+
        +-- 429 quota exhaustion (no repeat attempt) -----------------+
        +-- malformed output, 1 bounded re-prompt exhausted ----------+
        |                                                             |
        |                                                             v
        |                                             OpenAI (gpt-5.6-luna, candidate)
        |                                             SAME prompt text, SAME schema
        |                                                     |
        |                                                     +-- success, passes
        |                                                     |   validate_result()
        |                                                     |   -----------------> return, provider=openai
        |                                                     +-- failure or fails
        |                                                     |   validate_result()
        |                                                     |   -----------------> raise FAIL_CLOSED
        |
        +-- 401 / 403 / 400 --------------------------------------> raise FAIL_CLOSED
                                                                      (no fallback attempt)
```

`validate_result()` is called on **whichever** provider's parsed JSON comes
back, using the exact same function and the exact same rules — this is what
lets the rest of `rank_news_with_ai.py` (`main()`, the archive/duplicate
pre-filtering, the console report) remain completely unaware of which
provider answered.

---

## STEP 5 — Output contract compatibility (`rank_news_with_ai.py`)

Re-inspected `build_schema()`, `build_prompt()`, and `validate_result()`
directly from the current file.

- **Expected JSON structure** (`build_schema()`, enforced via Gemini's
  `responseJsonSchema` today): a single object with `recommended_id` (int),
  `recommended_reason` (str), and `top_five` — an array of **exactly 5**
  objects, each requiring `id, title, source, url, category, happiness, hope,
  general_interest, surprise, duck_visual, source_quality, freshness,
  broad_appeal, novelty_vs_archive, total_score, reason`. All fields are
  listed as `required` in the schema.
- **Required fields:** all 16 fields per story listed above; no optional
  fields exist in the schema.
- **Number of ranked items:** exactly 5 (`build_schema()`'s
  `minItems`/`maxItems` = 5; `validate_result()` re-checks `len(top_five) ==
  5` independently, i.e. this is enforced twice already).
- **ID/index mapping:** `build_prompt()` assigns a **synthetic sequential
  id** (1..N) to each pre-filtered candidate via `enumerate(candidates,
  start=1)` — this is *not* any original source-provided ID, it's generated
  fresh on every prompt build. `validate_result()` requires every returned
  `top_five[i].id` to be one of `1..len(candidates)`, unique across the 5,
  and requires `recommended_id` to be one of those 5. Because the OpenAI
  request would be built from the exact same `build_prompt()`/`build_schema()`
  call (same candidate numbering), this constraint is automatically satisfied
  by reusing the prompt-building code unchanged for both providers — no
  provider-specific ID-mapping logic is needed.
- **Scoring fields:** 9 integer 0–10 sub-scores + `total_score` (0–100,
  schema-typed as integer but not range-clamped in `validate_result()` — this
  is a pre-existing looseness, not something Phase A introduces or needs to
  fix).
- **Ordering requirement:** **none** — unlike `send_email.py`'s editorial
  function (which explicitly enforces input-order preservation),
  `rank_news_with_ai.py`'s `validate_result()` does not check the order of
  `top_five`; it only needs valid/unique ids and a `recommended_id` that is
  among them. This makes ranking *more* tolerant of provider-to-provider
  formatting differences than editorial copy would be.
- **Parsing assumption:** Gemini's REST response is read via
  `response_data["candidates"][0]["content"]["parts"][0]["text"]`, then
  `json.loads()`'d directly (ranking uses `responseMimeType=json`, so unlike
  the other 3 call sites it does **not** run markdown-fence stripping). An
  OpenAI adapter inside `llm_provider.py` must normalize to the same "raw
  JSON string, no markdown fences" shape before handing it to
  `json.loads()`+`validate_result()`, or must apply the same fence-stripping
  used elsewhere in the repo defensively.
- **Sad-story exclusion:** enforced **only via prompt instructions** ("AVOID
  NEGATIVE STORIES" section of `build_prompt()`) — there is **no
  deterministic code-level check** in `validate_result()` today. This is a
  pre-existing gap for Gemini, identically inherited (not worsened) by an
  OpenAI-sourced result, since the exact same prompt text and the exact same
  (absent) check apply either way. Flagged again here because Step 9's Test
  10 depends on it directly.
- **Duplicate/archive protections:** two independent, already
  provider-independent layers — (1) `remove_already_published_urls()` and
  `remove_same_day_duplicates()` run in plain Python **before** any LLM sees
  the candidate list (both providers would see the identical pre-filtered
  list); (2) `validate_result()` re-checks, after the LLM responds, that no
  selected URL is in the published-archive set and that no URL repeats
  within the 5 selections. Neither layer references Gemini by name anywhere
  in its logic.
- **Deterministic constraints:** the synthetic id numbering (above) is the
  only hard determinism requirement, and it is satisfied automatically by
  reusing `build_prompt()` unchanged.

**Conclusion:** `validate_result()` requires zero changes to accept an
OpenAI-sourced response, provided the adapter layer in `llm_provider.py`
normalizes OpenAI's raw text response to the same "JSON string, no fences"
shape Gemini's REST path already produces. This confirms the original
audit's "Provider A / Provider B → NORMALIZED_RANKING_RESULT" design is
achievable with **no validation-code changes**, only a new adapter.

---

## STEP 6 — Duplicate / idempotency analysis

Scoped specifically to Phase A's actual change surface: a fallback path
added *inside* `rank_news_with_ai.py`'s own function, called once per
`daily-duck.yml` run. Re-read `daily-duck.yml` in full to verify: it has
**no git commit/push step at all** (only checkout, Python setup, script
execution, and an `actions/upload-artifact@v4` step with 7-day retention) —
unlike `design-options.yml`, which does commit. Its only durable side effect
is the Gate A email sent by `send_email.py`. There is no automatic
GitHub-side retry of a failed job/step by default; a re-run only happens via
explicit human action (`workflow_dispatch` or "re-run failed jobs").

| Downstream side effect | Assessment | Reasoning |
|---|---|---|
| Duplicate approved-story state (`automation_state/approved_story.json`) | **SAFE** | Written only by a human's Gate A email reply processed by `approval-check-phase2.yml`, a separate workflow with no code path reachable from a ranking-stage change. Entirely outside Phase A's blast radius. |
| Duplicate Gate A email | **SAFE** (for Phase A's actual diff) | Exactly one email per successful `daily-duck.yml` run today and after Phase A — the fallback only changes what happens *inside* the ranking call before it returns a result to `send_email.py`, which is untouched. A **pre-existing, unchanged** exposure exists if a human manually re-runs `daily-duck.yml` for a day that already ran (no same-day guard exists in this workflow, unlike `design-options.yml`'s `should_run` check) — this is not introduced or worsened by Phase A; noted as a candidate for a future, separate hardening item, not a Phase A blocker. |
| Duplicate Design Options trigger | **SAFE** | `design-options.yml` is a separate, human-triggered workflow with its own existing `should_run`/`issue_date` idempotency gate (verified in the prior audit). Unreachable from a ranking-stage change. |
| Duplicate website publish | **SAFE** (out of blast radius) | `website-publish.yml` is several stages downstream with its own existing duplicate-date protections (per `429_UPDATE_README.txt`); not read in detail this session since it's untouched and unreachable from Phase A's change. |
| Duplicate X publish | **SAFE** (out of blast radius) | Same reasoning as website publish. |
| Duplicate archive entry (`data/archive.json`) | **UNKNOWN** (honest — not fully verified this session) | The append-on-publish logic was not directly inspected in this session; however it is unreachable from Phase A's change (ranking only reads `data/archive.json`, it does not write it), so this is out of Phase A's blast radius regardless of its own independent safety, which is a pre-existing property of the publish stage, not something this phase touches or needs to re-verify to proceed safely. |
| Duplicate git commit | **SAFE** | `daily-duck.yml` (where ranking runs) performs no git operations at all — confirmed by re-reading the full workflow file. A duplicate commit is structurally impossible from this stage. |

**Overall Phase A idempotency conclusion: SAFE.** The one PARTIALLY_SAFE item
identified (no same-day re-run guard on `daily-duck.yml` itself) is
pre-existing, applies identically with or without Phase A, and is out of
scope for this phase's stated objective — but is recorded here per the
task's "mandatory" analysis requirement rather than silently omitted.

---

## STEP 7 — OpenAI secret reuse

Verified from workflow/code structure only — no secret value inspected.

- `OPENAI_API_KEY` already appears as `${{ secrets.OPENAI_API_KEY }}` in
  `.github/workflows/design-options.yml` (line 116) and
  `.github/workflows/design-selection-check.yml` (line 103), both already in
  production use for `gpt-image-2` image generation.
- `daily-duck.yml` (where ranking runs) currently references only
  `GEMINI_API_KEY` in its "Rank Daily Duck news" and "Send Daily Duck email"
  steps — it does **not** currently expose `OPENAI_API_KEY` anywhere.

**Phase A can reuse the existing secret, pending one verification** (not an
assumption): whether the existing `OPENAI_API_KEY` is scoped broadly enough
to also authorize chat/text-generation calls, or narrowly to images only.
This cannot be determined by reading workflow YAML or code — it requires an
authenticated capability check, which is explicitly out of scope for a
planning-only task. Recorded as a required verification step for Step 15's
controlled validation, not assumed either way.

**Least-privilege placement:** if reused, `OPENAI_API_KEY` should be added
to the `env:` block of the **"Rank Daily Duck news" step only**, matching
the existing per-step `env:` pattern already used throughout `daily-duck.yml`
(e.g. `GEMINI_API_KEY` is scoped per-step there, not job-wide). It must
**not** be added to the "Send Daily Duck email" step, the artifact-upload
step, or the failure-notification step — none of those need it for Phase A's
scope, and job-wide exposure would be broader than necessary.

---

## STEP 8 — Dependency analysis

`requirements-phase2.txt` (read directly, current `HEAD`):

```
google-genai>=1.0.0
openai>=1.0.0
Pillow>=10.0.0
requests>=2.31.0
requests-oauthlib>=1.3.1
```

**The official OpenAI Python SDK is already a repository dependency**
(`openai>=1.0.0`), already imported and used identically (`from openai
import OpenAI`, `client.images.generate(...)`) in
`generate_image_concepts.py` and `generate_design_previews.py`.

**Recommendation: (B) use existing infrastructure.** No new dependency is
required or recommended. `scripts/llm_provider.py` should use the same
already-installed `openai` SDK, calling its chat/completions-equivalent
method the same way the existing two files already call
`client.images.generate(...)`, for consistency and to avoid introducing a
second HTTP client pattern. `requirements-phase2.txt` itself is not modified
by this phase (per Strict Prohibitions and because nothing new is needed).

---

## STEP 9 — Test plan (12 tests, all mocked, no live provider calls)

All tests target the new `scripts/llm_provider.py` module (and a thin
integration layer in `rank_news_with_ai.py`) with the Gemini and OpenAI
transport calls fully replaced by mock/fixture functions. No test in this
plan makes a real network call.

| # | Scenario | MOCK / FIXTURE strategy | EXPECTED_CALL_COUNT | EXPECTED_RESULT | SIDE_EFFECT_ALLOWED |
|---|---|---|---|---|---|
| 1 | Gemini succeeds first attempt | `gemini_call` mock returns valid schema-conformant JSON on call 1; `openai_call` mock present but must not be invoked | gemini=1, openai=0 | Normalized result, provider=gemini, passes `validate_result()` | none |
| 2 | Gemini 503 once then succeeds | `gemini_call` mock raises simulated 503 on call 1, returns valid JSON on call 2 | gemini=2, openai=0 | Normalized result, provider=gemini | none |
| 3 | Gemini repeatedly 503 | `gemini_call` mock raises 503 on every call (ceiling=2 reached); `openai_call` mock succeeds on its 1st call | gemini=2, openai=1 | Normalized result, provider=openai; fallback logged exactly once (`reason=TEMPORARY_UNAVAILABLE`) | none |
| 4 | Gemini 429 | `gemini_call` mock raises simulated 429 on call 1 only; `openai_call` mock succeeds | **gemini=1** (asserts strictly no 2nd attempt — this is the key regression test against reintroducing quota-burning retry), openai=1 | Normalized result, provider=openai | none |
| 5 | Gemini 401 | `gemini_call` mock raises simulated 401 on call 1 | gemini=1, **openai=0** (asserts fallback was never attempted, per "no automatic fallback unless justified") | Raises classified `FailClosed`/auth exception; propagates to non-zero exit | `HUMAN_ALERT` log/flag emitted (asserted); no OpenAI call |
| 6 | Gemini 400 | `gemini_call` mock raises simulated 400 on call 1 | gemini=1, openai=0 | Raises `FailClosed`; no fallback attempted | none beyond error logging |
| 7 | OpenAI fallback succeeds | `gemini_call` mock exhausts its 503 ceiling; `openai_call` mock returns well-formed JSON (5 valid stories, valid `recommended_id`) | gemini=2, openai=1 | Result passes the **exact same** `validate_result()` used for Gemini; asserts no provider-specific branching exists in the caller | none |
| 8 | OpenAI returns malformed output | `gemini_call` mock exhausts ceiling; `openai_call` mock returns JSON missing required fields / wrong story count | gemini=2, openai=1 (single bounded attempt for this category, no further ping-pong) | `validate_result()` raises → overall `FailClosed`; asserts the file-write/email-send functions were **never called** in this branch | none — test explicitly proves no downstream publish path is reached |
| 9 | Gemini fails + OpenAI fails | `gemini_call` mock exhausts its ceiling with a fallback-eligible error; `openai_call` mock also exhausts its own ceiling (2) | gemini=2, openai=2 | Final exception propagates uncaught, matching `rank_news_with_ai.py`'s existing "uncaught → non-zero exit" contract, so `daily-duck.yml`'s existing `if: failure()` → `send_workflow_failure_email.py` step remains reachable | none from this module; failure-notification script itself is untouched and out of scope for this test |
| 10 | Fallback result contains a prohibited/sad story | `openai_call` mock returns structurally **valid** JSON (passes id/shape/field checks) but with sad/negative content | gemini=2, openai=1 | **Open decision, see below** — with today's `validate_result()` (no deterministic content guard, Gemini or OpenAI), this passes validation as-is; documented as a known pre-existing gap rather than silently passed or silently "fixed" without the human deciding | none |
| 11 | Fallback returns a duplicate/already-published story | `openai_call` mock returns a URL present in the archive/published-URL set | gemini=2, openai=1 | Existing `validate_result()` already-published-URL check (provider-independent, unchanged) raises → `FailClosed`. Proves the reused-validator design works without modification. | none |
| 12 | No nested retry amplification (hard upper bound) | `gemini_call` and `openai_call` mocks both configured to **always** fail, arbitrarily many times if allowed | Combined `gemini_call.call_count + openai_call.call_count` asserted **strictly ≤ HARD_MAX_PROVIDER_CALLS (4)**, regardless of how many failures the mocks are willing to produce | Raises `FailClosed` after exactly the bounded total, never more | none — **highest-priority test in this plan**, directly regression-guards the amplification this whole phase exists to remove |

**Open decision for Test 10:** Phase A's stated scope (Step 4) is narrowly
"add OpenAI fallback for ranking," not "add a new content-safety check." But
Test 10, taken literally, demands a guard that doesn't exist today for
*either* provider. Two honest options, presented for a human decision rather
than picked silently:

- **(a) Defer:** mark Test 10 as a documented `xfail`/skip pointing at the
  pre-existing gap (tracked as the original audit's Phase C item), and note
  in the PR/commit description that this is a known, pre-existing limitation
  inherited unchanged, not a Phase A regression.
- **(b) Pull forward:** add a small, deterministic keyword/category
  blocklist check into `validate_result()` (or a thin wrapper around it) as
  part of Phase A, specifically because it's cheap, provider-independent by
  construction, and turns Test 10 into a real passing safety test rather
  than a documented gap.

This plan does not choose for the user; it is listed explicitly in
`MANDATORY FINAL DECISIONS` below as something to resolve before
implementation.

---

## STEP 10 — Hard request budget

```
NEWS_RANKING:

  Gemini maximum physical attempts   = 2   (transient/malformed-output ceiling)
                                        1   (quota/auth/invalid-request categories — no retry)
  OpenAI  maximum physical attempts  = 2   (mirrors Gemini's bounded-retry shape for
                                             transient OpenAI-side errors; 1 attempt only
                                             for the malformed-output category, matching
                                             the "no further ping-pong" rule in Step 3)

  HARD_MAX_PROVIDER_CALLS for one logical ranking operation <= 4
```

This is **the same total ceiling `rank_news_with_ai.py` already has today**
(`MAX_GEMINI_ATTEMPTS = 4`) — Phase A does not raise the worst-case physical
call count for ranking at all. It *reallocates* those 4 possible attempts
across two providers instead of exhausting all 4 against a single provider
that may already be quota-dead, and — unlike today — the worst case now
usually *resolves successfully* via fallback instead of guaranteeing failure
after 4 futile Gemini attempts. Test 12 (Step 9) makes this bound directly
testable and enforced in code, not just documented.

---

## STEP 11 — Observability plan

```
LLM_OPERATION=news_ranking LLM_PROVIDER=gemini LLM_MODEL=gemini-3.6-flash LLM_ATTEMPT=1 LLM_RESULT=TEMPORARY_UNAVAILABLE
LLM_OPERATION=news_ranking LLM_PROVIDER=gemini LLM_MODEL=gemini-3.6-flash LLM_ATTEMPT=2 LLM_RESULT=TEMPORARY_UNAVAILABLE
LLM_FALLBACK_TRIGGERED=true LLM_FALLBACK_REASON=TEMPORARY_UNAVAILABLE
LLM_OPERATION=news_ranking LLM_PROVIDER=openai LLM_MODEL=gpt-5.6-luna LLM_ATTEMPT=1 LLM_RESULT=SUCCESS
VALIDATION_RESULT=pass PROVIDER_USED=openai
```

Proposed counters (in-process, printed/aggregated at end of run — no new
persistent state store required for Phase A):

```
logical_operations        (always 1 for a single rank_news_with_ai.py run)
gemini_physical_requests
openai_physical_requests
fallback_count             (0 or 1 per run)
provider_failure_count     (0, 1, or 2 — how many providers ultimately failed)
```

**Never logged:** `GEMINI_API_KEY`, `OPENAI_API_KEY`, full request/response
bodies (which could incidentally contain key-adjacent header echoes on some
error paths — the wrapper should log only the classified error category and
a truncated message, not raw exception text verbatim, mirroring existing
scripts' `print(f"...: {exc}")` pattern but reviewed for this specifically
during implementation, not assumed safe by default).

---

## STEP 12 — Stale audit technical debt

Both scripts were re-verified this session by re-reading their current
source (not re-executed again, since neither the scripts nor `HEAD` changed
since the prior audit's read-only run confirmed `EXIT_CODE=1` for both).

- **`scripts/gemini_retry_audit.py`** — fails because it structurally
  requires every script containing the literal substring
  `"client.models.generate_content("` to also contain
  `"call_with_retry(lambda: client.models.generate_content("` and an
  `"from gemini_retry import call_with_retry"` import. None of the three
  `google.genai`-SDK-based scripts match this pattern — each has its own
  bespoke retry function instead (Step 1). **Also note:**
  `rank_news_with_ai.py` doesn't even use `client.models.generate_content(`
  at all (it's raw `urllib` REST) — so this audit was never covering the
  ranking call site's retry shape in the first place, an independent scope
  gap that predates Phase A. **Classification: REPLACE_REQUIRED** — once
  `llm_provider.py` exists, this script's *detection pattern itself* needs
  to change (check for `llm_provider` usage, not the old
  `gemini_retry`/`call_with_retry` pattern), not just have its inputs
  updated — `scripts/gemini_retry.py`, the thing it currently checks for, is
  being superseded, not merely more consistently adopted.
- **`scripts/model_audit.py`** — fails because it asserts the literal string
  `gemini-3.1-flash-lite-image` must appear across `model_config.py`,
  `generate_image_concepts.py`, `generate_design_previews.py`, and two
  workflow files, but image generation moved to `gpt-image-2` (OpenAI) at
  some point after this script was written. **Unrelated to Phase A's own
  scope** (it checks the *image* model, not the *text/ranking* model — its
  `EXPECTED_TEXT = "gemini-3.6-flash"` assertion is still correct and
  unaffected by this phase, since Gemini stays primary for ranking and the
  model literal itself doesn't change). **Classification: UPDATE_REQUIRED**
  — a straightforward constant/assertion fix (converging it with what
  `scripts/recovery_audit.py` already correctly checks), not a
  detection-logic redesign.

Both are `workflow_dispatch`-only (confirmed by re-reading their workflow
files) — **zero production/scheduled blast radius** either way; they mislead
a human who runs them manually, not the automated pipeline. Per the task's
explicit instruction that "the new reliability implementation must not leave
knowingly false audits presented as trustworthy production checks," both
fixes are included in Phase A's **file plan** below as small, isolated,
low-risk changes — but are not executed in this planning session (Strict
Prohibitions).

---

## STEP 13 — Implementation file plan

| PATH | MODIFY / CREATE | PURPOSE | RISK | TEST COVERAGE |
|---|---|---|---|---|
| `scripts/llm_provider.py` | **CREATE** | Sole retry owner + error taxonomy (Step 3) + Gemini→OpenAI dispatch (Step 4/10) for the ranking operation; generic-shaped interface so later phases (editorial, design-options) can adopt it without a rewrite, but **only wired into ranking in Phase A** | MEDIUM — new logic, but a pure function with no side effects of its own (no file writes, no email sends); all behavior is unit-testable in isolation | Step 9's 12 tests target this module directly |
| `scripts/rank_news_with_ai.py` | **MODIFY** | Replace `call_gemini()`, `call_gemini_once()`, `MAX_GEMINI_ATTEMPTS`, `RETRY_DELAYS`, `RETRYABLE_HTTP_CODES` with a single call into `llm_provider.get_ranking_result(...)`; `build_prompt()`, `build_schema()`, `validate_result()`, `load_candidates()`, `load_archive()`, the pre-filter functions, and `main()`'s console reporting stay **unchanged** | MEDIUM-HIGH — the one production call site touched, reachable from the daily scheduled cron; highest-scrutiny file in this plan | New regression tests (below) plus Step 9's integration-level assertions (Tests 9, 12 in particular) |
| `tests/test_llm_provider.py` | **CREATE** | The 12 tests from Step 9, fully mocked, no live calls | LOW (test-only) | Is the coverage |
| `tests/test_rank_news_with_ai.py` | **CREATE** | Regression tests proving `build_prompt()`/`build_schema()`/`validate_result()` behavior is byte-for-byte unchanged by the refactor, and that `main()` correctly delegates to `llm_provider.py` instead of the removed local functions — **currently zero unit tests exist for this file** (`tests/` only contains `test_automation_chain_audit.py`), so this closes a pre-existing coverage gap the refactor would otherwise be exposed to | LOW-MEDIUM | Self-covering |
| `scripts/gemini_retry_audit.py` | **MODIFY** (future phase of Phase A implementation, not this planning session) | Update detection logic to check for `llm_provider` usage instead of the superseded `gemini_retry`/`call_with_retry` pattern (Step 12) | LOW — manual-dispatch-only tool, no production blast radius | New/updated assertions in the same file's own smoke-test pattern |
| `scripts/model_audit.py` | **MODIFY** (same, future phase) | Fix stale `EXPECTED_IMAGE` assertion to match current `gpt-image-2` reality (Step 12) — independent of ranking, bundled here as low-risk cleanup | LOW — manual-dispatch-only tool | Re-run manually post-fix (`EXIT_CODE=0` expected) |
| `.github/workflows/daily-duck.yml` | **MODIFY** — *not performed until Step 15's Human Gate, and never in this planning session* | Add `OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}` to the **"Rank Daily Duck news" step's `env:` block only** (Step 7's least-privilege placement) | Listed separately because it is the only item on this list that touches live production wiring; everything above it (files 1–6) can be fully built and unit-tested with zero workflow changes | Verified via Step 15's staged, human-gated validation — not a unit test |

Nothing else — `send_email.py`, `generate_image_concepts.py`,
`regenerate_titles.py`, `requirements-phase2.txt`, and every other workflow
YAML file are **not** touched in Phase A, per the task's narrow-scope
instruction and to keep this the smallest diff that satisfies Objectives A–H.

---

## STEP 14 — Rollback plan

- **No persistent schema or state migration is added.** `ai_ranked_news.json`
  keeps the exact same structure (`recommended_id`, `recommended_reason`,
  `top_five[5]`) regardless of which provider produced it — `validate_result()`
  is what defines that shape, and it is unchanged. Nothing downstream needs
  to know or care which provider ran.
- **Rollback strategy:** two independent options, not mutually exclusive:
  1. Revert the commit(s) introducing `llm_provider.py` and the
     `rank_news_with_ai.py` changes; since ranking output feeds only an
     emailed, artifact-only Gate A package (never committed to git — Step 6),
     a revert has no cascading effect on any already-approved or
     already-published state.
  2. **Recommended in addition:** build a feature flag into
     `llm_provider.py` from day one (e.g. an env var read at call time) that
     disables the OpenAI branch entirely and reproduces today's
     Gemini-only, bounded-retry behavior. This makes rollback a
     zero-deploy, workflow-`env:`-only change rather than requiring a git
     revert and redeploy — safer for a production automation chain, and
     directly answers "can this be turned off without a code change."
- **Does rollback affect current publication state?** No. Phase A's change
  is upstream of every commit/publish step (Step 6); disabling or reverting
  it only affects the *next* ranking run's provider behavior, never any
  already-approved story, already-sent email, or already-published issue.

---

## STEP 15 — Production validation plan (staged, not executed now)

1. Static/unit tests (Step 9's 12 tests + the new `rank_news_with_ai.py`
   regression tests) — fully offline, no live calls.
2. Offline mocked fallback tests — same suite, explicitly re-run with the
   feature flag toggled both ways (fallback enabled/disabled) to prove the
   rollback path (Step 14) actually reproduces today's behavior bit-for-bit
   when disabled.
3. Independent review of `llm_provider.py` and the `rank_news_with_ai.py`
   diff — a second reader should specifically re-verify Step 3's taxonomy
   table against the diff, since that table is the part most likely to
   silently drift during implementation.
4. **Human Gate** — explicit approval before any live credential is ever
   exercised, per this project's existing human-approval culture (Gate A/B
   emails) and this task's own instruction.
5. One controlled workflow execution — **must not run against the live
   `daily-duck.yml` schedule while the currently-active 24-hour Gate A cron
   reliability A/B test is in progress** (see below). Recommended default:
   schedule this step to occur **after** that experiment's 24-hour window
   closes, rather than attempting date-isolation tricks against
   `DAILY_DUCK_TARGET_DATE` during the experiment — simplest and safest.
   Alternative, if timing can't wait: use an explicit
   `DAILY_DUCK_TARGET_DATE` far outside the current issue-date range so the
   controlled run cannot be confused with, or interleave state with, the
   real cron-driven cycle the experiment is measuring.
6. Inspect provider logs (Step 11's fields) — confirm attempt counts never
   exceed the Step 10 hard budget in the real run, not just in mocks.
7. Verify no duplicate state was created (re-check the Step 6 table's items
   against the actual run's artifacts/emails, not just in theory).
8. Verify the Gate A package (`gate_a_package.json`,
   `daily_duck_email.txt`) is structurally identical in shape whether the
   controlled run happened to use Gemini or (if deliberately forced)
   OpenAI.
9. Stop before any downstream publication step if anything in 6–8 looks
   off — this phase's blast radius should never reach
   `website-publish.yml`/`x-publish.yml` regardless, but this is an explicit
   stop-gate, not an assumption.
10. Human approval for broader rollout (Phase B onward, per the original
    audit's staged plan) — this document does not request or imply
    authorization beyond Phase A.

---

## IMPORTANT — current cron A/B test (verified, not touched)

Re-read the relevant workflow files directly to confirm the task's warning
against current `HEAD`, rather than assuming it:

```
.github/workflows/approval-check-phase2.yml : cron "11,26,41,56 * * * *"   <- Gate A test cron
.github/workflows/design-selection-check.yml: cron "9,24,39,54 * * * *"    <- Design Selection, unchanged control group
```

The most recent commit on `main` (`8029795`, "test: offset Gate A schedule
for cron reliability") confirms an active scheduling experiment on
`approval-check-phase2.yml`. **Neither file, nor
`automation-chain-audit.yml`'s thresholds, nor any cron expression, was
read for modification purposes or touched in any way in this session** — they
were only re-read to confirm the experiment is real and to scope Step 15's
timing recommendation around it. No Gate A workflow was manually triggered.

---

## Report version-control decision

**Recommendation: (A) commit `LLM_PROVIDER_AUDIT_AND_FALLBACK_PLAN.md` as
project evidence — but not in this session, and not without being asked.**

Reasoning: it documents findings (retry-amplification math, the stale-audit
discovery, the already-existing OpenAI image-generation precedent) that took
real repository investigation to establish and that this Phase A plan
directly depends on and cites throughout. Losing it (e.g. an accidental
`git clean`, a fresh clone, a different machine) would mean re-deriving all
of Phase 1–2's request-budget math from scratch before any future phase
could safely proceed. It is evidence, not scratch output — the same
reasoning that applies to `MODEL_AUDIT.md` and `429_UPDATE_README.txt`,
which are already committed project history in this repository. **(B) kept
local only** was considered and rejected: the whole point of an audit
artifact is that a future session (or a different person) can pick up Phase
B–F without re-auditing. **(C) incorporate into another canonical document**
was considered and rejected for now: nothing in the repo is currently
positioned as that canonical document, and merging two large, differently-
purposed reports (a point-in-time audit vs. a staged implementation plan)
would make both harder to read; they can be cross-referenced (as this
document already does) without merging. **This plan does not commit either
document** — that remains the user's explicit action per Strict Prohibitions.

---

## MANDATORY FINAL DECISIONS

**1. What caused the request amplification?**
Two independent design gaps compounding: (a) three of four call sites nest
an outer content-validation retry loop *around* an inner transport-retry
loop, multiplying rather than sharing an attempt budget (3×5=15 twice in the
codebase); (b) no call site distinguishes 429 (quota exhaustion, where
retrying is nearly always futile within the same run) from 503/network
(genuinely transient, where retrying helps) — both are retried identically,
so the very mechanism meant to survive a transient Gemini hiccup is what
burns through a 20-request/day-class free tier when the actual problem is
quota, not transience.

**2. What exact retry ownership model should replace it?**
One function per logical operation owns every attempt and every provider
decision (Step 2); every existing caller (`main()`, validators, prompt
builders) stops retrying entirely and calls that owner exactly once.

**3. Maximum Gemini attempts for news ranking?**
2 for transient/malformed-output error categories; 1 (no retry) for
quota/auth/invalid-request categories — see Step 3's full taxonomy.

**4. Maximum OpenAI fallback attempts?**
2, mirroring Gemini's bounded-retry shape for transient errors; 1 for the
malformed-output category (no further ping-pong between providers).

**5. Hard maximum physical API calls per ranking operation?**
**4 total** (Gemini + OpenAI combined) — identical to today's existing
`MAX_GEMINI_ATTEMPTS = 4` ceiling, just reallocated across two providers
instead of exhausted against one. Made directly testable via Step 9 Test 12.

**6. Which errors trigger immediate fallback?**
429 (quota — immediately, no Gemini retry), 503/network/timeout (after one
bounded Gemini retry), and malformed/invalid output (after one bounded
same-provider re-prompt).

**7. Which errors fail closed?**
401, 403, 400 — always, with no automatic OpenAI fallback, per the task's
own preferred direction, verified compatible with existing code (none of
these are retried today either).

**8. Can existing OPENAI_API_KEY be reused?**
Very likely yes for authentication (it already authorizes OpenAI image
generation in production) — but its exact scope for text/chat-style calls
is unverified by this repo-only audit and must be confirmed in Step 15
before relying on it, not assumed.

**9. Is a new dependency required?**
No. `openai>=1.0.0` is already in `requirements-phase2.txt` and already used
in production by two other scripts.

**10. What exact files should Phase A modify?**
See Step 13's table: create `scripts/llm_provider.py`,
`tests/test_llm_provider.py`, `tests/test_rank_news_with_ai.py`; modify
`scripts/rank_news_with_ai.py`, `scripts/gemini_retry_audit.py`,
`scripts/model_audit.py`; and — only at the Step 15 Human Gate, not as part
of the code-only implementation — add one `env:` line to
`.github/workflows/daily-duck.yml`.

**11. Are downstream publication operations currently idempotent enough?**
For Phase A's actual scope: **yes** — every downstream side effect is either
demonstrably unreachable from a ranking-stage change (design-options,
website publish, X publish, git commits — Step 6) or has its own existing,
independent idempotency guard already in place. One pre-existing gap was
found (no same-day re-run guard on `daily-duck.yml` itself) but it is
unrelated to and unworsened by this phase.

**12. What must be fixed before controlled production validation?**
(a) Confirm `OPENAI_API_KEY`'s actual scope covers text generation (Step 7);
(b) resolve the Test 10 open decision — defer vs. pull forward a minimal
content guard (Step 9); (c) complete and pass all 12 unit tests plus the
`rank_news_with_ai.py` regression tests; (d) independent review of the
taxonomy-to-code mapping; (e) confirm the controlled run's timing doesn't
overlap the active Gate A cron A/B test window (see above).

**13. What should happen to the two stale audit scripts?**
`gemini_retry_audit.py` → **REPLACE_REQUIRED** (its detection pattern itself
is obsolete once `llm_provider.py` supersedes `gemini_retry.py`).
`model_audit.py` → **UPDATE_REQUIRED** (a straightforward stale-constant fix,
unrelated to ranking). Both included in Phase A's file plan as small,
isolated, zero-production-blast-radius changes — not executed in this
planning session.

**14. Should the audit report be committed?**
Recommended: **yes** (option A), as project evidence — but as a separate,
explicit, user-approved action, not bundled into this planning task and not
performed here.

**15. Estimated implementation complexity:**
**MEDIUM.** The core retry-ownership/taxonomy logic is conceptually simple
and fully unit-testable offline (leaning LOW), but the fact that it touches
the one call site in the automated daily cron path, requires careful
verification of an existing secret's scope, and needs to be validated
without contaminating a currently-running production reliability experiment
pushes it to MEDIUM rather than LOW.

**16. Estimated implementation + tests time:**
Roughly **1–2 focused working days**: `llm_provider.py` + taxonomy +
`rank_news_with_ai.py` refactor + the 12 Step 9 tests + regression tests for
the previously-untested prompt/schema/validation code (~0.5–1 day); the two
stale-audit-script fixes (~1–2 hours); independent review + Step 15 staged
validation coordination, excluding any wait time imposed by the active cron
experiment (~0.5 day). This is a planning-time estimate, not a commitment.

---

## Compliance note (Strict Prohibitions)

This session did not: modify production code, modify workflow YAML, modify
cron, modify `requirements-phase2.txt`, modify tests, modify
`gemini_retry_audit.py`/`model_audit.py`, add provider-abstraction code, add
an OpenAI fallback, change Gemini behavior, make any live Gemini/OpenAI API
call, run any production workflow (including the Gate A cron under
experiment), commit, push, alter any GitHub Secret, or inspect any secret's
value. `git status`/`git log` were read for verification only. Workflow YAML
files (`approval-check-phase2.yml`, `design-selection-check.yml`,
`automation-chain-audit.yml`, `daily-duck.yml`) were read, never written.

---

# FINAL REPORT

**STATUS:** PASS_WITH_FINDINGS

**ROOT_CAUSE:** Nested outer(3)×inner(5) retry loops at two call sites
(15 physical requests each) plus 429 being retried identically to 503 at
every call site — the retry logic itself is the dominant, largely
self-inflicted cause of the observed free-tier quota exhaustion.

**RETRY_OWNERSHIP:** One new function (`llm_provider.get_ranking_result()`)
becomes the sole retry owner for news ranking; every existing caller stops
retrying and calls it exactly once. Other 3 call sites unchanged in Phase A.

**ERROR_POLICY:** 8-category taxonomy (Step 3) — 429 gets zero additional
Gemini attempts and goes straight to OpenAI fallback; 503/network/timeout
get one bounded Gemini retry then fallback; 401/403/400 fail closed with no
automatic fallback; malformed output gets one bounded same-provider
re-prompt then fallback, gated by the existing provider-independent
validator either way.

**NEWS_RANKING_GEMINI_MAX_ATTEMPTS:** 2 (transient/malformed-output
categories); 1 (no retry) for quota/auth/invalid-request categories.

**NEWS_RANKING_OPENAI_MAX_ATTEMPTS:** 2 (1 for the malformed-output
category, no further ping-pong).

**HARD_MAX_PROVIDER_CALLS:** 4 total per logical ranking operation — same
ceiling as today, reallocated across two providers instead of exhausted
against one.

**OPENAI_FALLBACK_MODEL:** `gpt-5.6-luna` (candidate only, per prior audit —
not newly confirmed or changed by this plan).

**OPENAI_SECRET:** REUSE_EXISTING (very likely) — `OPENAI_API_KEY` already
live and already used for image generation; exact scope for text/chat calls
unverified, flagged as a required Step 15 check, not assumed.

**DEPENDENCY_CHANGE:** NO — `openai>=1.0.0` already in
`requirements-phase2.txt`, already in production use.

**OUTPUT_CONTRACT:** `validate_result()` is already fully provider-agnostic
(operates only on the parsed dict: exact story count, unique valid IDs,
`recommended_id` membership, no already-published/duplicate URLs); requires
zero changes to accept an OpenAI-sourced response, provided the adapter
normalizes OpenAI's text response to the same fence-free JSON-string shape
Gemini's REST path already produces.

**IDEMPOTENCY:** SAFE for Phase A's actual change surface (Step 6); one
pre-existing, Phase-A-independent gap noted (no same-day re-run guard on
`daily-duck.yml` itself) but not introduced or worsened by this phase.

**STALE_AUDITS:** `gemini_retry_audit.py` = REPLACE_REQUIRED (obsolete
detection pattern); `model_audit.py` = UPDATE_REQUIRED (stale constant,
unrelated to ranking). Both zero-blast-radius (manual-dispatch-only), both
included in Phase A's file plan, neither touched in this session.

**FILES_PLANNED:** create `scripts/llm_provider.py`,
`tests/test_llm_provider.py`, `tests/test_rank_news_with_ai.py`; modify
`scripts/rank_news_with_ai.py`, `scripts/gemini_retry_audit.py`,
`scripts/model_audit.py`; later, human-gated: one `env:` line in
`.github/workflows/daily-duck.yml`.

**TESTS_PLANNED:** 12 (Step 9) — covering happy path, bounded transient
retry, fallback-on-exhaustion, no-retry-on-429 (regression guard), fail-
closed on 401/400, contract equivalence across providers, malformed-output
handling, dual-provider failure, a pending content-safety open decision, and
a hard-ceiling regression test (highest priority of the twelve). Plus new
regression tests for previously-untested `rank_news_with_ai.py` logic.

**ROLLBACK:** No schema/state migration; git revert is safe (ranking output
is artifact/email-only, never committed); an env-var feature flag is
recommended in addition, for a zero-deploy disable path. No effect on any
already-published or already-approved state either way.

**AUDIT_REPORT_VERSION_CONTROL:** COMMIT (recommended, as project evidence)
— not performed in this session, pending explicit user action.

**IMPLEMENTATION_COMPLEXITY:** MEDIUM.

**ESTIMATED_IMPLEMENTATION_TIME:** ~1–2 focused working days, excluding any
wait imposed by the active Gate A cron reliability experiment.

**RISKS:** (1) Test 10's content-safety gap is pre-existing but now
explicitly surfaced — needs a human decision (defer vs. pull forward), not
a silent default. (2) `OPENAI_API_KEY`'s scope for text calls is unverified.
(3) `gpt-5.6-luna`'s real capabilities remain unverified by this
repo-only process (carried over from the prior audit). (4) Any controlled
validation run must be timed to avoid the active Gate A cron A/B test
window. (5) `daily-duck.yml`'s pre-existing lack of a same-day re-run guard
remains unresolved (not blocking, but not fixed either).

**RECOMMENDATION:** GO_WITH_CHANGES — proceed to implementation once the
Step 9/Test 10 content-safety decision and the Step 7 secret-scope
verification are resolved; do not skip either in the name of speed, since
both were surfaced specifically by trying to make the test plan concrete
rather than staying abstract.

**FILES_MODIFIED:** NONE (production code, workflows, cron, dependencies,
tests, and audit scripts all untouched)
**Planning report created:** `PHASE_A_RETRY_NORMALIZATION_AND_RANKING_FALLBACK_PLAN.md`

**COMMIT:** NOT_PERFORMED

**PUSH:** NOT_PERFORMED

**NEXT:** WAITING_FOR_HUMAN_APPROVAL
