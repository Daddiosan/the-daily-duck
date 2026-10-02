# Approval Recovery and Hardening Readiness — 2026-10-02

Status: production recovery complete; consolidated hardening deployed and
validated on 2026-10-02, with the explicitly listed monitoring extensions
still requiring a reviewed collector/design.

- External Fallback: `DEPLOYED_AND_VALIDATED`.
- Monitoring core: `DEPLOYED_AND_VALIDATED`.
- Pub/Sub hardening: `DEPLOYED_AND_VALIDATED`.
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
| Fallback Cloud Run | Ready; revision `daily-duck-approval-fallback-a168e1d`; 100%; authenticated-only |
| Fallback runtime SA | deployed; no project roles; GitHub App key accessor only |
| Fallback Scheduler SA | deployed; no project roles; service-level invoker only |
| Fallback Scheduler jobs | both enabled; both validated HTTP 200 |
| Fallback registry image | immutable index digest `sha256:39673b672b70c8c899b886d2a7a32ef87534893fa12cd81856e7e895a92bd3bb` |
| GitHub App key | explicit secret version `1` |
| OAuth canary Scheduler | enabled; HTTP 200 validated |
| Monitoring policies/channels/log metrics | six / one / four; notification smoke passed |
| Pub/Sub source | retention 604800s; retry 10--600s; DLQ max attempts 10 |
| Pub/Sub DLQ | topic plus seven-day ops subscription; service-agent IAM verified |
| GitHub checker schedules | both active; retained as tertiary best-effort paths |

## Repository validation

- Application source baseline for the deployed fallback image: `a168e1d6c877`.
- Full test suite: 694/694 PASS.
- Focused duplicate/fallback safety tests: 108/108 PASS.
- Local image: `approval-fallback:a168e1d6c877`.
- Local image ID:
  `sha256:39673b672b70c8c899b886d2a7a32ef87534893fa12cd81856e7e895a92bd3bb`.
- Container user: `fallback` (non-root).
- Local health: HTTP 200.
- Unauthenticated fixed route: HTTP 401.
- Unknown route: HTTP 404.
- Worktree and image secret-pattern scan: no credential material found. The
  only tracked candidate was a test assertion containing a PEM marker string,
  not a key.

The recorded digest is the immutable Artifact Registry OCI index used for the
deployment. Cloud Run resolved it to the Linux/amd64 child digest documented in
`EXTERNAL_FALLBACK_RUNBOOK.md`.

## Deployed hardening package

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
`WATCH_RENEWAL_RUNBOOK.md`. The production mutations above are complete. The
undeployed monitoring extensions are watch-expiration and business-aware
pending-wake collectors, plus a revised watch-renewal absence design that fits
platform limits.
