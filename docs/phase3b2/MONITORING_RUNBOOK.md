# Approval Pipeline Monitoring and Pub/Sub Recovery

Status: repository evaluator and thresholds ready; production alert policies,
Scheduler jobs, IAM, retention, and DLQ are Human-gated and not deployed.

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

## Current Pub/Sub state and proposed hardening

Read-only discovery on 2026-10-01 found subscription
`daily-duck-relay-r1` on topic `daily-duck-gmail-events`, one-day retention,
retry backoff 10--600 seconds, authenticated push, and no dead-letter topic.
At 00:58 JST it had 8 undelivered messages and an oldest-unacked age of 52,917
seconds, consistent with the OAuth outage.

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
