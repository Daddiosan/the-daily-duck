# Thin Approval Relay Runbook (Phase 3B-2 / M3C)

Component: `cloud/approval_relay/`

## Trust boundary

A relay event is a wake-up signal, never an approval. The relay does not
interpret the reply body, decide whether content is approved, write production
`automation_state`, publish, send mail, or commit. The downstream workflows
remain the approval authority and continue to validate commands through
`scripts/approval_domain.py`.

The deployed R1 service remains locked operationally to `DRY_RUN`. R2A adds a
local, reviewable GitHub App adapter and fail-closed `LIVE` wiring, but R2A
does not authorize GitHub App creation, secret/IAM changes, deployment, or
LIVE activation.

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
  -> DRY_RUN stop (zero GitHub calls), or atomically claim a LIVE attempt
  -> fixed authenticated GitHub workflow_dispatch in LIVE only
  -> transactional cursor advance only after no event requires redelivery
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
  nullable `workflow_run_id`, immutable `dispatch_eligible`, `created_at`, and
  `updated_at`. Legacy records without `dispatch_eligible` are read as
  ineligible.
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
`DISPATCH_ATTEMPTING`, and only when immutable `dispatch_eligible=true`, so
concurrent instances can grant at most one dispatch attempt for an event.
Maximum business attempts remain four. `UNKNOWN_OUTCOME`,
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
reached a state that does not require automatic redelivery. `SAFE_TO_RETRY`
and `ALREADY_IN_PROGRESS` block the entire batch's cursor advance and return a
controlled 503 so Pub/Sub redelivers. Already-confirmed events become terminal
no-ops during that redelivery. Cursor CAS failure is acknowledged only when a re-read proves
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
- access only to the separately named GitHub App private-key secret when LIVE
  deployment is approved.

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

## R2A GitHub App boundary

`cloud/approval_relay/github_app_dispatch.py` is the only module allowed to
perform outbound GitHub HTTP. It uses an RS256 GitHub App JWT to obtain a
short-lived installation token, caches that token under a process lock with a
five-minute refresh skew, and makes exactly one workflow-dispatch request per
relay delivery. There is no PAT or `GITHUB_TOKEN` fallback and no in-process
retry loop.

The owner (`Daddiosan`), repository (`the-daily-duck`), API host/version,
workflow filenames, and `main` ref are application constants. Callers cannot
supply workflow inputs or arbitrary GitHub targets. The fixed request sets
`return_run_details=true`, so a successful `200` can be durably correlated by
its returned workflow run ID. LIVE requires
`RELAY_GITHUB_APP_CLIENT_ID`, `RELAY_GITHUB_APP_INSTALLATION_ID`, and
`RELAY_GITHUB_APP_PRIVATE_KEY`; malformed or missing configuration fails
closed. Private keys, App JWTs, and installation tokens are never logged.

Successful dispatch stores GitHub's returned workflow run ID. A proven
pre-send failure or explicit rate-limit rejection is safe to retry. A timeout,
reset, 5xx, or malformed success response after the request may have reached
GitHub and is therefore `UNKNOWN_OUTCOME`; it is never automatically
redispatched. Existing polling remains the recovery authority while R2 is in
coexistence.

## R2A cutover and approval-token boundary

An event first reserved in DRY_RUN stores `dispatch_eligible=false`; one first
reserved in LIVE stores `true`. The value is immutable, duplicate reservation
cannot upgrade it, ambiguous events are always false, and legacy records
without the field are false. Deploying the R2A-compatible image in DRY_RUN
before a separately approved LIVE revision therefore cannot replay old
DRY_RUN observations.

The relay remains a wake-up service and does not validate approval commands or
approval tokens. Existing approval-email scripts generate an issue-bound
192-bit random token and put the raw value only in the reply-preserved Subject.
Tracked state stores only a domain-separated SHA-256 digest bound to stage,
issue date, and design batch. Existing approval checkers require the exact
sender, current digest, current issue/batch, and a valid command. A regenerated
design batch creates a new token. Complete token-bearing Subjects are neither
stored in tracked state nor printed to logs.

## Deployment progression and Human Gates

- **R0 / M3A:** local synthetic relay core.
- **M3B:** real-shaped Gmail ingress, sender gating, and OIDC authentication.
- **M3C:** durable Firestore cursor and message ledger; still local-only.
- **R1:** Cloud `DRY_RUN`, with zero real GitHub dispatch.
- **R2A:** local adapter, retry/cursor safety, cutover guard, approval-token
  authorization, tests, and this runbook. Code review only.
- **R2:** separately gated GitHub App resources, DRY_RUN deployment, LIVE
  activation, and natural end-to-end validation.

Before R1: build and runtime-test the container, provision the two Firestore
collections through ordinary first writes, configure least-privilege IAM and
secrets, configure authenticated Pub/Sub push, and perform an observed DRY_RUN
smoke test. No real collections or cloud resources are created by M3C.

Before LIVE R2: review the R2A adapter and sender authorization, provision the
repository-scoped GitHub App and private-key secret, verify DRY_RUN with the new
image, approve an `UNKNOWN_OUTCOME` operator procedure, set quota policy, and
approve any catch-up/backfill. Each requires a Human Gate.

The existing polling cron schedules remain unchanged. Real GitHub dispatch and
cron removal are not authorized by R2A. LIVE activation is not authorized by
R2A.

Merging R2A changes the scheduled approval checkers even while the Cloud Run
relay remains in DRY_RUN. Merge only at a clean issue boundary with no
outstanding tokenless Gate A or design-selection email. An in-flight legacy
reply intentionally fails closed; migrating one requires a separate Human
Gate. Existing polling schedules stay enabled throughout.

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
