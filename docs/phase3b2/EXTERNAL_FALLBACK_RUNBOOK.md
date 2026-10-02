# Independent Approval Fallback Runbook

Status: `READY_FOR_DEPLOYMENT`. Repository implementation, duplicate-safety
tests, local container build, health smoke test, and authentication-negative
test pass. As of 2026-10-02, the production service, dedicated identities,
Scheduler jobs, and Artifact Registry image do not exist. Every cloud write in
the deployment and rollback sections is behind the Production Human Gate.

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

## Read-only production precheck (2026-10-02)

| Check | Result |
| --- | --- |
| Project / number | `the-daily-duck` / `424584128509` |
| Region | `asia-northeast1` |
| Artifact Registry repository | `daily-duck` exists (Docker) |
| Fallback image | absent from Artifact Registry; local image only |
| Fallback Cloud Run service | absent |
| Runtime service account | absent |
| Scheduler service account | absent |
| Gate A Scheduler job | absent |
| Design Scheduler job | absent |
| GitHub App client ID | `Iv23livuEEqUrwEkLfdw` |
| GitHub App installation ID | `165176453` |
| GitHub App private-key secret | `relay-github-app-private-key:1` (enabled) |
| Existing secret access | primary Relay runtime only |
| Existing fallback IAM bindings | none |

The local image built from source baseline `bcca41c533d1` has image ID
`sha256:3482125dd56c65000da21122653954376fc9de1e39b6f97bd952ea2fcee38557`.
This is not an Artifact Registry digest and is not deployable until the gated
push resolves the remote immutable digest.

## Human-gated deployment sequence

Build from the repository root because the container intentionally copies the
reviewed Relay authentication and GitHub App adapters. Resolve the pushed tag
to a digest before deployment; never deploy the floating tag. The following
PowerShell is a command plan, not authorization:

```powershell
$PROJECT_ID = 'the-daily-duck'
$PROJECT_NUMBER = '424584128509'
$REGION = 'asia-northeast1'
$SERVICE = 'daily-duck-approval-fallback'
$RUNTIME_SA = "daily-duck-fallback-runtime@$PROJECT_ID.iam.gserviceaccount.com"
$SCHEDULER_SA = "daily-duck-fallback-scheduler@$PROJECT_ID.iam.gserviceaccount.com"
$SERVICE_URL = "https://$SERVICE-$PROJECT_NUMBER.$REGION.run.app"
$SOURCE_REV = git rev-parse --short=12 HEAD
$IMAGE_TAG = "asia-northeast1-docker.pkg.dev/$PROJECT_ID/daily-duck/approval-fallback:$SOURCE_REV"

docker build -f cloud/approval_fallback/Dockerfile -t $IMAGE_TAG .

gcloud iam service-accounts create daily-duck-fallback-runtime --project $PROJECT_ID
gcloud iam service-accounts create daily-duck-fallback-scheduler --project $PROJECT_ID

gcloud secrets add-iam-policy-binding relay-github-app-private-key `
  --project $PROJECT_ID `
  --member "serviceAccount:$RUNTIME_SA" `
  --role roles/secretmanager.secretAccessor

docker push $IMAGE_TAG
$IMAGE_DIGEST = gcloud artifacts docker images describe $IMAGE_TAG `
  --project $PROJECT_ID --format 'value(image_summary.digest)'
$FALLBACK_IMAGE = "asia-northeast1-docker.pkg.dev/$PROJECT_ID/daily-duck/approval-fallback@$IMAGE_DIGEST"

gcloud run deploy $SERVICE `
  --project $PROJECT_ID --region $REGION --image $FALLBACK_IMAGE `
  --service-account $RUNTIME_SA --no-allow-unauthenticated --ingress all `
  --concurrency 1 --max-instances 1 `
  --set-env-vars "FALLBACK_OIDC_EXPECTED_AUDIENCE=$SERVICE_URL,FALLBACK_OIDC_EXPECTED_PRINCIPALS=$SCHEDULER_SA,RELAY_GITHUB_APP_CLIENT_ID=Iv23livuEEqUrwEkLfdw,RELAY_GITHUB_APP_INSTALLATION_ID=165176453" `
  --set-secrets "RELAY_GITHUB_APP_PRIVATE_KEY=relay-github-app-private-key:1"

gcloud run services add-iam-policy-binding $SERVICE `
  --project $PROJECT_ID --region $REGION `
  --member "serviceAccount:$SCHEDULER_SA" --role roles/run.invoker

gcloud scheduler jobs create http daily-duck-fallback-gate-a `
  --project $PROJECT_ID --location $REGION `
  --schedule '2,17,32,47 * * * *' --time-zone 'Asia/Tokyo' `
  --uri "$SERVICE_URL/wake/gate-a" --http-method POST `
  --attempt-deadline 60s --max-retry-attempts 0 `
  --oidc-service-account-email $SCHEDULER_SA --oidc-token-audience $SERVICE_URL

gcloud scheduler jobs create http daily-duck-fallback-design-selection `
  --project $PROJECT_ID --location $REGION `
  --schedule '7,22,37,52 * * * *' --time-zone 'Asia/Tokyo' `
  --uri "$SERVICE_URL/wake/design-selection" --http-method POST `
  --attempt-deadline 60s --max-retry-attempts 0 `
  --oidc-service-account-email $SCHEDULER_SA --oidc-token-audience $SERVICE_URL
```

Before creating either job, invoke each fixed route once with an authenticated
operator identity in a non-pending business state and confirm a harmless
checker result. Confirm that unauthenticated calls are rejected by Cloud Run or
the application with `401`/`403`. Do not treat an authenticated validation wake
as approval to alter business state.

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

Full cleanup after the jobs are paused and no request is in flight:

```powershell
gcloud scheduler jobs delete daily-duck-fallback-gate-a --quiet --project the-daily-duck --location asia-northeast1
gcloud scheduler jobs delete daily-duck-fallback-design-selection --quiet --project the-daily-duck --location asia-northeast1
gcloud run services delete daily-duck-approval-fallback --quiet --project the-daily-duck --region asia-northeast1
gcloud secrets remove-iam-policy-binding relay-github-app-private-key --project the-daily-duck --member 'serviceAccount:daily-duck-fallback-runtime@the-daily-duck.iam.gserviceaccount.com' --role roles/secretmanager.secretAccessor
gcloud iam service-accounts delete daily-duck-fallback-scheduler@the-daily-duck.iam.gserviceaccount.com --quiet --project the-daily-duck
gcloud iam service-accounts delete daily-duck-fallback-runtime@the-daily-duck.iam.gserviceaccount.com --quiet --project the-daily-duck
```
