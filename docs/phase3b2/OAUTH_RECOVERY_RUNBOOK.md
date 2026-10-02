# Gmail OAuth Production Recovery Runbook

Status: production recovery completed on 2026-10-01; the first automatic watch
renewal and a real natural-fire approval were accepted on 2026-10-02. The
command plan remains below for incident response only.

## Confirmed failure and recovered state

The 2026-09-30 natural-fire failure occurred when the Relay attempted Gmail
API access and refresh failed with the sanitized category `invalid_grant`
(expired or revoked refresh token). Gmail Push and Pub/Sub delivery were
working. The watch was still valid and the downstream checker was healthy.

Initial read-only discovery on 2026-10-01 found:

- project `the-daily-duck`, region `asia-northeast1`;
- service `daily-duck-approval-relay`;
- active revision `daily-duck-approval-relay-r2c-live-664fc15`, 100% traffic;
- OAuth client and refresh token injected as environment secrets;
- both references used `latest`, with enabled version `1` at discovery time;
- Scheduler `daily-duck-watch-renewal`, `17 3 * * *`, `Asia/Tokyo`;
- watch history ID `35154`, expiration `2026-10-06 03:17:10 JST`;
- processing cursor history ID `35115`.

The reviewed recovery then completed with these production facts:

- OAuth app: `External / In production` (human-confirmed);
- active revision: `daily-duck-approval-relay-oauth-v2-e377414`, 100% traffic;
- OAuth client secret reference: explicit version `1`;
- OAuth refresh-token reference: explicit version `2`;
- authenticated `/oauth-canary`: HTTP 200;
- retained Pub/Sub backlog: drained to zero without a manual cursor edit;
- one approved live `/renew-watch`: HTTP 200;
- first resumed automatic renewal at 2026-10-02 03:17 JST: HTTP 200;
- watch expiration advanced from 2026-10-08 21:33:04.807 JST to
  2026-10-09 03:17:05.559 JST while the processing cursor stayed `37076`;
- real Design Selection reply at 2026-10-02 19:29:11 JST traversed Gmail Push,
  Pub/Sub, Relay, Gmail history, GitHub App dispatch, and the downstream checker;
- processing cursor advanced monotonically from `37680` to `37830`;
- Relay event reached `DISPATCH_CONFIRMED` in one attempt and GitHub run
  `36995749123` completed successfully on `main`;
- downstream state reached `READY_TO_PUBLISH`; Website and X each ran once;
- no duplicate business side effect and no 2026-09-30 replay occurred.

The production refresh-token value is never recorded here; version metadata is
the only repository evidence.

Do not edit or reset the cursor. Retained Pub/Sub delivery is the recovery
source after Gmail access is restored.

## Human action: OAuth publishing and consent

In Google Auth Platform, a human must verify that the OAuth app associated with
`relay-gmail-oauth-client-json` is `External / In production`. The status is
not safely observable from the deployed token metadata, so it must not be
inferred. If it is Testing, publishing is required before granting the
replacement long-lived offline token.

Re-consent the existing installed/Desktop client for the Relay mailbox with
only the existing `gmail.readonly` scope and offline access. Never paste the
authorization code, refresh token, client secret, or complete consent URL into
chat, tickets, logs, or this repository.

## Human-gated recovery sequence

Execute these steps as one reviewed change window:

1. Pause `daily-duck-watch-renewal` so an automatic live `users.watch` call
   cannot cross the separate Human Gate during recovery.
2. Verify External/In production and complete re-consent.
3. Add the new refresh token as a new Secret Manager version through stdin.
4. Build the reviewed repository revision, resolve it to an immutable digest,
   and deploy a no-traffic Relay candidate pinned to explicit OAuth secret
   versions. The new image is required because it contains `/oauth-canary`.
5. Invoke authenticated `POST /oauth-canary`; require HTTP 200.
6. Shift traffic to the candidate revision.
7. Observe Relay 2xx responses, cursor progression, and backlog drain.
8. With explicit approval for a live Gmail call, invoke `/renew-watch` once;
   require a newer valid watch state, then resume the renewal job.
9. Complete one real approval-reply natural-path acceptance test.

The following PowerShell is the exact command shape. Replace only bracketed
values after review; do not put a secret value on the command line:

```powershell
$PROJECT_ID = 'the-daily-duck'
$REGION = 'asia-northeast1'
$SERVICE = 'daily-duck-approval-relay'

gcloud scheduler jobs pause daily-duck-watch-renewal `
  --project $PROJECT_ID --location $REGION

# Paste only the new refresh token into stdin, then send EOF (Ctrl+Z, Enter in
# Windows PowerShell). The value must not appear in shell history.
gcloud secrets versions add relay-gmail-oauth-refresh-token `
  --project $PROJECT_ID --data-file=-

gcloud secrets versions list relay-gmail-oauth-refresh-token `
  --project $PROJECT_ID --filter 'state=ENABLED' `
  --sort-by '~createTime' --limit 2 `
  --format 'table(name.basename(),state,createTime)'

$CLIENT_VERSION = '1'
$TOKEN_VERSION = '<new refresh-token version>'
$SOURCE_REV = git rev-parse --short=12 HEAD
$IMAGE_TAG = "asia-northeast1-docker.pkg.dev/$PROJECT_ID/daily-duck/approval-relay:$SOURCE_REV"

docker build -f cloud/approval_relay/Dockerfile -t $IMAGE_TAG cloud/approval_relay
docker push $IMAGE_TAG
$IMAGE_DIGEST = gcloud artifacts docker images describe $IMAGE_TAG `
  --project $PROJECT_ID --format 'value(image_summary.digest)'
$RELAY_IMAGE = "asia-northeast1-docker.pkg.dev/$PROJECT_ID/daily-duck/approval-relay@$IMAGE_DIGEST"

gcloud run deploy $SERVICE `
  --project $PROJECT_ID --region $REGION --image $RELAY_IMAGE `
  --no-traffic --tag oauth-recovery-candidate `
  --update-secrets "RELAY_GMAIL_OAUTH_CLIENT_JSON=relay-gmail-oauth-client-json:$CLIENT_VERSION,RELAY_GMAIL_OAUTH_REFRESH_TOKEN=relay-gmail-oauth-refresh-token:$TOKEN_VERSION"

gcloud run revisions list --service $SERVICE `
  --project $PROJECT_ID --region $REGION `
  --format 'table(metadata.name,status.conditions[0].status,status.conditions[0].message)'
```

Use the candidate tag URL with an identity token whose audience is the
configured service audience. Call only `POST /oauth-canary`; its response and
logs expose a fixed status/category, never Gmail profile data. After HTTP 200:

```powershell
$CANDIDATE_REVISION = '<ready candidate revision>'
gcloud run services update-traffic $SERVICE `
  --project $PROJECT_ID --region $REGION `
  --to-revisions "$CANDIDATE_REVISION=100"
```

The live `/renew-watch` invocation and Scheduler resume remain separately
explicit steps because they mutate the Gmail watch:

```powershell
gcloud scheduler jobs run daily-duck-watch-renewal `
  --project $PROJECT_ID --location $REGION

gcloud scheduler jobs resume daily-duck-watch-renewal `
  --project $PROJECT_ID --location $REGION
```

## Fail-closed checks

Stop without traffic shift if the canary is not 200, the candidate is not
Ready, its secret references are not explicit versions, authentication fails,
or logs contain anything other than fixed categories. Do not repeatedly run
watch renewal and do not dispatch the 2026-09-30 approval again.

## Rollback

Keep the Scheduler paused. Route 100% traffic to
`daily-duck-approval-relay-r2c-live-664fc15` and restore the prior explicit
secret version only if it is still valid and explicitly approved. Do not
delete the new version during the incident window, delete Firestore records,
or change `relay_cursor`. If no valid prior token exists, rollback means
leaving Relay fail-closed while the independent fallback remains available.

Expected Cloud Run traffic-shift downtime is effectively zero; primary Gmail
processing remains unavailable until a valid token reaches a Ready revision.
