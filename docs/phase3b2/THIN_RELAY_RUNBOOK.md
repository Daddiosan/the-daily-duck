# Thin Approval Relay Runbook (Phase 3B-2 / M3A)

Component: `cloud/approval_relay/`
Task: TDD-M3A-THIN-RELAY-LOCAL-01

## Architecture

```
Gmail (Daily Duck mailbox)
  -> existing Gmail watch (owned by A1 only)
  -> existing Pub/Sub topic
       +-- existing push subscription -> A1 (cloud/approval_receiver)
       +-- existing push subscription -> A2 (cloud/approval_dispatcher, shadow only)
       +-- NEW push subscription      -> Relay (cloud/approval_relay)
                                             -> routing (subject only)
                                             -> ledger reserve/attempt CAS
                                             -> GitHub Actions workflow_dispatch
                                                  (approval-check-phase2.yml or
                                                   design-selection-check.yml)
```

The relay is a third independent sibling subscription on the same
Pub/Sub topic A1 and A2 already use (the same fan-out pattern
`docs/phase3b2/A2_SHADOW_RUNBOOK.md` establishes for A2). It does not
import from, depend on, or modify `cloud/approval_receiver/` (A1) or
`cloud/approval_dispatcher/` (A2), and neither of those is modified by
this component's existence.

## Trust rule

**A relay event is a wake-up signal, not an approval.** The relay never
inspects, parses, or trusts a human's approval command text (a story
number, an image/title selection). It never decides a story or image is
approved, never writes Daily Duck production `automation_state`, never
sends email, never publishes the website or X, and never commits to git.
The GitHub Actions workflows it wakes up
(`approval-check-phase2.yml` -> `scripts/check_story_approval.py`,
`design-selection-check.yml` -> `scripts/check_design_selection.py`)
remain exclusively responsible for reading the approval mailbox
themselves, validating the human reply through
`scripts/approval_domain.py`, issue-date/staleness/design-batch
validation, committing approval state, and triggering downstream
production steps.

## Routing authority

The relay classifies a candidate stage (`GATE_A`, `DESIGN_SELECTION`,
`UNRELATED`, `AMBIGUOUS`) from the inbound reply's **email Subject header
only** — a plain `pattern in subject` substring match against two
configured constants, `RoutingConfig.gate_a_subject_pattern` /
`design_subject_pattern` (`cloud/approval_relay/router.py`,
`classify_stage`). This is the exact technique already reviewed and
shipped in `cloud/approval_dispatcher/main.py`'s
`DispatcherService._resolve_stage_guess` and
`scripts/a2_dispatch.py`'s `_classify_and_dispatch`: stage there is
selected purely from a subject substring match, strictly before any
production-`automation_state` read (production state is only consulted
afterwards, for staleness/no-op checks unrelated to which stage a message
belongs to). No production state is required to distinguish the two
stages, and the relay reads none.

This is safe because the two real outbound approval-email subjects are
stable and mutually exclusive substrings:

- Gate A (`scripts/send_email.py`): `"The Daily Duck — Choose Today's
  Story — <issue_date>"`
- Design Selection (`scripts/send_design_approval_email.py`):
  `"The Daily Duck — Choose Image + Title — <issue_date> — Batch <N>"`

`"Choose Today's Story"` and `"Choose Image + Title"` never overlap as
substrings and both survive an email client's `"Re: "` / `"Re: Re: "`
reply prefix (the base phrase remains a substring of the reply Subject).
If a malformed/adversarial subject were engineered to contain both marker
phrases, `classify_stage` resolves to `AMBIGUOUS` rather than silently
preferring one stage or triggering both.

This subject-substring check is routing-only pattern matching, not
approval-command parsing — the relay never imports or calls
`scripts/approval_domain.py`'s `extract_gate_a_command_from_gmail` /
`extract_design_command_from_gmail`, and never touches the reply body at
all.

## Final approval authority

Unchanged and untouched by this component: the existing GitHub Actions
workflows and `scripts/approval_domain.py`'s `decide_transition`, exactly
as before. The relay has zero authority over approval decisions.

## Fixed workflow allowlist

`cloud/approval_relay/github_dispatch.py` defines:

- `GATE_A_WORKFLOW = "approval-check-phase2.yml"`
- `DESIGN_SELECTION_WORKFLOW = "design-selection-check.yml"`
- `DISPATCH_REF = "main"`

These are application-controlled Python constants, never derived from
caller input. `router.workflow_for_stage` maps `GATE_A` /
`DESIGN_SELECTION` to exactly these two names and `UNRELATED` /
`AMBIGUOUS` to `None` (never dispatched).
`github_dispatch.FakeGitHubDispatcher.dispatch` independently re-validates
both `workflow` and `ref` against the same allowlist/constant before
returning any outcome (defense in depth — see the security contract
below).

## Event key

`router.event_key_for(mailbox_identity, gmail_message_id)` computes
`sha256(sha256(mailbox_identity.strip().lower()):gmail_message_id)`.

This is the same construction already used by
`cloud/approval_receiver/observation.py`'s `create_sanitized_observation`
and `scripts/a2_dispatch.py`'s `_observation_id` — reused deliberately,
not reinvented, so the relay's redelivery-dedupe key rests on the exact
same foundation those two already-reviewed components depend on.

**Inherited assumption, not independently re-verified here:** Gmail's
message `id` is treated as stable and distinct per message, including
across a Pub/Sub redelivery of the same notification. No sentence in this
repository states that as an explicit Gmail API guarantee (it is not
documented anywhere in this codebase); A1, A2, and now this relay all
build their idempotency mechanisms directly on top of that assumption
without an independent citation. The message body is never hashed to
form the key — only the two stable, non-secret identifiers above.
Different `(mailbox_identity, gmail_message_id)` pairs collide only on a
SHA-256 collision, treated as negligible.

## Retry: transport vs. business budget

Pub/Sub delivery-attempt retries (a transport concern, not implemented by
this local-only phase) are entirely separate from the relay's own
**business** attempt budget, tracked durably per `event_key` in
`RelayLedgerRecord.attempt_count`:

- initial attempt = 1
- maximum retries = 3
- maximum business attempts = 4

`cloud/approval_relay/main.py`'s `MAX_BUSINESS_ATTEMPTS = 4` enforces
this: a `CLEAR_RETRYABLE_FAILURE` outcome moves the record to
`SAFE_TO_RETRY` only while `attempt_count < 4`; at `attempt_count == 4` it
moves to `FAILED_FINAL` instead. No sleep/backoff loop exists anywhere in
this component (see the security contract).

## Ledger states

`cloud/approval_relay/ledger.py` — `RelayLedgerState`:

`RECEIVED`, `DISPATCH_ATTEMPTING`, `DISPATCH_CONFIRMED`, `SAFE_TO_RETRY`,
`UNKNOWN_OUTCOME`, `FAILED_FINAL`. The exhaustive legal transition table
is `ledger.LEGAL_TRANSITIONS`; see that module's docstring for the full
state-machine writeup, including why a crash between a real dispatch
attempt and its durable outcome write is safe by construction (the
record stays stuck in `DISPATCH_ATTEMPTING`, which is not
attempt-eligible, so it can never be automatically redispatched — the
same non-redispatch guarantee `UNKNOWN_OUTCOME` carries, without a
seventh state).

## UNKNOWN_OUTCOME

Covers every case where a dispatch request may have reached GitHub but
this relay never received a trustworthy confirmation. `UNKNOWN_OUTCOME`
is reserved and **never** automatically redispatched, by any future
duplicate Pub/Sub delivery. Only a future, **separately approved**
reconciliation/manual-resolution path may resolve it — that path is not
designed or implemented in this phase.

## Security

- Fixed `workflow`/`ref` targets only (see Fixed workflow allowlist
  above); no arbitrary `workflow_dispatch` endpoint, no caller-controlled
  repo/ref.
- No real GitHub network implementation exists in this phase — only a
  `Protocol` and `FakeGitHubDispatcher`
  (`cloud/approval_relay/github_dispatch.py`). Adding a real GitHub
  App/JWT/token implementation requires an explicit **Human Gate**
  covering the credential-separation review, exactly as
  `docs/phase3b2/A2_SHADOW_RUNBOOK.md` already requires for A2/any
  successor — this relay is that successor.
- No SMTP, no website/X publication capability, no periodic
  schedule/polling implementation anywhere in this component.
- No production `automation_state` mirror or write.
- No full email body, full subject, or sender email address is ever
  persisted or logged — `RelayLedgerRecord` has no such fields, and
  `main.py`'s `_LOG_ALLOWED_FIELDS` is an explicit allowlist enforced by
  `tests/test_approval_relay_contract.py`.
- Removing the existing 15-minute cron schedules
  (`approval-check-phase2.yml`, `design-selection-check.yml`) requires
  its own explicit **Human Gate** and is not authorized by this phase —
  see Deployment progression below.

## Deployment progression

- **Phase R0** (this phase, M3A): local fake dispatcher only. No cloud
  deployment.
- **Phase R1**: cloud deployment in `DRY_RUN` mode, zero real GitHub
  dispatch.
- **Phase R2**: real GitHub dispatch enabled, but the existing 15-minute
  cron schedules on `approval-check-phase2.yml` /
  `design-selection-check.yml` remain unchanged and continue running.
- **Phase R3**: parallel observation / acceptance period (relay dispatch
  and cron both active; compare outcomes).
- **Phase R4**: an explicit Human Gate to disable the 15-minute cron,
  once R3 has demonstrated the relay is reliable.
- **Phase R5**: a daily reconciliation/catch-up mechanism remains in
  place even after R4 (covers `UNKNOWN_OUTCOME` resolution and any
  Gmail-history unobserved-window gap — see A2's own documented cursor
  limitations, which this relay inherits until a lossless catch-up
  design is separately approved).

**Real GitHub dispatch and cron removal are NOT authorized by this
phase (M3A).** Only the local fake dispatcher and `DRY_RUN` routing exist
here.

## Rollback

Disable the relay's Pub/Sub subscription/traffic; the existing
15-minute cron schedules on `approval-check-phase2.yml` and
`design-selection-check.yml` remain the system of record until Phase R4's
Human Gate explicitly disables them. No change made by this phase alters
those workflows, their cron triggers, or any other existing file.

## Out of scope for this phase

Real GitHub App/token, real `workflow_dispatch` API calls, actual Cloud
Run deployment, actual Pub/Sub subscription, actual Firestore, Gmail
watch changes, catch-up/backfill implementation, cron removal, any
modification to the existing approval workflows, Vercel configuration,
real `UNKNOWN_OUTCOME` reconciliation, and real credential/Secret Manager
setup.
