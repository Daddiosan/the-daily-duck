# Approval Recovery and Hardening Readiness — 2026-10-02

Status: recovery acceptance complete; hardening is ready for one consolidated
Production Human Gate.

- External Fallback: `READY_FOR_DEPLOYMENT`.
- Monitoring: `READY_FOR_DEPLOYMENT`.
- Pub/Sub hardening: `HUMAN_GATE_REQUIRED`.
- GitHub `on.schedule`: retained as a tertiary best-effort safety net.

## Accepted production evidence

- OAuth application: `External / In production`.
- Primary Relay revision: `daily-duck-approval-relay-oauth-v2-e377414`, 100%.
- OAuth secret references: client version `1`, refresh-token version `2`.
- OAuth canary: HTTP 200.
- Automatic watch renewal: HTTP 200 at 2026-10-02 03:17 JST.
- Watch expiration: 2026-10-08 21:33:04.807 JST to
  2026-10-09 03:17:05.559 JST.
- Renewal cursor safety: `37076` before and after.
- Real Design Selection reply: 2026-10-02 19:29:11 JST.
- Natural path: Gmail Push, Pub/Sub, Relay, Gmail history, GitHub App dispatch,
  and downstream checker all passed.
- Relay cursor: `37680` to `37830`.
- Relay event: `DISPATCH_CONFIRMED`, one attempt, dispatch eligible.
- GitHub run: `36995749123`, fixed `design-selection-check.yml`, `main`, success.
- Result: image/concept `2`, title `2`, `READY_TO_PUBLISH`.
- Website and X: one successful run each; no duplicate side effect.
- Pub/Sub backlog and oldest-unacked age: zero.

No secret value, message body, complete Subject, raw token, or manual cursor
edit is part of this evidence.

## Read-only production inventory

| Resource | Current state |
| --- | --- |
| Project / number | `the-daily-duck` / `424584128509` |
| Region | `asia-northeast1` |
| Primary Relay | Ready; IAM-authenticated; dedicated runtime SA |
| Artifact Registry | Docker repository `daily-duck` exists |
| Fallback Cloud Run | absent |
| Fallback runtime SA | absent |
| Fallback Scheduler SA | absent |
| Fallback Scheduler jobs | absent |
| Fallback registry image | absent |
| GitHub App key | existing secret version `1`; Relay runtime is the only accessor |
| OAuth canary Scheduler | absent |
| Monitoring policies/channels/log metrics | none |
| Pub/Sub source | retention 86400s; retry 10--600s; no DLQ |
| GitHub checker schedules | both active; retained as tertiary best-effort paths |

## Repository validation

- Application source baseline for the local fallback image: `bcca41c533d1`.
- Full test suite: 694/694 PASS.
- Focused duplicate/fallback safety tests: 108/108 PASS.
- Local image: `approval-fallback:bcca41c533d1`.
- Local image ID:
  `sha256:3482125dd56c65000da21122653954376fc9de1e39b6f97bd952ea2fcee38557`.
- Container user: `fallback` (non-root).
- Local health: HTTP 200.
- Unauthenticated fixed route: HTTP 401.
- Unknown route: HTTP 404.
- Worktree and image secret-pattern scan: no credential material found. The
  only tracked candidate was a test assertion containing a PEM marker string,
  not a key.

The local image ID is not a deployable registry digest. Artifact Registry push
is the first cloud write and remains Human-gated.

## Gated hardening package

The consolidated gate covers:

1. two dedicated fallback service accounts;
2. one minimal Secret Manager binding;
3. fallback image push and immutable digest resolution;
4. IAM-authenticated fallback Cloud Run deployment;
5. two fixed-route Cloud Scheduler jobs with retries disabled;
6. OAuth canary Scheduler deployment;
7. native and business-aware monitoring resources;
8. Pub/Sub seven-day retention, DLQ, service-agent IAM, and ops subscription.

Exact deployment and rollback commands are maintained in
`EXTERNAL_FALLBACK_RUNBOOK.md`, `MONITORING_RUNBOOK.md`, and
`WATCH_RENEWAL_RUNBOOK.md`. No production hardening mutation has been executed.
