# Thin Approval Relay Runbook (Phase 3B-2 / M3C)

Component: `cloud/approval_relay/`

## Trust boundary

A relay event is a wake-up signal, never an approval. The relay does not
interpret the reply body, decide whether content is approved, write production
`automation_state`, publish, send mail, or commit. The downstream workflows
remain the approval authority and continue to validate commands through
`scripts/approval_domain.py`.

Production wiring is locked to `DRY_RUN` and constructs only
`FakeGitHubDispatcher`. No real GitHub App, token, client, or dispatch call
exists.

## Authenticated ingress

```text
authenticated Pub/Sub push
  -> verify Google OIDC issuer, audience, email_verified, exact principal
  -> decode string emailAddress + decimal-string or non-negative integer historyId
  -> verify configured mailbox
  -> load or bootstrap durable mailbox cursor
  -> walk all Gmail history pages
  -> fetch Subject and From metadata only
  -> exact sender allowlist check plus Subject classification
  -> transactional durable message-event reservation
  -> transactional cursor advance
  -> DRY_RUN stop (zero GitHub calls)
```

Authentication runs before envelope decoding or Gmail access. Bearer tokens
and raw Authorization headers are never logged. At the notification wire
boundary, `historyId` accepts a nonempty ASCII decimal string or a non-negative
integer and canonicalizes either to a string. Floats, booleans, null, arrays,
objects, negative integers, empty strings, whitespace, and non-decimal strings
are rejected before Gmail history access. Persisted history IDs remain strict
strings.

The Gmail reader requests `gmail.readonly`, exhausts pagination, deduplicates
message IDs in first-seen order, rejects repeated/cyclic page tokens, and never
returns a partial batch after a later-page failure. Message fetch uses
`format=metadata` and requests only `Subject` and `From`; no body is fetched.

The real sender address is parsed from authenticated Gmail metadata using the
existing exact, trimmed, case-insensitive email semantics. It exists only long
enough for the allowlist comparison and is reduced to
`from_allowlist_match`. Sender plus exactly one recognized Subject pattern are
both required for a candidate. The reply body is never parsed.

## Firestore layout

The Relay defines three fixed, application-controlled collections. The first
two are used by normal ingress; the third is used only by the R1.3 watch
renewal source implementation and is not evidence of deployment:

- `relay_cursor`: document ID is the one-way mailbox hash. Fields are exactly
  `mailbox_hash`, `history_id`, and `updated_at`.
- `relay_events`: document ID is the deterministic message event key. Fields
  are exactly `event_key`, `stage`, `workflow`, `attempt_count`, `state`,
  nullable `workflow_run_id`, `created_at`, and `updated_at`.
- `relay_watch_state`: document ID is the one-way mailbox hash. Fields are
  exactly `expiration`, `history_id`, `mailbox_hash`, and `updated_at`.

Collection names are constants and cannot be supplied by a caller or changed
through environment configuration. None of these collections stores a raw mailbox,
sender, Subject, body, OAuth/OIDC token, Authorization header, or GitHub
credential.

Every cursor compare-and-update, event create-if-absent, dispatch-attempt
reservation, and state transition uses the Firestore SDK's transactional
decorator with reads performed inside the transaction. There is no
read-in-Python followed by an unconditional write masquerading as CAS.
Local tests use a controlled transactional fake, including deterministic
contention/retry behavior. They verify the requested SDK interaction contract;
they do not claim live Firestore compatibility or emulator coverage.

The ledger state machine remains:

`RECEIVED`, `DISPATCH_ATTEMPTING`, `DISPATCH_CONFIRMED`, `SAFE_TO_RETRY`,
`UNKNOWN_OUTCOME`, `FAILED_FINAL`.

Only `RECEIVED` or `SAFE_TO_RETRY` can atomically become
`DISPATCH_ATTEMPTING`, so concurrent instances can grant at most one dispatch
attempt for an event. Maximum business attempts remain four. `UNKNOWN_OUTCOME`,
`DISPATCH_CONFIRMED`, and `FAILED_FINAL` block automatic future attempts.
`UNKNOWN_OUTCOME` reconciliation is not implemented.

## Cursor bootstrap and restart behavior

`RELAY_GMAIL_INITIAL_HISTORY_ID` is Human-approved bootstrap input, not the
ongoing source of truth. On first use for a mailbox:

1. read the hashed-mailbox cursor document;
2. if absent, transactionally create it from the environment floor;
3. if another instance wins initialization, re-read and use the winner.

An existing cursor always wins and is never overwritten by a changed
environment floor. Scale-to-zero, cold start, a new container, and deployment
restart therefore reload cursor and message dedupe/ledger state from Firestore
instead of reverting to process memory.

The cursor advances only after the full history walk and every message has
been processed. Cursor CAS failure is acknowledged only when a re-read proves
another instance already advanced to the same or a newer history ID; otherwise
the request returns a retryable failure. Firestore unavailability, Gmail
failure, or ledger failure returns a retryable transport response and never
reports successful history completion.

## Dedupe decision

No notification-dedupe collection is used. The notification key
`sha256("notification:" + mailbox_hash + ":" + historyId)` is diagnostic only.
It is not a source of truth.

The durable cursor makes an already-completed notification a no-op. If two
instances walk the same range concurrently, the durable message event key
`sha256("message:" + mailbox_hash + ":" + gmail_message_id)` and transactional
ledger prevent duplicate dispatch permission. A separate notification record
would add another state machine without improving correctness.

Neither key includes sender, Subject, or body, and `historyId` is never treated
as a message ID.

## Production configuration and IAM intent

`create_app_from_env()` requires the mailbox, sender allowlist, Subject
patterns, OIDC issuer/audience/principals, Gmail OAuth configuration, initial
history floor, and `RELAY_FIRESTORE_PROJECT`. Missing configuration fails
closed. Production wiring builds one `FirestoreRelayStorage` used as both
cursor store and relay ledger. In-memory stores remain available only to local
unit tests.

Minimum intended future IAM for the relay runtime service account is:

- only the Firestore document access required for `relay_cursor` and
  `relay_events`; and
- access only to the Gmail OAuth secrets required by this service.

IAM is not configured by M3C. Pub/Sub invocation permission and OIDC principal
configuration remain deployment-time R1 work.

## R1 observation semantics and limits

R1 no longer depends on process lifetime for Gmail cursor progress, message
dedupe, or the relay event ledger. This makes scale-to-zero and restarts
meaningful for an unattended shadow.

This does not make Gmail notification delivery lossless. A missing push,
expired Gmail history range, or gap before the configured bootstrap floor is
not recovered here. Catch-up/backfill and stale-history recovery remain
separate pre-R2/cutover work.

No application sleep/retry loop exists. Firestore transaction retries are
owned by the official SDK; Pub/Sub transport retries remain bounded by the
subscription configuration.

## Deployment progression and Human Gates

- **R0 / M3A:** local synthetic relay core.
- **M3B:** real-shaped Gmail ingress, sender gating, and OIDC authentication.
- **M3C:** durable Firestore cursor and message ledger; still local-only.
- **R1:** Cloud `DRY_RUN`, with zero real GitHub dispatch.
- **R2:** real GitHub dispatch; NOT authorized by this phase.

Before R1: build and runtime-test the container, provision the two Firestore
collections through ordinary first writes, configure least-privilege IAM and
secrets, configure authenticated Pub/Sub push, and perform an observed DRY_RUN
smoke test. No real collections or cloud resources are created by M3C.

Before R2: add and review a real GitHub App adapter, revalidate sender
authorization at the dispatch boundary, design `UNKNOWN_OUTCOME`
reconciliation, set quota/circuit-breaker policy, and implement catch-up/
backfill. Each requires a Human Gate.

The existing polling cron schedules remain unchanged. Real GitHub dispatch and
cron removal are NOT authorized by this phase.

## Container and rollback

Build from the relay-only context:

```text
docker build -f cloud/approval_relay/Dockerfile cloud/approval_relay
```

The image copies only relay source and requirements, including `storage.py`;
it does not copy the repository root, `automation_state`, images, or secrets.
It runs non-root with one Gunicorn worker. Firestore now provides cross-instance
correctness, while one worker remains a conservative runtime setting.

R1 rollback is disabling relay subscription traffic or the service. Existing
polling workflows remain the system of record.
