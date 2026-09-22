# R1 Gmail Watch Renewal Runbook

Status: source implementation and local tests only. Nothing in this document
authorizes deployment, a live `users.watch` call, IAM changes, or Cloud
Scheduler creation.

## Architecture

The existing Relay service contains an authenticated `POST /renew-watch`
endpoint. A future dedicated Cloud Scheduler identity will invoke it with a
Google OIDC token. After application-level issuer, audience, verified-email,
and exact-principal checks, one request-local Gmail client makes exactly one
`users.watch` call. A successful result is written transactionally to the
dedicated `relay_watch_state` collection.

```text
Cloud Scheduler (future, daily)
  -> Cloud Run IAM authentication
  -> POST /renew-watch
  -> application OIDC issuer/audience/principal verification
  -> request-local Gmail client using gmail.readonly OAuth
  -> users.watch(existing topic, INBOX, include)
  -> monotonic Firestore watch-state transaction
```

The renewal principal is configured separately from the Pub/Sub push
principal. Deployment must grant only the dedicated Scheduler service account
permission to invoke Cloud Run and must put that exact identity in
`RELAY_RENEWAL_OIDC_EXPECTED_PRINCIPALS`. The application audience is supplied
by `RELAY_RENEWAL_OIDC_EXPECTED_AUDIENCE`. The Pub/Sub principal is not
automatically accepted.

## Gmail contract and credentials

Renewal reuses the Relay's existing OAuth parsing and the exact
`gmail.readonly` scope. It does not request another scope. Every endpoint
invocation constructs its own Gmail service/transport; it does not share the
thread-unsafe Gmail transport used by another request.

The topic comes from `RELAY_GMAIL_WATCH_TOPIC` and, at deployment review, must
be checked against the existing R1 Gmail events topic. The call uses:

- `userId=me`
- `labelIds=[INBOX]`
- `labelFilterBehavior=include` (the Gmail API wire value for INCLUDE)

There is one Gmail call per endpoint invocation and no in-application retry
loop. Scheduler retries provide the outer retry boundary.

## Watch state and cursor safety

The document ID is the SHA-256 mailbox hash. The exact persisted schema is:

| Field | Representation | Rule |
| --- | --- | --- |
| `expiration` | integer epoch milliseconds | positive integer |
| `history_id` | decimal string | non-negative, ASCII decimal |
| `mailbox_hash` | lowercase SHA-256 hex | never the raw mailbox |
| `updated_at` | nonempty UTC timestamp string | write time |

No raw mailbox, sender, Subject, body, OAuth token, client secret,
Authorization header, or Gmail error payload is stored or logged.

The returned watch `historyId` and `relay_cursor.history_id` have different
responsibilities. Renewal persists the returned value only in watch state. It
never initializes, resets, or advances `relay_cursor`, so renewing a watch
cannot skip the history interval between the processing cursor and the new
watch value.

Repeated renewal is idempotent. The Firestore transaction compares
`(expiration, numeric history_id)` and writes only a strictly newer successful
result. If two calls overlap and the later watch completes first, an older
result that completes afterward cannot overwrite it. A Gmail failure or
malformed response performs no state write.

## HTTP and retry behavior

- `200`: Gmail renewal succeeded. `RENEWED_STATE_RETAINED` is also success; a
  concurrent or repeated result was not newer than the stored state.
- `401`: missing, invalid, or unauthorized OIDC identity. Do not retry until
  authentication is corrected.
- `400`: Gmail returned 400/401/403, or its success response was malformed.
  Treat as configuration/authorization failure and require investigation.
- `503`: Gmail 429/500/502/503/504 or a temporary network/connectivity failure.
  A later Scheduler retry is safe.
- `500`: unclassified Gmail or storage failure. Inspect sanitized category
  logs; retry remains safe, but investigate repeated failures.

Responses and logs contain only fixed status/reason categories. They do not
echo provider payloads, tokens, mailbox identity, topic, history ID, or
expiration.

## Future Cloud Scheduler plan — document only

This plan is not deployed by R1.3:

| Setting | Planned value |
| --- | --- |
| Job name | `daily-duck-gmail-watch-renewal` |
| Region | `asia-northeast1` |
| Schedule | `17 03 * * *` (once daily) |
| Time zone | `Asia/Tokyo` |
| HTTP method | `POST` |
| Target | exact deployed Relay URL plus `/renew-watch` |
| OIDC service account | dedicated renewal-only service account |
| OIDC audience | exact reviewed Cloud Run service audience |
| Success | HTTP 200 |
| Retry | bounded exponential retry for 5xx/429-class outcomes; no retry for 4xx |

Daily renewal is comfortably inside Gmail's normal watch expiration window.
Before deployment, a Human Gate must review the concrete service URL,
audience, service-account identity, existing topic, retry limits, and schedule.
No secret is placed in the Scheduler configuration.

## Operations, emergency action, and rollback

Manual emergency renewal uses the same authenticated endpoint and dedicated
principal after a Human Gate; operators must not call Gmail directly with
printed credentials. A successful call may safely be repeated. Persistent
4xx responses require correcting identity, OAuth authorization, or topic
configuration before retrying. Persistent 5xx responses require reviewing
sanitized Cloud Run logs and Firestore availability.

To disable renewal after a future deployment, pause the Scheduler job. To
roll back the application, route Cloud Run traffic to the prior reviewed R1
revision. Do not delete watch state or change `relay_cursor`; the existing R1
push path and legacy 15-minute polling remain untouched. Any deployment,
Scheduler/IAM creation, live watch renewal, or rollback requires a separate
Human Gate.
