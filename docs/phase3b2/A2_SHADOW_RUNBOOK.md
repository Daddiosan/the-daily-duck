# Phase 3B-2 A2 Shadow Sibling Service Runbook

This runbook describes a future, separately approved deployment. Running
the service locally, or running its test suite, does not create a Gmail
subscription, a Google Cloud resource, an OAuth credential, or any GitHub
event.

No secret values appear in this document.

## Architecture

```
Gmail (Daily Duck mailbox)
  -> existing Gmail watch (owned by A1 only)
  -> existing Pub/Sub topic
       +-- existing push subscription -> A1 (cloud/approval_receiver)
       +-- NEW push subscription      -> A2 (cloud/approval_dispatcher)
                                            -> Gmail API read (A2's own reader)
                                            -> scripts.approval_domain /
                                               scripts.a2_dispatch classification
                                            -> sanitized shadow record
                                               (Firestore, Phase C shadow)
```

A2 is an independent sibling of A1, not its downstream. A1 never calls A2,
never knows A2 exists, and is never modified to support A2 (see A1/A2
Separation below). Both services independently receive the same Gmail push
notification because Pub/Sub delivers to every subscription on a topic, not
just one; adding A2's subscription requires no change to the existing Gmail
watch or to the topic's publisher IAM (`gmail-api-push@system.gserviceaccount.com`
publishing to the topic is unaffected by how many subscriptions read from
it).

## A1 / A2 Separation

- A2 lives entirely in `cloud/approval_dispatcher/`, never in
  `cloud/approval_receiver/`.
- A2 never imports `cloud.approval_receiver.*`. It implements its own,
  independent, minimal Gmail reader (`gmail_reader.py`) rather than reusing
  or copying A1's `gmail_client.py` -- see the Phase B.7 design review for
  why: importing A1's implementation would create a hidden deployment
  coupling in the opposite direction from what Phase B.6 rejected for A1.
  Duplicating a thin, rarely-changing Gmail API wrapper was judged an
  acceptable, bounded cost; duplicating approval-command parsing was not,
  and does not happen anywhere in A2 -- all classification goes through
  `scripts/approval_domain.py` and `scripts/a2_dispatch.py` unchanged.
- `tests/test_approval_receiver_contract.py` (A1's own governance contract)
  and `tests/test_approval_dispatcher_contract.py` (A2's) are independent
  files, each protecting its own service's boundary. Neither imports the
  other.

## Shared Pub/Sub Topic, Separate Subscription

A2 requires a new Pub/Sub push subscription on A1's existing Gmail topic,
targeting A2's own Cloud Run URL. This is additive: it does not require
recreating the Gmail watch and does not require changing the existing
publisher IAM grant. It does require its own new subscription-level IAM
grant (a push service account with Cloud Run Invoker on A2's service only,
separate from A1's existing push service account/Invoker grant).

**UNVERIFIED ASSUMPTION, PENDING LIVE CONFIRMATION**: that a second
subscription on an existing topic requires no change to the Gmail watch or
publisher IAM is standard, well-documented Pub/Sub behavior, but has not
been confirmed against this project's actual Google Cloud project. Confirm
this with a read-only `gcloud pubsub subscriptions list`/`describe` check
before creating the new subscription.

## Same OAuth Credential Values During Shadow

Per the Phase B.7 credential review: A1 and A2 use the SAME underlying
Gmail OAuth client and refresh token VALUES during Phase C shadow (both
need identical `gmail.readonly` read access to the same mailbox, so a
second, genuinely distinct OAuth client purchases no additional Gmail-side
data isolation). The values are stored in A2's OWN Secret Manager secret
(e.g. distinct from A1's `GMAIL_OAUTH_CLIENT_JSON`/`GMAIL_OAUTH_REFRESH_TOKEN`,
read by A2 as `A2_GMAIL_OAUTH_CLIENT_JSON`/`A2_GMAIL_OAUTH_REFRESH_TOKEN`),
with its own IAM grant scoped to A2's runtime service account only. This
gives independent revocation and independent audit trail without a second
OAuth production-promotion process.

> **HUMAN GATE: REQUIRED -- CREDENTIAL-SEPARATION RE-REVIEW BEFORE REAL
> DISPATCH.** The shared-value decision above is scoped explicitly to
> shadow mode, where A2 has no capability beyond reading Gmail and writing
> its own sanitized Firestore records. Before A2 gains real dispatch
> capability (the ability to actually call `workflow_dispatch`), this
> decision must be explicitly re-reviewed: a compromised A2 with real
> dispatch capability is a materially higher-value target than a
> compromised shadow-only A2, and a genuinely separate OAuth client/refresh
> token may then be warranted. Do not carry the shadow-mode credential
> decision into the real-dispatch phase without this re-review.

## No GitHub Dispatch, No Publishing, No Email Sending, No Polling

- A2 never calls the GitHub API and never performs a `workflow_dispatch` or
  `repository_dispatch` call in this phase. `main.py` constructs
  `scripts.a2_dispatch.FakeDispatchAdapter` (which never performs network
  I/O) as the dispatch adapter passed into `process_gmail_event`, never
  `GitHubAppDispatchAdapter` (the production placeholder, which itself
  still only raises `NotImplementedError` and is reserved for the
  real-dispatch phase). See
  `tests/test_approval_dispatcher_contract.py::test_dispatch_adapter_used_is_the_shadow_only_fake`.
- A2 never sends email (no `smtplib`, no Gmail `messages().send`).
- A2 never publishes the website or posts to X.
- A2 performs no periodic polling of any kind. It reacts only to Pub/Sub
  push notifications. It does not own the Gmail watch and therefore needs
  no watch-renewal schedule of its own (A1 alone renews the shared watch).
  A low-frequency (roughly daily, not 15-minute) catch-up/reconciliation
  check is a candidate future addition (mirroring the ~1x/day
  reconciliation design from Phase B.5), not a polling mechanism, and is
  not implemented in this phase.

## Firestore Collections (Phase C shadow only)

Same Google Cloud project and same Firestore database as A1 (see Phase
B.7's G1/F1 recommendation); separate collections:

- `a2_cursor` -- a single document holding A2's own independent Gmail
  history-walk cursor. Independent of A1's own cursor.
- `a2_shadow_observations` -- keyed by `observation_id`
  (`sha256(sha256(mailbox_identity):gmail_message_id)`, matching A1's own
  construction so both derive the same id for the same message
  independently). Serves as both the message-level dedupe gate
  `scripts.a2_dispatch.process_gmail_event` requires and the persistence
  target for the sanitized shadow schema below.
- `a2_transition_ledger` -- keyed by `transition_key`. Stores only a
  minimal reserved/confirmed concept, NOT the full five-state production
  dispatch model (`PENDING`/`DISPATCH_ATTEMPTED`/`CONFIRMED`/
  `UNKNOWN_OUTCOME`/`FAILED_FINAL`) designed in Phase B for real dispatch.
  Shadow mode never reaches `UNKNOWN_OUTCOME`/`FAILED_FINAL` in practice
  because the shadow dispatch adapter never fails; building out the full
  state model now, before any real dispatch exists to need it, would be
  premature.

No production-dispatch state (real GitHub run correlation, ambiguous-outcome
reconciliation) is built in this phase.

## Shadow Record Schema

Persisted fields, exactly:

```
observation_id, stage, issue_date, normalized_command, idempotency_key,
transition_key, classification, timestamp, source_type="GMAIL_PUSH"
```

`normalized_command` values are fully enumerable canonical strings only
(`SELECT_STORY:1`..`SELECT_STORY:5`, `SELECT_DESIGN:1:1`..`SELECT_DESIGN:3:3`,
`NEXT_3`) -- never free-form text, never the original wire reply.

Never persisted: full email body, quoted reply history, full subject line,
sender email address, or any credential/token.

## Retry Behavior

A2's own outbound calls are limited to Gmail API reads and Firestore
read/writes -- there is no outbound GitHub call to classify as
retryable/non-retryable/ambiguous in this phase (that design, from Phase B,
applies only once real dispatch exists). This section previously stated a
Pub/Sub `maxDeliveryAttempts` target of 3, conflating two genuinely
different mechanisms; the corrected distinction follows.

### A. Transport delivery (Pub/Sub, implemented in this phase)

- Terminal classification outcomes (valid, invalid, stale, unrelated,
  duplicate, benign concurrent cursor advance) -> HTTP 200 (ack). Pub/Sub
  does not redeliver.
- Rate-limit and other short-lived transient Gmail/Firestore failures, and
  unresolved cursor CAS concurrency conflicts -> HTTP 500 (non-2xx).
  Pub/Sub redelivers, bounded by the subscription's `maxDeliveryAttempts`.
  Pub/Sub's supported range for this setting is 5-100, and delivery-attempt
  enforcement is itself best-effort (Pub/Sub may deliver a small number of
  extra attempts beyond the configured value). If this architecture uses
  dead lettering for shadow, configure `maxDeliveryAttempts` at 5 (the
  protocol minimum) unless a later Human Gate chooses a higher value.
  Exceeding it routes to the dead-letter topic.
- Permanent Gmail authentication/configuration failures -> HTTP 200 (ack),
  to avoid an infinite Pub/Sub redelivery loop for a failure retrying
  cannot fix, while the failure is still logged under `error_category` for
  a human/reconciliation to notice.
- A long-duration Gmail quota condition (e.g. 403 `dailyLimitExceeded`) is
  distinct from both of the above: it is not acked away like a permanent
  failure (the quota does eventually clear, and acking it would silently
  stop processing unprocessed mail), and it is not assumed safe to retry
  immediately like an ordinary rate limit either. It maps to HTTP 500 like
  an ordinary temporary failure, so Pub/Sub redelivery still applies
  (bounded by `maxDeliveryAttempts`/dead-letter as above), but it is logged
  under a distinct `error_category` so it is observable and never confused
  with an ordinary short-lived rate limit or a permission failure. The
  cursor is never advanced past a batch that raised this condition.
- A 403 with an unrecognized or unreadable (malformed/non-JSON) structured
  reason is never silently treated as a known permanent permission
  failure. It is classified into its own distinct, observable
  `error_category` and follows the same HTTP 500/redelivery path as the
  quota condition above, rather than being guessed at from message text.

### B. Business retry budget (NOT implemented in this phase)

Future real Daily Duck operation (once real GitHub dispatch exists) is
expected to define a business retry budget of first attempt + at most 3
retries = at most 4 business attempts. This is a distinct concept from A's
transport-level `maxDeliveryAttempts` and is NOT implemented by it:

- A durable, logical per-transition attempt counter would be required to
  implement a business retry budget correctly (Pub/Sub's own delivery
  count is a transport-layer counter of *push attempts*, not of *logical
  dispatch attempts*, and is not durably exposed to application logic
  across redeliveries in a way that alone is sufficient for this).
- This ledger does not exist yet and is explicitly out of scope for shadow
  hardening -- it is future Thin Relay / real-dispatch work, not something
  this phase implements.
- `UNKNOWN_OUTCOME` for a future real GitHub dispatch call (did the
  `workflow_dispatch` actually happen before the failure?) is also not
  solved by Pub/Sub redelivery alone -- redelivering a request whose
  outcome is unknown risks a duplicate dispatch, which is exactly why the
  Phase B five-state `TransitionLedger` design (see Firestore Collections,
  above) exists as a separate, not-yet-built piece of work.
- Nothing in this runbook authorizes real GitHub dispatch. Shadow mode
  remains zero-GitHub-call, as stated throughout this document.

## Data Retention Restrictions

Plaintext (email body, subject, sender address) exists only transiently, in
process memory, for the duration of one classification call. It is never
written to Firestore and never logged. See
`tests/test_approval_dispatcher.py`'s `SanitizationTests` for the executable
proof.

## Logging Restrictions

Strict allowlist only:
`observation_id, stage, classification, issue_date, transition_key_prefix,
result, error_category, from_allowlist_match, recovery_path, processed`.

Never logged: full email body, full subject, sender email address, OAuth
token, refresh token, GitHub token, private key, raw `Authorization` header.

## Deployment Human Gates

> **HUMAN GATE: REQUIRED for every item below.** None of these have been
> performed. This runbook documents the plan only.

1. Confirm A1's own cloud deployment (per `docs/phase3b2/A1_RUNBOOK.md`) is
   complete -- A2 cannot observe real Gmail-derived events before A1's
   Gmail watch and Pub/Sub topic exist for real.
2. Create A2's Secret Manager secret holding the shared-value Gmail OAuth
   client/refresh token (see Same OAuth Credential Values, above), with IAM
   scoped to A2's runtime service account only.
3. Build and push the A2 container image
   (`docker build -f cloud/approval_dispatcher/Dockerfile .`, repository
   root as build context).
4. Deploy A2 to Cloud Run, privately, with no unauthenticated invocation,
   in the same Google Cloud project as A1.
5. Create the new Pub/Sub push subscription on the existing Gmail topic,
   targeting A2's Cloud Run URL, with its own push service account granted
   Cloud Run Invoker on A2's service only.
6. Configure the subscription's retry policy (`maxDeliveryAttempts`: 5,
   Pub/Sub's protocol minimum, per the corrected Retry Behavior section
   above -- Pub/Sub only supports 5-100, not 3) and dead-letter topic.
7. Read-only verification: confirm an unauthenticated or wrong-audience
   push to A2's `/pubsub` endpoint is rejected.
8. **PRODUCTION_STATE_GAP -- MUST BE RESOLVED BEFORE THIS GATE**:
   `main.py`'s `ProductionStateReader` has no real implementation. A real
   deployment needs a way to learn the current `automation_state/*.json`
   content (active issue date, current state) without a git checkout --
   options include a read-only GitHub Contents API read, or another
   sync mechanism -- and this must be designed and reviewed before A2 is
   deployed for real. Deploying with only `FakeProductionStateReader()`
   (which always returns an empty snapshot) is a safe fail-closed default
   (every message is rejected as missing an active issue) but is not a
   working shadow implementation.

> **HUMAN GATE: REQUIRED -- REAL DISPATCH.** Everything in this runbook
> describes shadow mode only: zero GitHub dispatch, zero publishing, zero
> email sending. Granting A2 (or any successor) the ability to actually
> call `workflow_dispatch` is a separate, explicitly human-gated decision,
> requiring at minimum: the GitHub App/PAT decision from the Phase B design
> review, the credential-separation re-review above, the UNKNOWN_OUTCOME
> reconciliation design from Phase B.5, and a Firestore-backed
> `TransitionLedger` implementing the full five-state production model --
> none of which exist yet.

## Rollback

- Disable or delete the A2 Pub/Sub push subscription. A1's own
  subscription and the Gmail watch are unaffected (they are independent
  resources).
- Route A2's Cloud Run service traffic to zero or delete it.
- A2 has no production mutation path in shadow mode, so rollback requires
  no production-state repair -- A2's own Firestore collections
  (`a2_cursor`, `a2_shadow_observations`, `a2_transition_ledger`) can be
  deleted independently of anything A1 or the legacy poller depend on.
