# Approval Pipeline Monitoring and Pub/Sub Recovery

Status: `READY_FOR_DEPLOYMENT`. Repository evaluator, thresholds, native-metric
alert design, OAuth-canary schedule, and rollback plan are ready. Production
alert policies, notification channels, Scheduler jobs, IAM, retention, and DLQ
remain Human-gated and are not deployed.

## Sanitized health contract

`scripts/approval_operations_monitor.py` evaluates a sanitized JSON snapshot.
It has no cloud credentials, performs no network calls, and makes no changes.
Collectors must provide only counts, timestamps, state names, and fixed status
categories. Never include mailboxes, senders, Subjects, bodies, OAuth values,
Authorization headers, provider payloads, or GitHub private keys.

Default critical conditions are:

| Signal | Alert condition |
| --- | --- |
| Relay 5xx | any in the evaluation window |
| OAuth canary | status other than `OK`/`SUCCESS` |
| Pub/Sub backlog | more than 5 messages |
| Oldest unacked | more than 900 seconds |
| Watch renewal | status other than `OK`/`SUCCESS` |
| Watch expiration | missing or less than 48 hours away |
| Relay progress | backlog exists and no successful Relay processing for 30 minutes |
| Gate A checker | absent for 30 minutes only while `WAITING_STORY_SELECTION` |
| Design checker | absent for 30 minutes only while `WAITING_FINAL_SELECTION` |

This deliberately does not alert on a missing GitHub cron run by itself.
Workflow absence becomes actionable only when the corresponding business state
is pending.

Example local evaluation:

```powershell
python scripts/approval_operations_monitor.py --snapshot <sanitized-snapshot.json>
```

Exit `0` means healthy; exit `1` means at least one alert. The output contains
fixed codes and non-sensitive values only.

## Production monitoring plan

Behind one Human Gate, configure:

1. a log-based counter for Cloud Run Relay HTTP 5xx;
2. a six-hour Scheduler call to authenticated `/oauth-canary`, alerting on
   any non-2xx;
3. an alert on `daily-duck-watch-renewal` execution failure;
4. Pub/Sub alerts for `num_undelivered_messages > 5` and
   `oldest_unacked_message_age > 900s` on `daily-duck-relay-r1`;
5. a sanitized periodic collector for Firestore watch expiration and cursor
   progress, plus GitHub checker timestamps and business state;
6. notification channels owned by the operator, tested with a synthetic alert.

The collector must use read-only permissions for Firestore/GitHub and must not
read Gmail messages. Alert-policy creation, notification-channel binding, and
the collector's deployment are production mutations.

### Read-only production precheck (2026-10-02)

- Existing alert policies: none.
- Existing notification channels: none.
- Existing user-defined logs-based metrics: none.
- `daily-duck-oauth-canary`: not deployed.
- Existing watch renewal: enabled at `17 3 * * *`, `Asia/Tokyo`; its first
  automatic recovered execution returned HTTP 200.
- Pub/Sub backlog and oldest-unacked age: both zero at the latest sample.
- Recommended operator email channel: `daily-duck-ops-email`, targeting the
  existing project operator account. Channel creation and destination review
  are part of the Human Gate.

### Concrete alert contract

| Policy | Source | Condition | Window | False-positive expectation | Rollback |
| --- | --- | --- | --- | --- | --- |
| `daily-duck-relay-5xx-critical` | `run.googleapis.com/request_count`, service `daily-duck-approval-relay`, response class `5xx` | sum > 0 | 5 min | A single real 5xx pages; intentional negative canary tests must not target `/relay` | delete policy |
| `daily-duck-oauth-canary-failure` | Cloud Scheduler failure log for `daily-duck-oauth-canary` | any non-2xx attempt | 5 min | transient provider failures can page once; do not auto-rotate credentials | pause canary job, then delete policy/metric |
| `daily-duck-oauth-canary-absence` | successful Scheduler completion counter | no success | 7 hours | one delayed six-hour tick is tolerated for one hour | delete absence policy |
| `daily-duck-watch-renewal-failure` | Cloud Scheduler failure log for `daily-duck-watch-renewal` | any non-2xx attempt | 5 min | retries can produce repeated matching logs; incident grouping should deduplicate | delete policy/metric; do not pause the healthy job merely to silence alerts |
| `daily-duck-watch-renewal-absence` | successful Scheduler completion counter | no success | 26 hours | two-hour grace around the daily schedule | delete absence policy |
| `daily-duck-pubsub-backlog-critical` | `pubsub.googleapis.com/subscription/num_undelivered_messages` for `daily-duck-relay-r1` | > 5 | 5 min | short bursts of five or fewer are ignored | delete policy |
| `daily-duck-pubsub-oldest-critical` | `pubsub.googleapis.com/subscription/oldest_unacked_message_age` for `daily-duck-relay-r1` | > 900 s | 5 min | a short cold start does not page | delete policy |
| `daily-duck-watch-expiration-critical` | sanitized collector field `watch_expiration_ms` | < 48 h remaining or missing | two consecutive 5-min evaluations | provider timestamp skew below one interval is tolerated | pause collector, then delete policy |
| `daily-duck-pending-wake-critical` | sanitized evaluator fixed codes | pending stage and no matching successful checker wake > 30 min | two consecutive 5-min evaluations | terminal/non-pending stages never alert, regardless of cron age | pause collector, then delete policy |

The business-aware policies are driven by the existing
`approval_operations_monitor.py` contract. A collector may read sanitized
Firestore cursor/watch fields, repository business-state names, and GitHub run
timestamps only. It must not read Gmail or emit provider payloads. Native
Cloud Monitoring policies do not depend on the GitHub scheduler.

### OAuth canary production command plan

The existing renewal identity already has `roles/run.invoker` on the Relay and
is accepted by the application OIDC boundary. No new Gmail scope is required.

```powershell
$PROJECT_ID = 'the-daily-duck'
$REGION = 'asia-northeast1'
$RELAY_URL = 'https://daily-duck-approval-relay-s5qi3b7igq-an.a.run.app'
$RENEWAL_SA = 'daily-duck-watch-renewal@the-daily-duck.iam.gserviceaccount.com'

gcloud scheduler jobs create http daily-duck-oauth-canary `
  --project $PROJECT_ID --location $REGION `
  --schedule '43 */6 * * *' --time-zone 'Asia/Tokyo' `
  --uri "$RELAY_URL/oauth-canary" --http-method POST `
  --attempt-deadline 60s --max-retry-attempts 0 `
  --oidc-service-account-email $RENEWAL_SA `
  --oidc-token-audience $RELAY_URL
```

Immediate rollback is:

```powershell
gcloud scheduler jobs pause daily-duck-oauth-canary --project the-daily-duck --location asia-northeast1
```

Each Monitoring API create call must retain the returned full policy name.
Rollback deletes that exact name with an authenticated
`DELETE https://monitoring.googleapis.com/v3/<policy-name>` request. Each
logs-based metric is then removed with
`gcloud logging metrics delete <metric-name> --quiet --project the-daily-duck`.
Never delete a metric while a retained policy still references it.

## Current Pub/Sub state and proposed hardening

Read-only discovery on 2026-10-02 confirms subscription
`daily-duck-relay-r1` on topic `daily-duck-gmail-events`, one-day retention,
retry backoff 10--600 seconds, authenticated push, no dead-letter topic, no
maximum-delivery-attempt policy, zero backlog, and zero oldest-unacked age. The
earlier 8-message outage backlog drained through the repaired primary Relay.

Proposed initial policy:

- extend retention from one day to seven days;
- add `daily-duck-gmail-events-dlq` with max delivery attempts 10;
- add pull subscription `daily-duck-gmail-events-dlq-ops` for controlled
  inspection/replay;
- grant the Pub/Sub service agent publisher on the DLQ topic and subscriber on
  the source subscription;
- alert before delivery attempts or retention are exhausted.

Command plan (do not execute without Human Gate):

```powershell
$PROJECT_ID = 'the-daily-duck'
$PROJECT_NUMBER = '424584128509'
$PUBSUB_AGENT = "service-$PROJECT_NUMBER@gcp-sa-pubsub.iam.gserviceaccount.com"

gcloud pubsub topics create daily-duck-gmail-events-dlq --project $PROJECT_ID
gcloud pubsub subscriptions create daily-duck-gmail-events-dlq-ops `
  --project $PROJECT_ID --topic daily-duck-gmail-events-dlq `
  --message-retention-duration 604800s

gcloud pubsub topics add-iam-policy-binding daily-duck-gmail-events-dlq `
  --project $PROJECT_ID --member "serviceAccount:$PUBSUB_AGENT" `
  --role roles/pubsub.publisher

gcloud pubsub subscriptions add-iam-policy-binding daily-duck-relay-r1 `
  --project $PROJECT_ID --member "serviceAccount:$PUBSUB_AGENT" `
  --role roles/pubsub.subscriber

gcloud pubsub subscriptions update daily-duck-relay-r1 `
  --project $PROJECT_ID --message-retention-duration 604800s `
  --dead-letter-topic "projects/$PROJECT_ID/topics/daily-duck-gmail-events-dlq" `
  --max-delivery-attempts 10
```

Verify the effective subscription configuration and IAM before relying on the
DLQ. Delivery-attempt forwarding is best effort unless the service-agent roles
are correct.

## Backlog and DLQ recovery

For the current OAuth incident, repair OAuth first and allow the original push
subscription to redeliver retained messages. Watch Relay 2xx, cursor monotonic
progress, terminal event states, and falling backlog. Do not pull and republish
live backlog manually.

For a future DLQ event:

1. pause any automated replay;
2. repair and validate the failed dependency;
3. inspect only message IDs, timestamps, delivery attempts, and sanitized
   attributes; never print the Pub/Sub data payload;
4. reconcile `UNKNOWN_OUTCOME` against GitHub runs before any replay;
5. replay through a separately reviewed fixed-purpose tool or approved console
   action, in a small batch;
6. confirm downstream terminal guards, cursor movement, and no duplicate
   business side effect.

Automatic blind DLQ republish is intentionally out of scope.

Pub/Sub hardening rollback, after pausing any replay and verifying that the DLQ
is empty, is:

```powershell
$PROJECT_ID = 'the-daily-duck'
$PROJECT_NUMBER = '424584128509'
$PUBSUB_AGENT = "service-$PROJECT_NUMBER@gcp-sa-pubsub.iam.gserviceaccount.com"

gcloud pubsub subscriptions update daily-duck-relay-r1 `
  --project $PROJECT_ID --message-retention-duration 86400s `
  --clear-dead-letter-policy
gcloud pubsub subscriptions remove-iam-policy-binding daily-duck-relay-r1 `
  --project $PROJECT_ID --member "serviceAccount:$PUBSUB_AGENT" `
  --role roles/pubsub.subscriber
gcloud pubsub topics remove-iam-policy-binding daily-duck-gmail-events-dlq `
  --project $PROJECT_ID --member "serviceAccount:$PUBSUB_AGENT" `
  --role roles/pubsub.publisher
gcloud pubsub subscriptions delete daily-duck-gmail-events-dlq-ops `
  --quiet --project $PROJECT_ID
gcloud pubsub topics delete daily-duck-gmail-events-dlq `
  --quiet --project $PROJECT_ID
```
