# The Daily Duck — LLM Provider Audit & Fallback Plan

Status: **AUDIT / DESIGN ONLY — nothing in this document has been implemented.**
Scope: read-only repository analysis. No production code, workflow YAML, models,
dependencies, or secrets were changed. See `STRICT PROHIBITIONS` compliance note
at the bottom.

Audited: 2026-09-15
Repository: `the-daily-duck` (local working copy, branch `main`, clean at audit start)

---

## PHASE 1 — Current Gemini usage audit

A full-repository search was run for: `Gemini`, `gemini`, `google.genai`,
`google.generativeai`, `generate_content`, `genai`, `GEMINI_API_KEY`, model
name literals, client-initialization code, and retry wrappers. 19 files
reference Gemini in some form; only **4 files actually call the Gemini API**.
Every other hit is documentation, model-name constants, or an audit script
that inspects those 4 files.

### Call site table

| # | File / function | Reached by (workflow → step) | Purpose | Model | Transport | Calls per normal cycle | Retry shape (outer × inner) | Worst-case physical requests |
|---|---|---|---|---|---|---|---|---|
| 1 | `scripts/rank_news_with_ai.py` → `call_gemini()` / `call_gemini_once()` | `daily-duck.yml` → "Rank Daily Duck news" (scheduled, 07:00 JST) | Score & select top-5 news stories from candidates; pick 1 recommended story; dedupe against `data/archive.json` | `gemini-3.6-flash` | raw `urllib` REST call to `generateContent`, `responseJsonSchema` enforced | 1 | 1 × 4 (flat delays: 10s / 30s / 60s) | **4** |
| 2 | `scripts/send_email.py` → `generate_five_editorial_packages()` / `call_gemini_with_retry()` | `daily-duck.yml` → "Send Daily Duck email" (scheduled) | Write full bilingual editorial package (title/body/duck line/X post, EN+JA) for all 5 shortlisted stories | `gemini-3.6-flash` | `google.genai` SDK, `client.models.generate_content` | 1 | `EDITORIAL_MAX_ATTEMPTS`(3) × `GEMINI_API_MAX_ATTEMPTS`(5, env-overridable) | **15** |
| 3 | `scripts/generate_image_concepts.py` → `generate_options()` / `call_gemini_with_retry()` | `design-options.yml` → "Generate 3 concepts…" (human-triggered, after Gate A story approval) | Write 3 visual concept briefs + 3 candidate publication titles for the *approved* story | `gemini-3.6-flash` | `google.genai` SDK | 1 | `CONCEPT_MAX_ATTEMPTS`(3, env `EDITORIAL_MAX_ATTEMPTS`... actually `CONCEPT_MAX_ATTEMPTS` env, default 3) × `GEMINI_API_MAX_ATTEMPTS`(5) | **15** |
| 4 | `scripts/regenerate_titles.py` → `generate_titles()` / `call_gemini()` | `regenerate-titles-only.yml` (optional, human-triggered "give me new titles" path) | Regenerate 3 alternate titles only; images untouched | `gemini-3.6-flash` | `google.genai` SDK | 0–1 (optional, not part of the baseline daily cycle) | 1 × 5 (`GEMINI_API_MAX_ATTEMPTS`), **no outer validation-retry loop** — a bad response raises immediately | **5** |

Image generation is **already on OpenAI, not Gemini**:

| File | Purpose | Model | Notes |
|---|---|---|---|
| `scripts/generate_image_concepts.py` → `generate_one_image()` | 1 real image per concept (3 total) at Gate A design stage | `gpt-image-2` via `OpenAI` SDK | `OPENAI_API_KEY` already a live repo secret |
| `scripts/generate_design_previews.py` → `generate_image_bytes()` | 3 final image variations of the human-selected concept | `gpt-image-2` via `OpenAI` SDK | Same key; `design-selection-check.yml` carries **no** `GEMINI_API_KEY` at all |

**No hidden/indirect Gemini usage was found.** `collect_news.py`, `publish_website.py`,
`publish_x.py`, `build_x_card.py`, `generate_x_cards.py`, `check_story_approval.py`,
`check_design_selection.py`, `automation_chain_audit.py`, and all email/notification
scripts other than `send_email.py` do not reference Gemini at all — confirmed by both
targeted grep and by reading the files that had any Gemini-adjacent grep hit
(`send_publish_complete_email.py`, `recovery_audit.py`, `model_audit.py`,
`gemini_retry_audit.py`, `gemini_retry.py`) end to end.

### Retry-wrapper consistency finding (new — not previously documented)

`scripts/gemini_retry.py` exports a shared `call_with_retry()` helper, and
`scripts/gemini_retry_audit.py` is an existing internal check (wired to
`.github/workflows/gemini-retry-audit.yml`, manual-dispatch only) whose job is
to verify every script calling `client.models.generate_content(` imports and
uses that shared helper.

**None of the 3 SDK-based call sites (`send_email.py`, `generate_image_concepts.py`,
`regenerate_titles.py`) actually use `gemini_retry.py`.** Each reimplements its
own near-duplicate exponential-backoff retry function locally, and
`rank_news_with_ai.py` implements a fourth, structurally different (flat-delay,
HTTP-status-code-based) retry scheme on top of raw `urllib`. This was confirmed
by actually running the existing audit script read-only:

```
$ python scripts/gemini_retry_audit.py
GEMINI RETRY AUDIT FAILED
- scripts\generate_image_concepts.py: unwrapped Gemini generate_content call
- scripts\generate_image_concepts.py: retry helper import missing
- scripts\regenerate_titles.py: unwrapped Gemini generate_content call
- scripts\regenerate_titles.py: retry helper import missing
- scripts\send_email.py: unwrapped Gemini generate_content call
- scripts\send_email.py: retry helper import missing
EXIT_CODE=1
```

This audit is manual-dispatch only, so it isn't blocking the daily pipeline —
but it is currently **false-negative on every run**, and `429_UPDATE_README.txt`
(already in the repo) explicitly tells a human to expect "GEMINI RETRY AUDIT
PASSED" as a test step. Right now that step would fail. This is stale tooling,
not a production incident, but it should be reconciled as part of any
provider-abstraction work (Phase 10) rather than left to mislead the next person
who runs it.

A second, same-shape finding: `scripts/model_audit.py` (also manual-dispatch
only, `.github/workflows/model-audit.yml`) still asserts that the literal string
`gemini-3.1-flash-lite-image` must appear across `model_config.py`,
`generate_image_concepts.py`, `generate_design_previews.py`, and two workflow
files — i.e. it still expects Gemini to be the image-generation model. Image
generation moved to `gpt-image-2` (OpenAI) at some point after `MODEL_AUDIT.md`
and `model_audit.py` were written. Confirmed by running it read-only:

```
$ python scripts/model_audit.py
MODEL AUDIT FAILED
- Missing image model: gemini-3.1-flash-lite-image
EXIT_CODE=1
```

`scripts/recovery_audit.py`, by contrast, is already correct/up to date — it
explicitly asserts `gemini-3.1-flash-lite-image` must **not** be present and
`gpt-image-2` / `OPENAI_API_KEY` must be. So the repo already contains one
audit script that reflects current reality (`recovery_audit.py`) and two that
don't (`model_audit.py`, `gemini_retry_audit.py`). Recommend reconciling all
three together when the provider wrapper lands, so there is one source of
truth instead of three drifting checks.

---

## PHASE 2 — Request budget

Definitions used throughout this report:

- **Logical LLM operation** = one editorial task the pipeline needs an answer
  to (e.g. "rank today's news"), regardless of how many physical HTTP calls
  it takes to get a usable answer.
- **Physical API request** = one actual call that counts against Gemini's
  quota, including every retry attempt.

### Normal-path (baseline daily cycle, no errors)

| Logical operation | Physical requests (happy path) |
|---|---|
| News ranking | 1 |
| Editorial copy (5 stories, EN+JA) | 1 |
| Design-options concepts + titles | 1 |
| **Total** | **3 logical operations = 3 physical requests** |

(`regenerate_titles.py` is optional/on-demand and is not part of this baseline.)

### Worst-case (every call exhausts its retry budget)

| Logical operation | Retry shape | Worst-case physical requests |
|---|---|---|
| News ranking | 1 × 4 | 4 |
| Editorial copy | 3 × 5 | 15 |
| Design-options | 3 × 5 | 15 |
| **Total (baseline 3 ops)** | | **34** |
| + optional title regen, if also triggered same day | 1 × 5 | +5 → **39** |

### Free-tier 20-request feasibility

**NOT_SAFE**, and this is the most load-bearing finding in this audit.

The normal path (3 requests) fits comfortably inside a 20-request quota. But
the retry architecture itself is what turns a single transient Gemini hiccup
into a quota-exhausting event: **the editorial-copy step and the
design-options step can each alone consume 15 requests — 75% of the entire
observed quota — before that one logical operation even finishes**, entirely
automatically, with no circuit breaker and no way to abort early once the
retry loop starts. Two such operations in the same day (which the normal
Gate A → Gate B flow produces across two separate workflow runs) can plausibly
exceed 20 on a single day where Gemini is simply slow or briefly overloaded —
without any unusual traffic, and without a human doing anything wrong.

In other words: the current retry logic was written to *survive* transient
429/503s, but on a 20-request/day-class free tier it is more likely to be the
*cause* of quota exhaustion than the cure for it. This is consistent with
the observed production incident (429 RESOURCE_EXHAUSTED on `gemini-3.6-flash`
at a 20-request free-tier value).

One honest caveat: the task states "observed free-tier quota value: 20
requests" without specifying whether that is a per-minute or per-day limit.
The repository contains no log/telemetry capture of the actual failing run,
so this audit cannot determine which. The conclusion above (NOT_SAFE) holds
under either interpretation — a per-day cap is blown by one bad day of retries
as shown; a per-minute cap is even more exposed, since the exponential-backoff
schedules (10s → 20s → 40s → 80s, or 10s → 30s → 60s) pack several attempts
into a single wall-clock minute.

---

## PHASE 3 — Failure classification policy (design)

| Error category | Current behavior today | Recommended policy |
|---|---|---|
| **429 RESOURCE_EXHAUSTED** (quota) | Retried like any other transient error, up to 4–5 times per call site, exhausting more of the same scarce quota, then fails closed + emails failure notice | Small bounded retry (1–2 attempts, short delay) to absorb genuine short-lived rate limiting → if still failing, **FALLBACK_TO_OPENAI** immediately rather than continuing to retry into a quota that won't refill mid-run. Log `PROVIDER_QUOTA_EXHAUSTED`. |
| **503 UNAVAILABLE** (temporary capacity) | Already retried (both the HTTP-code set and the substring-matching lists include 503/"unavailable") | Keep bounded retry (existing budgets are reasonable for this category) → on exhaustion, **FALLBACK_TO_OPENAI**. Log `PROVIDER_TEMPORARY_FAILURE`. |
| **401 / 403** (auth/permission) | Already correctly *not* retried (excluded from all four retryable-error lists) → fails immediately | Keep **FAIL_CLOSED + HUMAN_ALERT**. Do *not* fall back — a bad/expired `GEMINI_API_KEY` is a configuration bug; silently routing every day through OpenAI would hide it indefinitely and create unbounded, unnoticed OpenAI spend. |
| **400 invalid request** | Not retried, fails immediately | Keep **FAIL_CLOSED + HUMAN_ALERT**, not fallback — per the task's own caution, a malformed prompt/schema is a code bug in *this* repo, and OpenAI would likely reject or mishandle the same malformed request differently rather than fixing it. |
| **Timeout / network failure** | Retried (both client families treat timeouts as retryable) | Keep bounded retry → on exhaustion, **FALLBACK_TO_OPENAI**. |
| **Malformed model output** (valid HTTP 200, but JSON fails schema/field validation) | 2 of 4 call sites already self-heal via an outer re-prompt loop (`EDITORIAL_MAX_ATTEMPTS`); `rank_news_with_ai.py`'s `validate_result()` runs *outside* any retry loop today and fails immediately on a single bad ranking response | Keep a small bounded **re-prompt-the-same-provider** retry first (cheap, often fixes one-off formatting slips) → on exhaustion, **FALLBACK_TO_OPENAI is safe here specifically because Phase 6's validation is provider-independent** — if OpenAI's output also fails the same shared validator, fail closed. |

General rule carried through the design: **retry is for "ask the same model
again," fallback is for "ask a different model," and neither is a substitute
for "stop and tell a human."** Auth and malformed-request errors stay
fail-closed by design, matching the task's explicit instruction not to let
provider-switching silently mask a broken request.

---

## PHASE 4 — OpenAI fallback feasibility (`gpt-5.6-luna`, candidate only)

Important honest limitation: `gpt-5.6-luna` is a model this audit has no
vendor documentation for (it postdates this assistant's knowledge and was not
otherwise described in the task beyond the pricing figures). Nothing below
should be read as a confirmed spec — it is what's *architecturally required*
given how the repo currently uses Gemini, cross-checked against what is
generically true of OpenAI's chat/JSON-output API surface.

| Dimension | Gemini usage today | What OpenAI-side would need |
|---|---|---|
| API interface | 3 of 4 sites use `google.genai` SDK `client.models.generate_content(model=, contents=)`; 1 site uses raw REST | OpenAI SDK (already a repo dependency, `openai>=1.0.0`) chat/responses call — different call shape, isolated entirely inside the new provider wrapper |
| Structured JSON output | Gemini enforced via `generationConfig.responseMimeType="application/json"` + `responseJsonSchema` (ranking) or via prompt instruction + manual `json.loads()` (editorial/design) | Needs OpenAI's structured-output mechanism (schema-constrained JSON) to match the *ranking* site's strictness; the other two sites' prompt-instructed-JSON approach is a weaker pattern already and would carry over unchanged in risk profile, not made worse by switching provider |
| Token limits | No explicit `max_output_tokens` set anywhere in the 4 call sites (Gemini SDK defaults used) | Should be set explicitly for the fallback path, since editorial copy (5 stories × 11 fields) is the largest output shape (~2.5–4K tokens estimated, see Phase 9) |
| Temperature/settings | Only `rank_news_with_ai.py` sets `temperature=0.2`; the other 3 sites use SDK defaults | Not a blocker; should be set to match existing behavior as closely as possible per call site |
| System/developer/user roles | Gemini calls today send **everything as a single `user`/`contents` blob** — no system-instruction separation is used anywhere in this codebase | Fully compatible; OpenAI's roles are a superset. No prompt rewriting is required to make this work — the existing single-block prompts can be sent as-is as one user message. (Splitting into system/developer/user is an *optional* future improvement, not a compatibility requirement.) |
| Response parsing | All 4 sites read `response.text` (or REST-equivalent) and `json.loads()` it after `clean_json_text()` markdown-fence stripping | The wrapper only needs to normalize to the same `.text`-shaped return so none of the 4 call sites' parsing code has to change |
| Retry/timeout handling | 4 different bespoke implementations (Phase 1 finding) | The new wrapper should be the *one* place retry/timeout logic lives for both providers — this also finally resolves the `gemini_retry_audit.py` drift noted above |
| Deterministic expectations | None of the 4 prompts rely on Gemini-specific features (no grounding/search tool, no vision input, no citations) | No hard technical blocker found for any of the 4 tasks |
| Model-specific prompt assumptions | Prompts are generic natural-language instructions with embedded JSON examples; no Gemini-only syntax, function-calling schema, or vendor-specific tokens were found in any of the 4 prompt bodies | Low migration risk on the prompt-compatibility axis specifically; the real risk is *voice/quality*, not compatibility (see Q6 below) |

**No prompts are changed by this design** (per task instruction) — the above
table is what a future implementation phase would need to handle, not
something done now.

---

## PHASE 5 — Provider abstraction design (proposal, not implemented)

```
Daily Duck script (ranking / editorial / design-options / titles)
        |
        v
  llm_provider.generate(task_id, prompt, schema=None)
        |
        +-- Gemini PRIMARY  (google.genai, existing prompts unchanged)
        |         |
        |         +-- success --------------------------> return result
        |         +-- 429/503/timeout, bounded retry exhausted --+
        |         +-- 401/403/400 ---------------------> raise (FAIL_CLOSED)
        |                                                        |
        |                                                        v
        +-- OpenAI FALLBACK (gpt-5.6-luna, same prompt text, same schema)
                  |
                  +-- success, passes SAME validator ---> return result
                  +-- failure or fails validator --------> raise (FAIL_CLOSED)
```

Key properties:

- **One new module** (e.g. `scripts/llm_provider.py`) replaces the 4
  bespoke retry functions and the never-used `scripts/gemini_retry.py`
  with a single implementation of the Phase 3 policy table.
- Every one of the 4 call sites keeps its existing prompt-building,
  JSON-schema, and output-validation code untouched — they only swap their
  local `call_gemini(...)`/`call_gemini_with_retry(...)` call for
  `llm_provider.generate(...)`. Downstream code (`validate_result()`, the
  `required_fields` loops in `send_email.py` / `generate_image_concepts.py`)
  already operates purely on the parsed dict, with zero references to
  "Gemini" — so it needs **no changes at all**.
- The wrapper returns (or the caller records) which provider/model actually
  produced the result, for Phase 7 observability and Phase 6 quality gating.
- The wrapper is scoped **only** to the LLM generation calls. It must not
  touch the existing idempotency guards in `design-options.yml` (the
  "already exists for this issue_date → skip" check) or the publish-side
  duplicate protections in `website-publish.yml`/`x-publish.yml` — see
  Q11/Q12 below.

---

## PHASE 6 — Quality protection

### Validation that already exists today (and is already provider-independent)

| Call site | Existing validation | Provider-agnostic today? |
|---|---|---|
| Ranking (`rank_news_with_ai.py`) | `validate_result()`: exactly 5 stories, valid/unique candidate IDs, `recommended_id` must be one of the 5, rejects already-published or in-selection-duplicate URLs | **Yes** — operates only on the parsed dict, never references Gemini |
| Editorial (`send_email.py`) | Exactly 5 stories returned, IDs preserved in order, all 11 required text fields non-empty per story | **Yes** |
| Design options (`generate_image_concepts.py`) | Exactly 3 concepts + 3 titles, all required fields non-empty | **Yes** |
| Images (already OpenAI) | SHA-256 duplicate-image rejection (`MAX_DUPLICATE_RETRIES`), exact count checks | **Yes** — already proven working across a provider swap, since images already moved from Gemini to OpenAI without any change to this logic |
| Duplicate-publish protection | `remove_already_published_urls()` / `remove_same_day_duplicates()` run in plain Python **before** any LLM sees the candidate list | **Yes** — independent of which model ranks the (already-filtered) list |

Because all of the above validation is structural (shape/field/count/URL
checks) rather than provider-specific, **it already works unmodified for an
OpenAI-produced response** — this is the main reason a fallback is safe to
add without a validation rewrite.

### Gap found (pre-existing, independent of this fallback project)

There is **no deterministic post-hoc filter** for the "avoid negative/sad
stories" or "prohibited category" instructions in `rank_news_with_ai.py`'s
prompt — that constraint is enforced only by asking the model nicely in the
prompt text. This gap exists today with Gemini alone; adding an OpenAI
fallback does not create it, but it does mean a fallback response is
protected by exactly as little (or as much) as a Gemini response is today.
Recommend addressing this as a shared, provider-independent keyword/category
guard in whatever phase adds the fallback validator (Phase C below), since it
improves both providers at once — flagged here per the task's explicit ask,
not treated as blocking.

### Rule for the new design

**Fallback output must pass the exact same validator function the Gemini
path already uses.** If it doesn't, the pipeline does **not** publish — it
fails closed and alerts a human, identically to how a bad Gemini response
behaves today. No new "OpenAI-only" leniency is introduced anywhere.

---

## PHASE 7 — Observability (design only)

Proposed structured log lines around the new wrapper (no secrets, matching
the task's example shape):

```
LLM_TASK=news_ranking LLM_PROVIDER=gemini LLM_MODEL=gemini-3.6-flash LLM_ATTEMPT=1 LLM_RESULT=success
LLM_TASK=editorial_copy LLM_PROVIDER=gemini LLM_MODEL=gemini-3.6-flash LLM_ATTEMPT=1 LLM_RESULT=503
LLM_TASK=editorial_copy LLM_PROVIDER=gemini LLM_MODEL=gemini-3.6-flash LLM_ATTEMPT=2 LLM_RESULT=429
FALLBACK_TRIGGERED=true FALLBACK_REASON=quota_exhausted
LLM_TASK=editorial_copy LLM_PROVIDER=openai LLM_MODEL=gpt-5.6-luna LLM_ATTEMPT=1 LLM_RESULT=success
VALIDATION_RESULT=pass PROVIDER_USED=openai
```

`scripts/automation_chain_audit.py` already tracks workflow-level state
transitions (`approved_story` → `design_options` → `ready_to_publish`, with
timeout/staleness detection) but has **no concept of *why* a step was slow or
failed** — it only sees GitHub Actions run status. It should eventually be
extended to surface the four categories the task asks for
(`PROVIDER_TEMPORARY_FAILURE`, `PROVIDER_QUOTA_EXHAUSTED`,
`FALLBACK_SUCCESS`, `ALL_PROVIDERS_FAILED`), most naturally by having the
wrapper write a small `automation_state/llm_provider_events.json` (or similar)
that `automation_chain_audit.py` reads the same way it already reads
`approved_story.json` / `design_options.json`. **This is design-only — the
audit script is not modified in this pass**, per the task's instruction.

---

## PHASE 8 — Secrets / GitHub Actions plan (documented, not executed)

No secret values were requested, displayed, or inspected. No GitHub Secret
was created, changed, or read for its value.

**New secret required: very likely `NONE`**, with one open verification item.

`OPENAI_API_KEY` is **already a live repository secret**, already used
successfully in production today by `generate_image_concepts.py` (image half)
and `generate_design_previews.py`, wired through `design-options.yml` and
`design-selection-check.yml`. A standard OpenAI API key is normally valid
across both the Images and Chat/Responses endpoints on the same
account/project, so the same secret should also work for a `gpt-5.6-luna`
text fallback without provisioning anything new.

The one thing this repo-only audit cannot verify (and did not attempt to
verify, since that would require a live authenticated API call, out of scope
for a read-only pass): **whether the existing `OPENAI_API_KEY` is scoped to
images only.** If a future implementation phase finds it is scoped narrowly,
the clean fix is a second, purpose-scoped secret (e.g.
`OPENAI_TEXT_API_KEY`) so a text-fallback bug can never accidentally draw on
image-generation budget or vice versa. This should be a one-line check in
Phase F ("controlled production test"), not a blind assumption either way.

Two other OpenAI secrets already exist for an unrelated purpose and are **not**
usable for generation calls: `OPENAI_ADMIN_KEY` and `OPENAI_PROJECT_ID`,
wired into `.github/workflows/test-openai-cost.yml` to read the OpenAI
organization Costs API. These are admin/reporting-scoped, separate from the
generation key, and already give this project a working mechanism for
tracking actual OpenAI spend — useful context for Phase 9, not something that
needs to be duplicated for the fallback.

**Workflows that would eventually need `OPENAI_API_KEY` for the fallback path**
(currently only have `GEMINI_API_KEY`):

- `.github/workflows/daily-duck.yml` — ranking step, editorial-email step
- `.github/workflows/regenerate-titles-only.yml` — title regeneration step

`.github/workflows/design-options.yml` already carries both keys (it already
does OpenAI image generation alongside Gemini text generation), so no new
wiring would be needed there — only the in-script provider logic changes.

---

## PHASE 9 — Cost estimate

Pricing used, exactly as given in the task (no other figures assumed):
`gpt-5.6-luna` — input **$0.20 / 1M tokens**, output **$1.20 / 1M tokens**.

### Token-size basis (bounded estimate — repository evidence is insufficient for a precise figure, stated explicitly rather than faked)

- `rank_news_with_ai.py`'s prompt embeds the **full candidate list** (count
  not hard-capped in `collect_news.py` by any constant this audit could find)
  plus **up to 60 archive-history items** (`MAX_HISTORY_ITEMS = 60`; currently
  35 exist in `data/archive.json`, each truncated to 500/350-char fields in
  the prompt). This is the single largest and least-certain input.
- `send_email.py` and `generate_image_concepts.py` prompts are dominated by
  large fixed instructional text (voice/pun rules) plus a small amount of
  per-story data — comparatively stable and easier to bound.

| Scenario | Ranking in / out | Editorial in / out | Design-options in / out | **Total in / out (tokens)** |
|---|---|---|---|---|
| LOW | 15,000 / 1,000 | 2,000 / 3,000 | 4,000 / 1,500 | 21,000 / 5,500 |
| EXPECTED | 30,000 / 1,200 | 2,300 / 3,500 | 6,000 / 2,000 | 38,300 / 6,700 |
| HIGH | 50,000 / 1,500 | 2,500 / 4,000 | 8,000 / 2,500 | 60,500 / 8,000 |

### A. FALLBACK-ONLY (Gemini handles normal traffic; OpenAI only used when Gemini fails)

Cost **per triggered fallback event** (all 3 logical operations, i.e. one full
publication run entirely on OpenAI):

| Scenario | Cost per publication (if fully on OpenAI) |
|---|---|
| LOW | ~$0.011 |
| EXPECTED | ~$0.016 |
| HIGH | ~$0.022 |

Actual monthly cost depends on how often Gemini actually fails badly enough
to trigger fallback — even at a (pessimistic) 100%-of-days trigger rate this
is **≈$0.33–$0.66/month**; at a more realistic occasional-failure rate it is
**a few cents a month or less**. This is the FALLBACK-ONLY figure.

### B. OPENAI-PRIMARY (hypothetical — all 3 text operations moved to OpenAI every day)

| Scenario | Cost per publication | Cost per 30 days |
|---|---|---|
| LOW | ~$0.011 | ~$0.33 |
| EXPECTED | ~$0.016 | ~$0.47 |
| HIGH | ~$0.022 | ~$0.66 |

### Important scope note

**Both figures above are text-only** and exclude the OpenAI image-generation
spend (`gpt-image-2`) the project is *already* paying today regardless of
this decision — that spend is untouched by this proposal and already has its
own monitoring path via the existing `test-openai-cost.yml` / OpenAI Costs
API integration. This audit was given no per-image pricing figure and does
not invent one. Given how small the text-side numbers are (pennies to well
under $1/month in every scenario), **cost is not a material factor** in the
primary-vs-fallback decision; reliability and quality protection are.

---

## PHASE 10 — Implementation plan (staged, not started)

| Phase | Scope |
|---|---|
| **A** | Add `scripts/llm_provider.py` (Phase 5 wrapper) + unit tests against the Phase 3 error-classification table, using fixture/mocked responses only — no live API calls. Retire the unused `scripts/gemini_retry.py` and reconcile `gemini_retry_audit.py` / `model_audit.py` with current reality (Phase 1 finding) so they stop reporting stale failures. |
| **B** | Wire OpenAI fallback into **news ranking only** (`rank_news_with_ai.py`) — smallest, most self-contained call site (flat 4-attempt retry, single-script validator already isolated in `validate_result()`). |
| **C** | Add the shared, provider-independent validation hardening identified in Phase 6 (negative/prohibited-story guard), applied to both providers equally. |
| **D** | Extend fallback to the remaining Gemini call sites — editorial copy (`send_email.py`), design-options (`generate_image_concepts.py`), and optionally title regeneration (`regenerate_titles.py`) — in that order, each gated by its own existing validator. |
| **E** | Observability integration: wrapper emits the Phase 7 log lines; extend `automation_chain_audit.py` to read a new `llm_provider_events` state file and surface `PROVIDER_TEMPORARY_FAILURE` / `PROVIDER_QUOTA_EXHAUSTED` / `FALLBACK_SUCCESS` / `ALL_PROVIDERS_FAILED`. |
| **F** | Controlled production test: verify `OPENAI_API_KEY` actually authorizes `gpt-5.6-luna` (Phase 8 open item), run one manual `workflow_dispatch` cycle with Gemini deliberately forced to fail (e.g. a temporary bad model name in a test branch, never in `main`), confirm fallback fires, output passes validation, and the existing idempotency/duplicate-publish guards (Q11/Q12) are respected end to end before any scheduled/production reliance on the fallback path. |

Gradual rollout (A → F) is recommended over a repository-wide replacement, per
the task's preference and because the 4 call sites currently have 4 different
retry implementations that should converge one at a time, verified against
their own existing validator each step.

---

## MANDATORY QUESTIONS

**1. How many Gemini logical operations occur in one normal Daily Duck cycle?**
Three: news ranking, editorial copy generation (5 stories), and design-options
(concepts + titles). A fourth (title regeneration) is optional/on-demand and
not part of the baseline cycle.

**2. Approximately how many physical Gemini API requests can occur including retries?**
Normal path: 3. Worst case: **34** (39 if the optional title-regen path is
also triggered the same day) — see Phase 2.

**3. Which call site most likely exhausted the observed free-tier quota?**
Most likely `send_email.py` (editorial copy) or `generate_image_concepts.py`
(design-options) — both share the largest retry-amplification factor in the
codebase (3 × 5 = 15 physical requests per single logical operation), by far
exceeding `rank_news_with_ai.py`'s flat 4-attempt ceiling. This cannot be
pinned to one exact script with certainty from the repository alone (no
failing-run logs were available to this audit); confirming which requires
checking the specific GitHub Actions run's step logs for the incident.

**4. Can Gemini remain PRIMARY safely?**
Yes — conditionally. Nothing found in this audit argues for demoting Gemini;
cost is trivial either way, and Gemini is already deeply prompt-tuned for
this project's editorial voice. The condition is that the retry-amplification
problem (Phase 2) gets fixed — via tighter bounded retries plus the Phase 3
fallback policy — rather than left as-is, since the current architecture is
plausibly self-inflicting the quota exhaustion it's trying to survive.

**5. Is `gpt-5.6-luna` technically suitable as fallback for news ranking?**
Plausibly yes — the task has no unusual requirements (no vision, no
vendor-specific tooling, JSON-schema-constrained output is a standard OpenAI
capability) — but this cannot be *confirmed* by a static repository audit for
a model outside this assistant's knowledge. Needs the Phase F smoke test
before being trusted in production.

**6. Is it suitable for email generation (editorial copy)?**
Technically plausible on the same grounds as Q5, with a higher *quality/voice*
risk (the prompt encodes a very specific creative style — duck-pun mechanics,
3-lane title variety) that a model swap could shift even with 100%
JSON-shape compatibility. This risk is substantially mitigated by an
**existing, load-bearing safeguard**: a human already reviews and picks among
the 5 generated options at the Gate A approval email before anything
publishes, so a lower-quality fallback-authored option is caught by the
existing human gate, not published blind.

**7. Are there any tasks where Gemini should remain mandatory?**
No hard technical lock-in was found — none of the 4 call sites use a
Gemini-only feature. News ranking (dedup/diversity judgment against a growing
archive) is the most quality-sensitive of the three and deserves the most
scrutiny during rollout (Phase B is deliberately first and smallest for this
reason), but "most sensitive to verify" is not the same as "mandatory."

**8. What is the smallest production-code change required?**
One new module (`scripts/llm_provider.py`) implementing the Phase 3/5 design,
with each of the 4 existing call sites swapping their local retry function
for a call into it. No prompt, schema, or validation code changes are
required in any of the 4 files, since all existing validation already
operates on the parsed dict independent of provider.

**9. What new GitHub secret would be required?**
Very likely **none** — `OPENAI_API_KEY` already exists and is already used in
production for image generation, and a standard OpenAI key is normally valid
for chat/text endpoints on the same account. This should be verified (not
assumed) in Phase F; if the existing key turns out to be images-scoped, a
second purpose-scoped secret (e.g. `OPENAI_TEXT_API_KEY`) would be the clean
fallback.

**10. What is the estimated monthly OpenAI cost?**
Text-fallback cost is trivial in every scenario modeled: roughly
**$0.33–$0.66/month even under the pessimistic assumption that fallback fires
on every single publication**; a few cents a month or less under a realistic
occasional-failure rate. This excludes the pre-existing, already-incurred
image-generation spend, which this proposal does not change. See Phase 9.

**11. Could fallback cause duplicate publication or duplicate state commits?**
Potentially, if implemented carelessly — e.g. a fallback attempt inside a
re-run of a workflow that had already partially committed state could produce
two divergent `design_options.json` commits, or content authored by two
different providers for the same `issue_date`.

**12. How will the design prevent that?**
By scoping the provider wrapper strictly to "generate and validate," and
never letting it bypass the **existing** idempotency/duplicate-protection
logic already in the repo:
`design-options.yml`'s `should_run` gate already skips regeneration entirely
if a design package for the same `issue_date` already exists;
`automation_chain_audit.py` already cross-checks state-transition consistency
across workflow runs; and the website/X publish-side duplicate protections
referenced in `429_UPDATE_README.txt` are explicitly out of scope for this
change and must not be touched. The wrapper only ever races Gemini against
OpenAI *within a single generation attempt, within a single workflow run,
before anything is committed or emailed* — once one provider succeeds and
passes validation, the existing downstream commit/email/publish flow proceeds
exactly as it does today, unchanged. Recording `LLM_PROVIDER`/`LLM_MODEL` in
the generated state JSON (Phase 7) makes any future re-run's provenance
auditable without changing whether a re-run is allowed.

---

## Compliance note (Strict Prohibitions)

This audit did not: modify production code, modify workflow YAML, change the
Gemini model, add the OpenAI SDK (it was already a dependency), add any new
dependency, create API keys, inspect any secret's value, change any GitHub
Secret, commit, push, manually run a workflow, or publish website/X content.
Two existing internal audit scripts (`gemini_retry_audit.py`, `model_audit.py`)
were executed locally and read-only, purely to confirm their current pass/fail
status as evidence for this report — this does not write, publish, or touch
git state, and is consistent with "audit + design phase only."

---

# FINAL REPORT

**STATUS:** PASS_WITH_FINDINGS

**CURRENT_GEMINI_CALL_SITES:** 4 real call sites — `rank_news_with_ai.py`
(ranking), `send_email.py` (editorial copy), `generate_image_concepts.py`
(design-options text only — its image half is already OpenAI),
`regenerate_titles.py` (optional title regen). Full detail in Phase 1 table.

**NORMAL_LOGICAL_LLM_OPERATIONS:** 3 (ranking, editorial, design-options) per
publication cycle; +1 optional (title regen).

**ESTIMATED_NORMAL_API_REQUESTS:** 3

**ESTIMATED_WORST_CASE_API_REQUESTS:** 34 (39 with optional title regen)

**FREE_TIER_20_REQUEST_FEASIBILITY:** NOT_SAFE — normal path fits; the
retry architecture alone can consume 75%+ of the quota inside a single
logical operation, plausibly self-inflicting the observed exhaustion.

**RECOMMENDED_ARCHITECTURE:** Gemini stays PRIMARY for all 4 call sites;
one shared provider wrapper adds a bounded-retry-then-OpenAI-fallback policy
per Phase 3/5, gated by the SAME existing per-call-site validators (Phase 6),
rolled out incrementally starting with news ranking (Phase 10).

**PRIMARY_PROVIDER:** Gemini (`gemini-3.6-flash`)

**FALLBACK_PROVIDER:** OpenAI

**FALLBACK_MODEL:** `gpt-5.6-luna` (candidate; unverified — confirm suitability
via Phase F smoke test before production reliance, per task instruction not
to assume it's final)

**FALLBACK_TRIGGER_POLICY:** Bounded retry on Gemini first (small, provider
category-dependent per Phase 3) → fallback to OpenAI on 429/503/timeout/
malformed-output exhaustion → fail closed + human alert on 400/401/403 and on
any exhaustion of both providers. Never fallback silently on
auth/config/invalid-request errors.

**QUALITY_GATES:** Reuse existing structural validators unchanged for both
providers (exact-count, required-field, ID-preservation, duplicate-URL,
duplicate-image-hash checks — all already provider-independent); add a
provider-independent negative/prohibited-story guard as a hardening item
(pre-existing gap, not fallback-specific); fallback output that fails
validation blocks publication exactly as a bad Gemini response would today.

**NEW_SECRET_REQUIRED:** Very likely NONE — `OPENAI_API_KEY` already exists
and is already used in production; verify (don't assume) it also authorizes
`gpt-5.6-luna` before relying on it.

**ESTIMATED_OPENAI_COST_PER_PUBLICATION:** ~$0.011–$0.022 (text only; see
Phase 9 for the token-size assumptions this range rests on)

**ESTIMATED_OPENAI_COST_PER_MONTH:** ~$0.33–$0.66 even under a pessimistic
100%-fallback-trigger assumption; realistically a few cents/month or less.
Excludes pre-existing image-generation spend, which this proposal doesn't change.

**IMPLEMENTATION_PHASES:** A) provider wrapper + tests + retire stale retry
tooling → B) fallback for news ranking only → C) shared validation hardening
→ D) fallback for remaining call sites → E) observability/alert integration
→ F) controlled production test (incl. verifying the OpenAI key's scope).

**RISKS:** (1) Retry-amplification is the dominant quota risk today,
independent of any fallback decision — must be addressed regardless. (2)
Editorial-copy voice/quality drift under OpenAI is a real but human-gated
risk (existing Gate A approval email already catches it). (3) Two internal
audit scripts (`gemini_retry_audit.py`, `model_audit.py`) are currently
stale/false-failing and should not be trusted as-is until reconciled. (4)
`gpt-5.6-luna`'s actual capabilities/limits are unverified by this audit.
(5) Whether the existing `OPENAI_API_KEY` is scoped for text generation is
unverified.

**FILES_MODIFIED:** NONE (production code, workflows, dependencies, models,
and secrets all untouched)
**Report-only file created:** `LLM_PROVIDER_AUDIT_AND_FALLBACK_PLAN.md`

**COMMIT:** NOT_PERFORMED

**PUSH:** NOT_PERFORMED

**RECOMMENDATION:** GO_WITH_CHANGES — proceed with the staged implementation
plan (Phase 10), starting with Phase A/B, but do not skip fixing the
retry-amplification problem (it is the actual root cause of the observed
429, and fallback alone does not fix it — a quota-exhaustion 429 that fires
after 15 self-inflicted retries still wastes those 15 requests before
fallback ever gets a chance to help).

**NEXT:** WAITING_FOR_HUMAN_APPROVAL
