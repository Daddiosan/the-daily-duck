# Phase 3B-2A1 Google Event Receiver Runbook

This runbook describes a future, separately approved deployment. Running the
receiver locally does not create a Gmail watch, Google Cloud resource, OAuth
credential, GitHub event, or production transition.

## A1 boundary

A1 accepts an authenticated Gmail change notification, fetches exact messages
with the read-only Gmail API, and stores sanitized observations. It does not
parse approval commands, import `approval_domain`, contact GitHub, write Daily
Duck production state, send email, commit, push, publish the website, or post to
X.

Firestore writes in A1 are transport-only cursor, watch, and observation data.
They are not Daily Duck production approval state.

## Prerequisites

- A dedicated Google Cloud project with billing enabled.
- Human approval for every cloud mutation described below.
- Google Cloud CLI authenticated to the intended project.
- An operator able to configure the monitored Gmail account.
- Artifact Registry, Cloud Build, Cloud Run, Pub/Sub, Firestore, Secret
  Manager, Cloud Scheduler, Gmail API, and IAM APIs.

Use one region consistently where the products permit it. Configure a billing
budget and alerts before deployment. Free tiers are quotas rather than a hard
spending cap; logging, image retention, network transfer, and accidental high
traffic can still create charges.

## OAuth consent and refresh token

> **Critical: set the OAuth consent screen to `External` and publishing status
> `In production` before issuing the operational refresh token.**

An External app left in `Testing` can issue refresh tokens that expire after
seven days. Do not treat a Testing refresh token as an operational credential.

1. Configure the OAuth consent screen as External.
2. Move the app to In production.
3. Create a Desktop OAuth client.
4. Request only
   `https://www.googleapis.com/auth/gmail.readonly` with offline access.
5. Complete the interactive grant as the monitored Gmail mailbox owner.
6. Store the complete Desktop client JSON in Secret Manager secret
   `gmail-oauth-client`.
7. Store the refresh token separately in
   `gmail-oauth-refresh-token`.
8. Never place either value in a file, image, log, shell history, GitHub
   variable, or repository secret.

At deployment, expose the secret values to the container as
`GMAIL_OAUTH_CLIENT_JSON` and `GMAIL_OAUTH_REFRESH_TOKEN`. Grant the Cloud Run
runtime service account access only to those named secrets.

If the mailbox password changes and the refresh token stops working, revoke
the old OAuth grant, repeat the In-production offline grant, add a new Secret
Manager version, redeploy, and verify `users.getProfile` before re-enabling the
push subscription.

## Required non-secret configuration

The service fails closed if these values are absent or malformed:

| Variable | Purpose |
|---|---|
| `GMAIL_MAILBOX_IDENTITY` | Monitored mailbox |
| `GMAIL_ALLOWED_SENDERS` | Comma-separated exact sender allowlist |
| `GATE_A_SUBJECT_PATTERN` | Literal Gate A subject identity |
| `DESIGN_SUBJECT_PATTERN` | Literal Design subject identity |
| `GMAIL_PUBSUB_TOPIC` | `projects/PROJECT/topics/TOPIC` |
| `OIDC_EXPECTED_AUDIENCE` | Exact Cloud Run service/endpoint audience |
| `OIDC_EXPECTED_CALLERS` | Pub/Sub and Scheduler service-account emails |
| `FIRESTORE_CURSOR_COLLECTION` | Transport cursor collection |
| `FIRESTORE_CURSOR_DOCUMENT` | Single mailbox cursor document |
| `FIRESTORE_OBSERVATION_COLLECTION` | Sanitized observation collection |

Optional bounded values are `WATCH_RENEW_THRESHOLD_HOURS` (default 48),
`FULL_RESYNC_NEWER_THAN` (default `7d`), and
`FULL_RESYNC_MAX_MESSAGES` (default 100, maximum 500).

## Future infrastructure setup

Do not execute these steps without explicit infrastructure approval.

1. Enable the prerequisite APIs.
2. Create a regional Artifact Registry repository with a cleanup policy that
   retains the deployed image and a small rollback window while deleting old,
   untagged images.
3. Create Firestore Standard edition. Apply least-privilege access to the
   receiver's cursor and observation data where IAM permits.
4. Create separate service accounts:
   - Cloud Run runtime
   - Pub/Sub push caller
   - Cloud Scheduler maintenance caller
5. Create a Pub/Sub topic and push subscription. Configure retry and a dead
   letter topic/subscription.
6. Grant `serviceAccount:gmail-api-push@system.gserviceaccount.com` publisher
   access on the Gmail event topic.
7. Build the image from `cloud/approval_receiver/Dockerfile` and record its
   immutable digest.
8. Deploy Cloud Run privately, with no unauthenticated invocation, minimum
   instances zero, and the runtime service account.
9. Grant only the Pub/Sub push and Scheduler service accounts Cloud Run
   Invoker.
10. Configure Pub/Sub authenticated push to `/pubsub` using the push service
    account and the exact expected OIDC audience.
11. Configure a Cloud Scheduler hourly authenticated POST to `/maintenance`.
    The endpoint performs catch-up and renews the watch only when expiration is
    within 48 hours.
12. Configure monitoring for Cloud Run 5xx, Pub/Sub retry/DLQ depth, OAuth
    401/403, cursor age, observation latency, duplicate count, watch expiration,
    and renewal failure.

Cloud Run IAM is the primary authentication boundary. The application also
cryptographically verifies the Google-signed bearer token audience, verified
email claim, and exact service-account allowlist. No identity header supplied
by a caller is proof of identity.

## Gmail watch bootstrap

Bootstrap must be deliberate because Gmail sends an immediate notification
after a successful `users.watch` request.

1. Confirm the push endpoint rejects unauthenticated requests.
2. Confirm `users.getProfile` succeeds using `gmail.readonly`.
3. Start `users.watch` with the configured Pub/Sub topic and
   `labelIds: [INBOX]`.
4. Store the returned watch `historyId` and expiration only in the watch
   fields.
5. Do not copy the watch `historyId` into `processing_history_id`.
6. Let the immediate notification bootstrap through the bounded full-resync
   path when no processing cursor exists.
7. Verify that the bounded query contains configured `from:`, `subject:`, and
   `newer_than:7d` constraints and no hard-coded private address.

Watch renewal and the processing cursor are independent. Renewal must never
advance or reset the processing cursor.

## Verification commands

Exact project, service, region, and subscription names must be reviewed before
use. Representative read-only checks after an approved deployment are:

```text
gcloud run services describe SERVICE --region REGION
gcloud pubsub subscriptions describe SUBSCRIPTION
gcloud scheduler jobs describe MAINTENANCE_JOB --location REGION
gcloud secrets versions list gmail-oauth-client
gcloud secrets versions list gmail-oauth-refresh-token
```

Use an authenticated request from the approved caller to check `/health`, then
`/maintenance`. Never put bearer tokens in copied logs or reports. Inspect
Cloud Logging with field filters and confirm that raw sender, subject, message
body, client secret, and refresh token are absent.

## A1 live success criteria

- `users.watch` succeeds with `INBOX` and a separate expiration record.
- Unauthenticated or wrong-audience push is rejected.
- One real Gmail reply produces one sanitized observation with the exact Gmail
  API message ID, thread ID, internal date, history context, and hashed RFC
  Message-ID/body.
- A repeated notification for the same Gmail message produces no second
  observation and is acknowledged successfully.
- Notification-to-observation latency is measured without logging message
  content.
- Production state writes, git commits, pushes, downstream dispatches, email,
  website publishing, and X posting remain zero.
- Renewal below the 48-hour threshold changes watch fields without changing
  `processing_history_id`.
- Hourly catch-up advances the processing cursor only after every fetched
  message is durably observed.
- A forced stale-history 404 completes bounded full resync and records
  `FULL_RESYNC`.
- Structured logs contain result categories and identifiers only; no raw PII
  or credentials.

## Safe failure behavior

- Partial Gmail fetch: return non-2xx, retain the old cursor, retry; already
  inserted observations deduplicate on retry.
- Stale history 404: run bounded search, write sanitized observations, then CAS
  the cursor to the profile snapshot history ID.
- OAuth revoked: fail closed and alert; do not repeatedly issue new consent.
- Pub/Sub duplicate: acknowledge after identifying the existing Gmail message
  observation.
- Firestore/CAS failure: return non-2xx and do not claim cursor progress.
- Watch renewal failure: retain the current watch/cursor and alert.

## Rollback and revocation

1. Disable or detach the Pub/Sub push subscription.
2. Stop the Scheduler maintenance job.
3. Route Cloud Run to the previously recorded image digest or set service
   traffic to zero.
4. Do not delete Firestore cursor/observations until audit evidence is exported
   and retention approval is obtained.
5. Revoke the Gmail OAuth grant when abandoning the receiver.
6. Disable and then destroy the corresponding Secret Manager versions after
   confirming rollback.
7. Remove Gmail publisher and Cloud Run Invoker IAM bindings that are no
   longer required.
8. Apply the Artifact Registry cleanup policy; do not manually delete the only
   known-good rollback image.

Rollback does not require a production-state repair because A1 has no
production mutation path.

## Deployment Human Gate: real Firestore transaction isolation

The local test suite (`tests/test_approval_receiver_firestore.py`) validates
`FirestoreObservationStore`'s CAS/idempotency semantics only against the
project's own fake/in-memory Firestore client. It does not exercise a real
Firestore backend, and it does not prove transaction isolation under
concurrent writers.

Before A1 is considered live-successful, the deployment Human Gate must
explicitly verify and accept real Firestore concurrency/transaction behavior
under concurrent Cloud Run instances (for example: two or more instances
racing to process overlapping Gmail history ranges), confirming that the
cursor compare-and-set and observation insert-if-absent transactions
correctly serialize and deduplicate against a real Firestore project, not
just the local fake. This verification is separate from, and in addition to,
the local unit test suite passing.
