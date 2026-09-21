"""Phase 3B-2 / M3A thin approval relay core: Gmail reply event -> routing
-> deduplication -> business-retry-budgeted GitHub Actions dispatch
request.

TRUST RULE (read this first): this relay is a WAKE-UP SIGNAL ROUTER, never
an approval authority. It never inspects, parses, or trusts a human's
approval command text, never decides a story/image is approved, and never
writes Daily Duck production automation_state. The existing GitHub Actions
workflows it wakes up (approval-check-phase2.yml,
design-selection-check.yml) remain exclusively responsible for reading the
approval mailbox themselves, validating the human reply through
scripts/approval_domain.py, and committing approval state. See
docs/phase3b2/THIN_RELAY_RUNBOOK.md.

This module is a genuinely new, independent sibling of
cloud/approval_dispatcher/ (A2) and cloud/approval_receiver/ (A1): it does
not import from, depend on, or modify either. It does not import
scripts/approval_domain.py or scripts/a2_dispatch.py either -- unlike A2
(which reuses those to classify and validate a real approval command),
this relay never reaches the approval-command layer at all, so there is
nothing of theirs for it to reuse.

Only a FakeGitHubDispatcher exists in this phase (see
github_dispatch.py) -- no real GitHub network call is possible through
this module. create_app()'s default wiring only ever constructs the fake;
there is no create_app_from_env()-style production wiring in this phase,
deliberately, since a real deployment additionally requires the
not-yet-built GitHub App/token integration this phase explicitly excludes.

DRY_RUN is the default mode everywhere in this phase (local tests, and
create_app()'s own default) -- see RelayMode and RelayService.process_event
for exactly what DRY_RUN does and does not do.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping

from .github_dispatch import (
    DISPATCH_REF,
    DispatchOutcome,
    FakeGitHubDispatcher,
    GitHubDispatcher,
)
from .ledger import (
    InMemoryRelayLedger,
    LedgerStateConflict,
    RelayLedger,
    RelayLedgerState,
    utc_now_iso,
)
from .router import (
    RelayInboundEvent,
    RelayStage,
    RoutingConfig,
    classify_stage,
    event_key_for,
    workflow_for_stage,
)


LOGGER = logging.getLogger("approval_relay")

# Strict allowlist logging: only these keys are ever emitted. Never the
# Gmail message subject, body, sender address, or any credential/token.
_LOG_ALLOWED_FIELDS = frozenset(
    {
        "event_key_prefix",
        "stage",
        "workflow",
        "status",
        "attempt_count",
        "mode",
    }
)


def _log_event(event: str, fields: Mapping[str, Any]) -> None:
    safe = {key: value for key, value in fields.items() if key in _LOG_ALLOWED_FIELDS}
    LOGGER.info(json.dumps({"event": event, **safe}, sort_keys=True, default=str))


# Business retry budget (task spec Sec. 10): initial attempt = 1, maximum
# retries = 3, maximum business attempts = 4. This is entirely independent
# of Pub/Sub transport-level delivery attempts, which this module never
# counts or reasons about.
MAX_BUSINESS_ATTEMPTS = 4


class RelayMode(str, Enum):
    DRY_RUN = "DRY_RUN"
    LIVE = "LIVE"


class RelayStatus(str, Enum):
    DISPATCHED = "DISPATCHED"
    DRY_RUN_ROUTED = "DRY_RUN_ROUTED"
    ALREADY_IN_PROGRESS = "ALREADY_IN_PROGRESS"
    DUPLICATE_TERMINAL_NO_OP = "DUPLICATE_TERMINAL_NO_OP"
    UNRELATED_NO_DISPATCH = "UNRELATED_NO_DISPATCH"
    AMBIGUOUS_NO_DISPATCH = "AMBIGUOUS_NO_DISPATCH"
    MALFORMED_NO_RETRY = "MALFORMED_NO_RETRY"
    SAFE_TO_RETRY = "SAFE_TO_RETRY"
    FAILED_FINAL = "FAILED_FINAL"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    CRITICAL_UNKNOWN_OUTCOME_UNRECORDED = "CRITICAL_UNKNOWN_OUTCOME_UNRECORDED"


@dataclass(frozen=True)
class RelayResult:
    status: RelayStatus
    stage: RelayStage
    workflow: str | None
    event_key: str | None = None
    attempt_count: int | None = None
    workflow_run_id: str | None = None


class MalformedRelayEventError(ValueError):
    """A synthetic/local relay notification is missing a required field.
    Ack/no-retry (task spec Sec. 14): the caller returns a 200-equivalent
    result rather than requesting redelivery, since redelivering the same
    malformed payload can never succeed."""


def decode_relay_notification(payload: Mapping[str, Any]) -> RelayInboundEvent:
    """Decode a synthetic/local representation of a Gmail/Pub/Sub-derived
    notification into a RelayInboundEvent.

    This deliberately does NOT decode a raw Gmail-watch Pub/Sub envelope
    (contrast cloud/approval_dispatcher/main.py's decode_pubsub_envelope)
    and does NOT call the Gmail API: real Gmail/Pub/Sub integration is
    explicitly out of scope for this phase (see module docstring and
    docs/phase3b2/THIN_RELAY_RUNBOOK.md). payload is expected to already
    carry the minimal, already-resolved metadata a future real deployment
    would have obtained via its own thin Gmail read (mirroring A2's own
    independent gmail_reader.py) -- mailbox_identity, gmail_message_id,
    subject -- exactly what task spec Sec. 4 calls a "synthetic/local
    representation of Gmail/PubSub notification".
    """

    if not isinstance(payload, Mapping):
        raise MalformedRelayEventError("Relay notification must be a JSON object.")
    mailbox_identity = str(payload.get("mailbox_identity", "")).strip()
    gmail_message_id = str(payload.get("gmail_message_id", "")).strip()
    subject = payload.get("subject")
    if not mailbox_identity or not gmail_message_id or not isinstance(subject, str):
        raise MalformedRelayEventError(
            "Relay notification requires mailbox_identity, gmail_message_id, "
            "and subject."
        )
    return RelayInboundEvent(
        mailbox_identity=mailbox_identity,
        gmail_message_id=gmail_message_id,
        subject=subject,
    )


class RelayService:
    """Core relay logic: routing -> dedupe/reserve -> (DRY_RUN: stop here)
    -> business-budgeted dispatch attempt -> durable outcome recording.

    No method here ever calls the GitHub dispatcher before the ledger
    reservation/attempt CAS for the same event_key has already durably
    succeeded (task spec Sec. 14, "never dispatch first and record
    later") -- see process_event's call order: begin_attempt() always
    happens, and always succeeds, strictly before self.dispatcher.dispatch
    is ever called.
    """

    def __init__(
        self,
        *,
        ledger: RelayLedger,
        dispatcher: GitHubDispatcher,
        routing: RoutingConfig,
        mode: RelayMode = RelayMode.DRY_RUN,
        clock: Callable[[], str] = utc_now_iso,
        max_business_attempts: int = MAX_BUSINESS_ATTEMPTS,
    ) -> None:
        self.ledger = ledger
        self.dispatcher = dispatcher
        self.routing = routing
        self.mode = mode
        self.clock = clock
        self.max_business_attempts = max_business_attempts

    def process_event(self, event: RelayInboundEvent) -> RelayResult:
        stage = classify_stage(event.subject, self.routing)

        if stage is RelayStage.UNRELATED:
            _log_event("relay_unrelated", {"stage": stage.value})
            return RelayResult(
                status=RelayStatus.UNRELATED_NO_DISPATCH, stage=stage, workflow=None
            )

        event_key = event_key_for(event.mailbox_identity, event.gmail_message_id)

        if stage is RelayStage.AMBIGUOUS:
            # Recorded (if not already present) as a permanently-RECEIVED,
            # human-review-visible observation. Never advanced to an
            # attempt -- AMBIGUOUS is never dispatched, by construction:
            # nothing below this branch ever calls begin_attempt() or the
            # dispatcher for an AMBIGUOUS event_key.
            self.ledger.reserve_new(
                event_key, stage=stage.value, workflow=None, now=self.clock()
            )
            _log_event(
                "relay_ambiguous",
                {"stage": stage.value, "event_key_prefix": event_key[:12]},
            )
            return RelayResult(
                status=RelayStatus.AMBIGUOUS_NO_DISPATCH,
                stage=stage,
                workflow=None,
                event_key=event_key,
            )

        workflow = workflow_for_stage(stage)
        assert workflow is not None  # GATE_A/DESIGN_SELECTION always map to one

        created = self.ledger.reserve_new(
            event_key, stage=stage.value, workflow=workflow, now=self.clock()
        )
        existing = created if created is not None else self.ledger.get(event_key)
        assert existing is not None  # reserve_new only returns None if it exists

        if self.mode is RelayMode.DRY_RUN:
            # DRY_RUN never calls begin_attempt() or the dispatcher,
            # regardless of the record's state -- see RelayMode docstring
            # and task spec Sec. 13. The result can never be confused with
            # a real DISPATCHED/DISPATCH_CONFIRMED outcome.
            _log_event(
                "relay_dry_run_routed",
                {
                    "stage": stage.value,
                    "workflow": workflow,
                    "event_key_prefix": event_key[:12],
                    "mode": self.mode.value,
                },
            )
            return RelayResult(
                status=RelayStatus.DRY_RUN_ROUTED,
                stage=stage,
                workflow=workflow,
                event_key=event_key,
                attempt_count=existing.attempt_count,
            )

        attempt = self.ledger.begin_attempt(event_key, now=self.clock())
        if attempt is None:
            # Either a concurrent duplicate already holds
            # DISPATCH_ATTEMPTING, or the record is already terminal.
            current = self.ledger.get(event_key)
            in_progress = (
                current is not None
                and current.state is RelayLedgerState.DISPATCH_ATTEMPTING
            )
            status = (
                RelayStatus.ALREADY_IN_PROGRESS
                if in_progress
                else RelayStatus.DUPLICATE_TERMINAL_NO_OP
            )
            return RelayResult(
                status=status,
                stage=stage,
                workflow=workflow,
                event_key=event_key,
                attempt_count=current.attempt_count if current is not None else None,
                workflow_run_id=(
                    current.workflow_run_id if current is not None else None
                ),
            )

        dispatch_result = self.dispatcher.dispatch(workflow=workflow, ref=DISPATCH_REF)

        if dispatch_result.outcome is DispatchOutcome.SUCCESS:
            next_state = RelayLedgerState.DISPATCH_CONFIRMED
            status = RelayStatus.DISPATCHED
        elif dispatch_result.outcome is DispatchOutcome.CLEAR_RETRYABLE_FAILURE:
            if attempt.attempt_count < self.max_business_attempts:
                next_state = RelayLedgerState.SAFE_TO_RETRY
                status = RelayStatus.SAFE_TO_RETRY
            else:
                next_state = RelayLedgerState.FAILED_FINAL
                status = RelayStatus.FAILED_FINAL
        elif dispatch_result.outcome is DispatchOutcome.CLEAR_FINAL_FAILURE:
            next_state = RelayLedgerState.FAILED_FINAL
            status = RelayStatus.FAILED_FINAL
        else:
            next_state = RelayLedgerState.UNKNOWN_OUTCOME
            status = RelayStatus.UNKNOWN_OUTCOME

        try:
            final = self.ledger.set_state(
                event_key,
                expected_state=RelayLedgerState.DISPATCH_ATTEMPTING,
                next_state=next_state,
                now=self.clock(),
                workflow_run_id=dispatch_result.workflow_run_id,
            )
        except LedgerStateConflict:
            # A real dispatch attempt already happened; its outcome could
            # not be durably recorded. Never issue a second dispatch call
            # in this request (task spec Sec. 14). Best-effort: try once
            # to at least mark UNKNOWN_OUTCOME for future reconciliation
            # visibility. If even that fails, the record stays stuck in
            # DISPATCH_ATTEMPTING, which is equally safe against automatic
            # redispatch -- see ledger.py's module docstring.
            try:
                self.ledger.set_state(
                    event_key,
                    expected_state=RelayLedgerState.DISPATCH_ATTEMPTING,
                    next_state=RelayLedgerState.UNKNOWN_OUTCOME,
                    now=self.clock(),
                )
            except LedgerStateConflict:
                pass
            _log_event(
                "relay_critical_unrecorded_outcome",
                {
                    "stage": stage.value,
                    "workflow": workflow,
                    "event_key_prefix": event_key[:12],
                },
            )
            return RelayResult(
                status=RelayStatus.CRITICAL_UNKNOWN_OUTCOME_UNRECORDED,
                stage=stage,
                workflow=workflow,
                event_key=event_key,
                attempt_count=attempt.attempt_count,
            )

        _log_event(
            "relay_dispatch_outcome",
            {
                "stage": stage.value,
                "workflow": workflow,
                "status": status.value,
                "attempt_count": final.attempt_count,
                "event_key_prefix": event_key[:12],
            },
        )
        return RelayResult(
            status=status,
            stage=stage,
            workflow=workflow,
            event_key=event_key,
            attempt_count=final.attempt_count,
            workflow_run_id=final.workflow_run_id,
        )


def create_app(
    *,
    service: RelayService | None = None,
    routing: RoutingConfig | None = None,
    mode: RelayMode = RelayMode.DRY_RUN,
) -> Any:
    """Create the Flask adapter.

    No production/network wiring exists in this phase: the default
    service, if none is supplied, always uses InMemoryRelayLedger and
    FakeGitHubDispatcher -- there is no create_app_from_env() and no
    Firestore/real-GitHub-App wiring anywhere in this module (see module
    docstring). Deploying this app to a real Cloud Run service is
    explicitly out of scope for this phase.
    """

    try:
        from flask import Flask, jsonify, request
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("Flask dependency is unavailable.") from exc

    actual_routing = routing or RoutingConfig(
        gate_a_subject_pattern="The Daily Duck — Choose Today's Story",
        design_subject_pattern="The Daily Duck — Choose Image + Title",
    )
    actual_service = service or RelayService(
        ledger=InMemoryRelayLedger(),
        dispatcher=FakeGitHubDispatcher(),
        routing=actual_routing,
        mode=mode,
    )

    app = Flask(__name__)

    @app.post("/relay")
    def relay() -> Any:
        try:
            event = decode_relay_notification(request.get_json(silent=True) or {})
        except MalformedRelayEventError as exc:
            _log_event("relay_malformed_event", {})
            return (
                jsonify(
                    {"status": RelayStatus.MALFORMED_NO_RETRY.value, "reason": str(exc)}
                ),
                200,
            )
        result = actual_service.process_event(event)
        return (
            jsonify(
                {
                    "status": result.status.value,
                    "stage": result.stage.value,
                    "workflow": result.workflow,
                    "attempt_count": result.attempt_count,
                }
            ),
            200,
        )

    @app.get("/health")
    def health() -> Any:
        return jsonify({"status": "OK"}), 200

    return app
