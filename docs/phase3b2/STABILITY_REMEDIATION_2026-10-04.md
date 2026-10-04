# Post-R2 P1 Stability Remediation — 2026-10-04

Status: local review candidate only. This record does not authorize a push,
merge, deployment, workflow dispatch, rerun, cloud change, or secret change.

## Incident and recovery timeline

- The approval-path incident window began at **2026-09-29 10:45:50 JST**.
- On 2026-09-30, Gmail Push and Pub/Sub delivered the approval notification,
  but the Relay could not read Gmail history because OAuth refresh returned
  `invalid_grant`. GitHub's tertiary scheduled poller did not create a run in
  the required window.
- Recovery on 2026-10-01 pinned refresh-token secret version `2` and restored
  the Relay without resetting or manually advancing its durable Gmail cursor.
- The natural scheduled watch renewal passed on 2026-10-02. The renewal
  advanced only watch-expiration state and left the processing cursor intact.
- A natural approval end-to-end path passed on 2026-10-02: Gmail Push,
  Pub/Sub, Relay, Gmail history, GitHub App dispatch, checker, Website, and X.
- Gate A run `37068868660` on 2026-10-03 selected older successful Daily Duck
  run `34660443103`, whose retained artifact was unavailable. The old lookup
  treated workflow success as sufficient authority and did not validate
  artifact existence, expiry, freshness, or package structure.
- Gate A runs `37156273270` and `37157058380` on 2026-10-04 failed when the
  read-only GitHub run lookup returned HTTP 503. Run `37157901570` later
  recovered without intervention, confirming a transient GitHub API failure.
- The 2026-09-30 X publication reached `X_POSTED` exactly once. A later
  duplicate/no-op X workflow correctly avoided a second post but replaced the
  authoritative `x_publish_result.json` action with
  `WEBSITE_STATE_NOT_PUBLISHED_BLOCKED`.

No credential value, message body, complete Subject, approval token, or raw
provider response is included in this record.

## Local remediation contract

Before reading artifacts, Gate A synchronizes authoritative `main` and computes
the expected issue day at the workflow layer. The issue-day boundary is 07:00
JST: before that boundary, the preceding calendar day's issue remains active
for overnight approval or recovery; at and after the boundary, the current
calendar day's issue is required. That explicit date is passed to the selector.

Gate A queries the fixed `daily-duck.yml` workflow on `main` and considers at
most 20 successful runs. A candidate is usable only when all of the following
are true:

1. GitHub reports the run as completed and successful on `main`.
2. The package `issue_date` exactly equals the workflow's expected issue date;
   recency alone can never establish issue authority.
3. The run is no more than 36 hours old. This secondary filter accommodates
   delayed GitHub scheduling and overnight approval/recovery while rejecting
   obsolete output; it never overrides the exact issue-date requirement.
4. A non-expired `daily-duck-results` artifact still exists for that run.
5. Its size and entry count are bounded, its ZIP is safe to extract, and it
   contains exactly one structurally valid `gate_a_package.json` for Gate A.

Unusable newer candidates are rejected and the next newest fresh candidate is
examined only if it identifies the same expected issue. A valid, fresh
previous-day artifact is rejected when the current issue is expected. If no
authoritative candidate is usable, the checker fails closed with counts by
sanitized reason; response bodies and credentials are never included.

All network retries in this remediation are limited to read-only HTTP GETs.
HTTP 429 and 5xx responses, proven rate-limited HTTP 403 responses, plus
transport failures receive at most three total attempts. A 403 is retryable
only when `Retry-After`, or zero remaining quota with a reset time, proves rate
limiting; an ordinary authorization 403 fails immediately. `Retry-After`
values up to the 60-second local bound are honored exactly; a longer value
fails closed instead of retrying early. Other transient responses use short
exponential delays. Workflow dispatch remains single-attempt and is not
wrapped in this retry boundary.

Once `x_publish_result.json` records `X_POSTED` for an issue, that result is an
early authoritative no-republish guard independent of weaker companion state.
A later same-issue invocation makes no X API call and cannot replace the
original post ID or top-level terminal action. The no-op is retained in a
bounded `post_terminal_observations` audit list. A terminal result for a
different issue does not block a valid new issue, while contradictory current
issue identities fail closed.

## Current wake-up topology and idempotency

```text
Primary:   Gmail Push -> Pub/Sub -> Approval Relay -> GitHub App dispatch
Secondary: Cloud Scheduler -> authenticated fallback -> GitHub App dispatch
Tertiary:  GitHub Actions on.schedule (best effort)
                                |
                                v
               serialized checker + terminal state guards
```

All three paths may wake the same checker. The stage-specific concurrency
groups, Gate A same-issue guard, Design Selection `ALREADY_SELECTED` guard,
Website duplicate-date protection, and X local/remote duplicate guards remain
authoritative. This patch adds no new dispatch and does not weaken those
cross-path controls.

## Stability-window gate

The historical three-issue stability gate **FAILED**. The historical seven-day
stability gate **FAILED**. Those historical failures cannot be retroactively
repaired by local code or later healthy runs.

The external fallback and tertiary GitHub polling remain in place. A fresh
post-remediation stability window must be observed and reviewed before any
proposal to reduce or remove polling. The window begins only after this patch
passes code review, is separately approved, and is deployed; local tests do
not count as production stability evidence. Polling changes remain a separate
Human Gate.
