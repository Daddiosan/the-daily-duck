# Thin Approval Relay Runbook (Phase 3B-2 / M3B)

Component: `cloud/approval_relay/`
Task: `TDD-M3B-RELAY-INGRESS-AUTH-01`

## Trust rule

A relay event is a wake-up signal, never an approval. The relay does not read
or interpret the reply body, decide whether a story or image is approved,
write production `automation_state`, send mail, publish, or commit. The
existing GitHub Actions workflows remain the final approval authority and
continue to validate the command through `scripts/approval_domain.py`.

M3B adds production-shaped Gmail ingress and application authentication, but
it remains local-only and `DRY_RUN`. Only `FakeGitHubDispatcher` exists.

## Ingress flow

```text
authenticated Pub/Sub push
  -> verify Google OIDC issuer + audience + service-account principal
  -> decode emailAddress + historyId
  -> verify configured mailbox
  -> walk Gmail history from the local cursor (all pages)
  -> fetch each message in metadata format (Subject and From only)
  -> exact sender allowlist check
  -> Subject stage classification
  -> sanitized message-key ledger record
  -> DRY_RUN stop (zero GitHub calls)
```

Authentication runs before envelope parsing or Gmail access. The raw
Authorization header and bearer token are never logged. The Gmail reader asks
for `gmail.readonly`, follows every `nextPageToken`, deterministically removes
duplicate message IDs, rejects repeated/cyclic tokens, and returns no batch if
any page fails. Message fetches use Gmail `format=metadata` with only `Subject`
and `From`; no body is fetched or retained.

## Sender and routing boundary

The real address extracted from Gmail's authenticated `From` metadata is
trimmed and case-folded, matching the existing A1 exact-address semantics.
Display names do not grant access. The address is used only for the transient
allowlist comparison and is then reduced to `from_allowlist_match`.

Both conditions are required for a dispatch candidate:

1. the actual normalized sender exactly matches `RELAY_ALLOWED_SENDERS`; and
2. the Subject matches exactly one configured stage substring.

The result is `GATE_A`, `DESIGN_SELECTION`, `UNRELATED`, or `AMBIGUOUS`. A
missing Subject, malformed From, unauthorized sender, unrelated Subject, or
ambiguous Subject never becomes a dispatch candidate. Subject matching is
wake-up routing only and does not parse or trust approval-command content.

## Event keys and cursor

Raw mailbox identity is never stored in a key. `mailbox_hash` is
`sha256(mailbox.strip().casefold())`.

- Notification dedupe key:
  `sha256("notification:" + mailbox_hash + ":" + historyId)`. It deduplicates
  Pub/Sub/Gmail notification delivery in the local ingress state.
- Message routing key:
  `sha256("message:" + mailbox_hash + ":" + gmail_message_id)`. It deduplicates
  each expanded message and is the future dispatch-dedupe identity.

Neither key includes the body, full Subject, or sender address. `historyId` is
never treated as a message ID.

The cursor begins at the configured `RELAY_GMAIL_INITIAL_HISTORY_ID`. Only a
fully successful history walk and all message metadata processing advance it
to the notification's `historyId`. A page failure, repeated token, message
fetch failure, or other incomplete processing releases the local lease without
advancing the cursor, so Pub/Sub redelivery can retry. An active walk
serializes later work for the same mailbox; the later push receives a retryable
response instead of being silently acknowledged.

## Authentication and configuration

`/relay` requires `Authorization: Bearer <OIDC token>`. Verification uses
Google's signature-verifying `google-auth` implementation in production and a
dependency-injected verifier in tests. The relay independently checks:

- configured issuer (`RELAY_OIDC_EXPECTED_ISSUER`);
- configured audience (`RELAY_OIDC_EXPECTED_AUDIENCE`);
- `email_verified is true`; and
- exact normalized principal membership in
  `RELAY_OIDC_EXPECTED_PRINCIPALS`.

Missing/malformed authorization, a verification error, or wrong
issuer/audience/principal is rejected with HTTP 401 before event processing.
Malformed Pub/Sub/Gmail envelopes and mailbox mismatch are rejected with HTTP
400. Incomplete Gmail processing returns HTTP 500 for bounded Pub/Sub retry.

Production construction also requires the mailbox, sender allowlist, both
Subject patterns, initial history ID, and Gmail OAuth client/refresh-token
configuration. Missing or malformed configuration fails closed. `RELAY_MODE`
defaults to `DRY_RUN`; M3B production wiring rejects any other value.

## Ledger and runtime limitation

The message ledger and notification cursor/dedupe state are thread-safe within
one process. They are not durable and are not cross-instance coordination.
Accordingly, any R1 Cloud DRY_RUN deployment using this M3B state must use:

- Cloud Run maximum instances = 1; and
- Gunicorn workers = 1 (the Dockerfile enforces this).

Threads may be greater than one because both local stores use locks and the
ingress state serializes history walks per mailbox. A restart loses cursor and
dedupe progress; that is acceptable only while GitHub dispatch remains fake.
A durable cross-instance ledger/cursor is required before R2.

The sanitized message ledger persists only `event_key`, `stage`, `workflow`,
attempt count/state, optional fake workflow run ID, and timestamps. It never
persists the Subject, body, sender address, mailbox address, token, or raw
envelope.

## Fixed targets and Human Gate

The only allowlisted workflow targets remain:

- `approval-check-phase2.yml` on `main`;
- `design-selection-check.yml` on `main`.

No real GitHub client, App, token, or `workflow_dispatch` call exists. Adding
one requires an explicit Human Gate. The current polling cron schedules remain
unchanged; removing them requires a later Human Gate after parallel validation
and recovery coverage. Real GitHub dispatch and cron removal are NOT authorized by this phase.

## Deployment progression

- **R0:** local synthetic core (M3A).
- **M3B:** real-shaped Gmail Pub/Sub ingress, Gmail history/message metadata
  retrieval, sender allowlist, and OIDC authentication; still local only and
  always `DRY_RUN` in production wiring.
- **R1:** Cloud `DRY_RUN`; no real GitHub dispatch.
- **R2:** real GitHub dispatch; NOT authorized by M3B.

R1 prerequisites are authenticated Pub/Sub push, real Gmail history/message
retrieval, sender allowlisting, a verified container build/runtime, configured
watch baseline history ID, and enforcement of the single-instance/single-worker
limitation while the in-memory state remains.

Before R2, all of the following require separate design/review and approval:

- real GitHub App credentials and adapter;
- sender allowlist revalidation at the dispatcher boundary;
- durable cross-instance cursor and dispatch ledger;
- `UNKNOWN_OUTCOME` reconciliation;
- quota and circuit-breaker policy; and
- catch-up/backfill for stale history or restart gaps.

R1 readiness must not be claimed until every R1 prerequisite, including an
actual container build/runtime test, has passed.

## Container and rollback

Build only from the relay directory, which excludes repository secrets,
`automation_state`, and image assets:

```text
docker build -f cloud/approval_relay/Dockerfile cloud/approval_relay
```

The image copies only the six Python source modules and requirements, runs as a
non-root user, and starts `create_app_from_env()` with one Gunicorn worker.

Rollback for R1 is disabling relay subscription traffic or the service. The
existing polling workflows remain the system of record and are unchanged.
