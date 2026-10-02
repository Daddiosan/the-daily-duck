# Gmail Watch Renewal and OAuth Canary Runbook

Status: deployed and recovered. The first automatic execution after OAuth
recovery passed on 2026-10-02. This document does not authorize a live
`users.watch`, OAuth, secret, IAM, Scheduler, or Cloud Run mutation.

## Production source of truth

| Setting | Production value |
| --- | --- |
| Project | `the-daily-duck` (`424584128509`) |
| Region | `asia-northeast1` |
| Cloud Run service | `daily-duck-approval-relay` |
| Renewal endpoint | `POST /renew-watch` |
| Scheduler job | `daily-duck-watch-renewal` |
| Schedule | `17 3 * * *`, `Asia/Tokyo` |
| Scheduler identity | `daily-duck-watch-renewal@the-daily-duck.iam.gserviceaccount.com` |
| Gmail topic | `projects/the-daily-duck/topics/daily-duck-gmail-events` |

Latest accepted automatic execution:

| Evidence | Value |
| --- | --- |
| Scheduled time | 2026-10-02 03:17 JST |
| Scheduler result | HTTP 200 |
| Cloud Run `/renew-watch` | HTTP 200 |
| Expiration before | 2026-10-08 21:33:04.807 JST |
| Expiration after | 2026-10-09 03:17:05.559 JST |
| Processing cursor before / after | `37076` / `37076` |

The Scheduler remains `ENABLED`. Renewal changed only `relay_watch_state` and
did not reset or advance `relay_cursor`.

The older planned name `daily-duck-gmail-watch-renewal` was never the deployed
resource name. Operators must use `daily-duck-watch-renewal`.

```text
Cloud Scheduler (daily, deployed)
  -> Cloud Run IAM and application OIDC authentication
  -> POST /renew-watch
  -> users.watch(existing topic, INBOX, include)
  -> monotonic relay_watch_state transaction
```

The dedicated Scheduler principal is separate from the Pub/Sub push principal.
The application verifies issuer, audience, `email_verified`, and the exact
principal before Gmail access.

## OAuth credential contract

The Relay uses only `gmail.readonly`. Production injects Secret Manager values
as environment variables, not mounted files:

- `RELAY_GMAIL_OAUTH_CLIENT_JSON=relay-gmail-oauth-client-json:<version>`
- `RELAY_GMAIL_OAUTH_REFRESH_TOKEN=relay-gmail-oauth-refresh-token:<version>`

Production recovery must pin explicit versions rather than continuing to rely
on `latest`. Never print or place the client secret or refresh token in a file,
log, command history, repository secret, or issue.

The OAuth app must be `External / In production` before an operational offline
refresh token is granted. Publishing status is a Human Gate and must be checked
in Google Auth Platform; do not infer it from a token error.

## OAuth canary

The Relay exposes authenticated `POST /oauth-canary`. It calls only
`users.getProfile(userId=me)`, validates the metadata shape, discards the
response, and logs only a fixed category. It never reads message bodies or
message metadata.

Recommended Scheduler configuration after Human Gate:

| Setting | Value |
| --- | --- |
| Job | `daily-duck-oauth-canary` |
| Schedule | `43 */6 * * *`, `Asia/Tokyo` |
| Target | Relay URL plus `/oauth-canary` |
| OIDC identity/audience | same reviewed renewal identity and service audience |
| Success | HTTP 200 |

Alert on any non-2xx. Fixed categories are `AUTH_FAILURE`, `TIMEOUT`,
`RATE_LIMITED`, `RETRYABLE`, `MALFORMED_RESPONSE`, and `UNKNOWN`.

## Watch state and cursor safety

`relay_watch_state` stores only `expiration`, `history_id`, `mailbox_hash`, and
`updated_at`. `users.watch` renewal never initializes, resets, or advances
`relay_cursor`; the returned history ID and processing cursor have different
roles. The transaction accepts only a strictly newer `(expiration, history_id)`.

No mailbox, sender, Subject, body, OAuth token, client secret, Authorization
header, or Gmail provider payload is stored or logged.

## HTTP behavior

- `200`: renewal/canary succeeded.
- `400`: non-retryable Gmail renewal rejection or malformed watch response.
- `401`: OIDC authentication rejected.
- `503`: sanitized retryable Gmail or canary failure.
- `500`: unexpected or storage failure; fail closed and investigate.

## Incident check and recovery

On 2026-09-30 the watch remained valid, but the OAuth refresh token returned
`invalid_grant`. Renewal and Relay Gmail history reads therefore both failed.
The recovery sequence is in `OAUTH_RECOVERY_RUNBOOK.md`.

Before replacing credentials, pause `daily-duck-watch-renewal` if the next
03:17 execution could cross the users.watch Human Gate. After credential and
revision validation, explicitly approve one live renewal, verify a newer
Firestore watch state, and only then resume the job.

Rollback routes traffic to the last known-good revision and restores the prior
explicit secret version. Never delete watch state or edit `relay_cursor`.
