# Independent Approval Fallback Runbook

Status: repository implementation ready; production resources are not created.
Every command in the deployment and rollback sections is behind the Production
Human Gate.

## Purpose and boundary

The fallback is an independent wake-up path when Gmail OAuth or the primary
Relay is unhealthy:

```text
Cloud Scheduler
  -> authenticated Cloud Run service
  -> repository-scoped GitHub App
  -> fixed workflow_dispatch
```

It never reads Gmail, Firestore, an approval body, sender, Subject, or token. It
has two fixed routes:

| Route | Fixed workflow | Suggested schedule |
| --- | --- | --- |
| `POST /wake/gate-a` | `approval-check-phase2.yml` | `2,17,32,47 * * * *` |
| `POST /wake/design-selection` | `design-selection-check.yml` | `7,22,37,52 * * * *` |

The owner is fixed to `Daddiosan`, the repository to `the-daily-duck`, and the
ref to `main` in the reused GitHub App adapter. Request bodies are ignored and
cannot select a repository, ref, workflow, or URL.

The five-minute staggering avoids making both checkers contend at the same
instant. Fifteen minutes is the initial bounded recovery objective. Existing
GitHub `on.schedule` triggers remain enabled as a tertiary best-effort path.

## Duplicate safety

Multiple wake-ups are expected. The GitHub workflows use stage-specific
concurrency groups with `cancel-in-progress: false`. Gate A checks the current
issue state before acting; Design Selection reaches the terminal
`ALREADY_SELECTED` path once the selection is recorded. Contract tests protect
these downstream guards and the fixed dispatch targets.

The fallback returns `202` for an ambiguous GitHub transport outcome. It does
not blindly retry within the same request. The next scheduled tick is the next
bounded wake-up, and the downstream state guard remains authoritative.

## Proposed production resources

| Resource | Proposed value |
| --- | --- |
| Cloud Run service | `daily-duck-approval-fallback` |
| Region | `asia-northeast1` |
| Runtime service account | `daily-duck-fallback-runtime@the-daily-duck.iam.gserviceaccount.com` |
| Scheduler service account | `daily-duck-fallback-scheduler@the-daily-duck.iam.gserviceaccount.com` |
| Gate A job | `daily-duck-fallback-gate-a` |
| Design job | `daily-duck-fallback-design-selection` |
| Ingress | authenticated only |
| GitHub credentials | existing GitHub App identifiers and private-key secret |

The runtime identity needs only access to the GitHub App private-key secret.
It needs no Gmail, Firestore, Pub/Sub, or approval-secret access. The Scheduler
identity needs only `roles/run.invoker` on this service.

## Human-gated deployment sequence

Build from the repository root because the container intentionally copies the
reviewed Relay authentication and GitHub App adapters. Resolve the pushed tag
to a digest before deployment; never deploy the floating tag. The following
PowerShell is a command plan, not authorization:

```powershell
$PROJECT_ID = 'the-daily-duck'
$REGION = 'asia-northeast1'
$SERVICE = 'daily-duck-approval-fallback'
$RUNTIME_SA = "daily-duck-fallback-runtime@$PROJECT_ID.iam.gserviceaccount.com"
$SCHEDULER_SA = "daily-duck-fallback-scheduler@$PROJECT_ID.iam.gserviceaccount.com"
$SOURCE_REV = git rev-parse --short=12 HEAD
$IMAGE_TAG = "asia-northeast1-docker.pkg.dev/$PROJECT_ID/daily-duck/approval-fallback:$SOURCE_REV"

docker build -f cloud/approval_fallback/Dockerfile -t $IMAGE_TAG .
docker push $IMAGE_TAG
$IMAGE_DIGEST = gcloud artifacts docker images describe $IMAGE_TAG `
  --project $PROJECT_ID --format 'value(image_summary.digest)'
$FALLBACK_IMAGE = "asia-northeast1-docker.pkg.dev/$PROJECT_ID/daily-duck/approval-fallback@$IMAGE_DIGEST"

gcloud iam service-accounts create daily-duck-fallback-runtime --project $PROJECT_ID
gcloud iam service-accounts create daily-duck-fallback-scheduler --project $PROJECT_ID

gcloud secrets add-iam-policy-binding relay-github-app-private-key `
  --project $PROJECT_ID `
  --member "serviceAccount:$RUNTIME_SA" `
  --role roles/secretmanager.secretAccessor

gcloud run deploy $SERVICE `
  --project $PROJECT_ID --region $REGION --image $FALLBACK_IMAGE `
  --service-account $RUNTIME_SA --no-allow-unauthenticated `
  --set-env-vars "FALLBACK_OIDC_EXPECTED_PRINCIPALS=$SCHEDULER_SA,RELAY_GITHUB_APP_CLIENT_ID=<existing-app-client-id>,RELAY_GITHUB_APP_INSTALLATION_ID=<existing-installation-id>" `
  --set-secrets "RELAY_GITHUB_APP_PRIVATE_KEY=relay-github-app-private-key:<explicit-version>"

$SERVICE_URL = gcloud run services describe $SERVICE `
  --project $PROJECT_ID --region $REGION --format 'value(status.url)'

gcloud run services add-iam-policy-binding $SERVICE `
  --project $PROJECT_ID --region $REGION `
  --member "serviceAccount:$SCHEDULER_SA" --role roles/run.invoker

gcloud run services update $SERVICE `
  --project $PROJECT_ID --region $REGION `
  --update-env-vars "FALLBACK_OIDC_EXPECTED_AUDIENCE=$SERVICE_URL"

gcloud scheduler jobs create http daily-duck-fallback-gate-a `
  --project $PROJECT_ID --location $REGION `
  --schedule '2,17,32,47 * * * *' --time-zone 'Asia/Tokyo' `
  --uri "$SERVICE_URL/wake/gate-a" --http-method POST `
  --oidc-service-account-email $SCHEDULER_SA --oidc-token-audience $SERVICE_URL

gcloud scheduler jobs create http daily-duck-fallback-design-selection `
  --project $PROJECT_ID --location $REGION `
  --schedule '7,22,37,52 * * * *' --time-zone 'Asia/Tokyo' `
  --uri "$SERVICE_URL/wake/design-selection" --http-method POST `
  --oidc-service-account-email $SCHEDULER_SA --oidc-token-audience $SERVICE_URL
```

Before creating either job, invoke each fixed route once with an authenticated
operator identity in a non-pending business state and confirm a harmless
checker result. Confirm that unauthenticated calls return `401`.

## Validation

1. Cloud Run has no public invoker binding and uses the dedicated runtime SA.
2. Secret references use an explicit version.
3. Both Scheduler jobs report HTTP 2xx.
4. GitHub shows only the corresponding fixed workflow and `main` ref.
5. A simultaneous primary wake-up causes one business transition at most.
6. Logs contain workflow name and fixed outcome category only.

## Rollback

Pause both jobs first; this disables only the secondary path:

```powershell
gcloud scheduler jobs pause daily-duck-fallback-gate-a --project the-daily-duck --location asia-northeast1
gcloud scheduler jobs pause daily-duck-fallback-design-selection --project the-daily-duck --location asia-northeast1
```

Leave the primary Relay and GitHub tertiary schedules unchanged. Route the
fallback service to its prior reviewed revision if needed. Resource deletion
and IAM removal require a separate reviewed cleanup window.
