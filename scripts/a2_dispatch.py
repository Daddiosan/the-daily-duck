"""Phase 3B-2 A2: Gmail event -> classification -> validation -> idempotency ->
dispatch decision.

This module never calls the GitHub API, never opens an IMAP/SMTP connection,
and never writes Daily Duck production state (automation_state/*.json) or
commits to git. It reuses scripts.approval_domain's validated, side-effect-free
primitives instead of re-implementing approval parsing; the only new behavior
here is (1) classifying one already-fetched Gmail message, (2) a
dispatch-layer duplicate-dispatch ledger distinct from decide_transition's own
applied-key tracking, and (3) a dispatch-adapter boundary a future GitHub App
integration can implement without changing this module.

This module is intentionally kept out of cloud/approval_receiver/: that
directory is A1, and tests/test_approval_receiver_contract.py enforces it as
an exact five-file set that must never mention workflow_dispatch,
repository_dispatch, or GitHub at all (A1's job is Gmail reception only). A2
is a separate, later stage in the same pipeline and lives alongside
scripts/approval_domain.py and scripts/approval_shadow.py instead, which it
depends on directly.

cloud/approval_receiver/observation.py's A1 discards the Gmail message's
plaintext body after hashing it into body_sha256. A2 must therefore run on
the message while it is still plaintext, in the same request as the Gmail
fetch, before that discard happens; it cannot classify from A1's stored
observation alone.

The trusted-principal source for this module's build_approval_command() calls
is ApprovalSource.GMAIL_POLL, not ApprovalSource.EVENT. This looks
counterintuitive for an event-driven path, but it is what
scripts/approval_domain.py's _validate_principal_source actually authorizes:
ApprovalSource.EVENT requires a TrustedPrincipalSource.GITHUB_WORKFLOW_CONTEXT
principal (a GitHub Actions actor), which does not exist yet at the point A2
classifies a Gmail message. The trust origin here is an authenticated Gmail
message (GMAIL_MESSAGE_METADATA), which is exactly what GMAIL_POLL pairs with,
regardless of whether the message arrived via periodic IMAP search or a Gmail
push notification. See PHASE_A_NOTES in tests/test_a2_dispatch.py for the
full rationale.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol

try:
    from scripts.approval_domain import (
        ApprovalCommand,
        ApprovalSource,
        ApprovalStage,
        ApprovalValidationError,
        TransitionOutcome,
        build_approval_command,
        decide_transition,
        extract_design_command_from_gmail,
        extract_gate_a_command_from_gmail,
        trusted_principal_from_gmail_metadata,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script invocation
    from approval_domain import (  # type: ignore
        ApprovalCommand,
        ApprovalSource,
        ApprovalStage,
        ApprovalValidationError,
        TransitionOutcome,
        build_approval_command,
        decide_transition,
        extract_design_command_from_gmail,
        extract_gate_a_command_from_gmail,
        trusted_principal_from_gmail_metadata,
    )


# Production target workflow for each stage's event-triggered processing.
# These are the SAME two workflows that already run on a 15-minute schedule
# today (approval-check-phase2.yml, design-selection-check.yml); A2 requests
# an additional workflow_dispatch run of the existing workflow, it does not
# introduce a new workflow file.
WORKFLOW_FILE_BY_STAGE: Mapping[ApprovalStage, str] = {
    ApprovalStage.GATE_A: "approval-check-phase2.yml",
    ApprovalStage.DESIGN_SELECTION: "design-selection-check.yml",
}
DISPATCH_REF = "main"


class EventClassification(str, Enum):
    GATE_A_REPLY = "GATE_A_REPLY"
    DESIGN_REPLY = "DESIGN_REPLY"
    UNRELATED = "UNRELATED"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    STALE_REPLY = "STALE_REPLY"
    INVALID_REPLY = "INVALID_REPLY"


class A2Decision(str, Enum):
    DISPATCHED = "DISPATCHED"
    SKIPPED_DUPLICATE_EVENT = "SKIPPED_DUPLICATE_EVENT"
    SKIPPED_UNRELATED = "SKIPPED_UNRELATED"
    SKIPPED_STALE = "SKIPPED_STALE"
    SKIPPED_INVALID = "SKIPPED_INVALID"
    SKIPPED_NO_OP = "SKIPPED_NO_OP"
    SKIPPED_CONFLICT = "SKIPPED_CONFLICT"
    SKIPPED_DUPLICATE_TRANSITION = "SKIPPED_DUPLICATE_TRANSITION"
    DISPATCH_FAILED_RETRYABLE = "DISPATCH_FAILED_RETRYABLE"
    DISPATCH_FAILED_NON_RETRYABLE = "DISPATCH_FAILED_NON_RETRYABLE"


class DispatchErrorClass(str, Enum):
    RETRYABLE = "RETRYABLE"
    NON_RETRYABLE = "NON_RETRYABLE"


# HTTP-status and symbolic-error-kind classification for A2's OWN outbound
# GitHub dispatch call only. This is a separate layer from
# scripts/gemini_retry.py's LLM-call retry: A2 never calls that helper, and
# nothing in this module sleeps or loops. Retry attempts themselves are the
# Pub/Sub subscription's responsibility (maxDeliveryAttempts), not this
# module's; classify_dispatch_failure only decides whether a failure should
# be acknowledged (non-retryable) or left for Pub/Sub redelivery (retryable).
_RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
_NON_RETRYABLE_HTTP_STATUS = frozenset({400, 401, 403})
_RETRYABLE_ERROR_KINDS = frozenset({"TIMEOUT", "NETWORK_ERROR"})
_NON_RETRYABLE_ERROR_KINDS = frozenset(
    {
        "INVALID_SCHEMA",
        "MISSING_SECRET",
        "CONFIGURATION_ERROR",
        "AUTHENTICATION_ERROR",
    }
)


@dataclass(frozen=True)
class FetchedGmailMessage:
    """The minimum fields A2 needs from an already-fetched Gmail message."""

    gmail_message_id: str
    sender: str
    subject: str
    body: str


@dataclass(frozen=True)
class DispatchAttemptResult:
    """Outcome of one call to a GitHubDispatchAdapter."""

    success: bool
    http_status: int | None = None
    error_kind: str | None = None
    detail: str | None = None


class GitHubDispatchAdapter(Protocol):
    """Boundary a production GitHub App adapter must implement.

    No implementation used by this module may perform a real network call
    during Phase A.
    """

    def dispatch_workflow(
        self, *, workflow_file: str, ref: str, inputs: Mapping[str, str]
    ) -> DispatchAttemptResult: ...


class TransitionLedger(Protocol):
    """Dispatch-layer duplicate-dispatch guard.

    This is deliberately separate from decide_transition's own
    applied_transition_keys check: that check only sees the CURRENT
    automation_state snapshot, which will not yet reflect a dispatch whose
    downstream GitHub Actions run has not completed. Without this ledger, a
    second delivery of an equivalent (but not identical) Gmail message could
    request a second dispatch before the first run finishes.
    """

    def mark_applied_if_absent(self, transition_key: str) -> bool: ...

    def release(self, transition_key: str) -> None:
        """Undo a reservation after a failed dispatch so a legitimate retry
        (this module's own caller, or a Pub/Sub redelivery) is not
        permanently blocked."""
        ...


class InMemoryTransitionLedger:
    """Fake ledger for local tests. Not durable; carries no cloud dependency.

    A production implementation should reuse the same transactional
    check-and-set pattern already implemented by
    cloud.approval_receiver.observation.FirestoreObservationStore
    (insert_observation_if_absent), keyed by transition_key in a separate
    Firestore collection, rather than a new mechanism.
    """

    def __init__(self) -> None:
        self._applied: set[str] = set()

    def mark_applied_if_absent(self, transition_key: str) -> bool:
        if transition_key in self._applied:
            return False
        self._applied.add(transition_key)
        return True

    def release(self, transition_key: str) -> None:
        self._applied.discard(transition_key)


class MessageDedupeStore(Protocol):
    """Message-level (Gmail delivery / Pub/Sub redelivery) duplicate guard.

    contains() is a peek used before processing; insert_observation_if_absent
    is called only once processing reaches a terminal outcome, so a
    RETRYABLE dispatch failure is deliberately left unmarked and a
    redelivery of the same Gmail message can be reprocessed. This is
    structurally compatible with
    cloud.approval_receiver.observation.ObservationStore, so the existing
    Firestore-backed store can be reused in production without adapting
    this module (its insert_observation_if_absent already returns False for
    an id inserted earlier, which is exactly the contains() check plus the
    write in one step; a thin wrapper can supply the separate contains()
    read this Protocol needs).
    """

    def contains(self, observation_id: str) -> bool: ...

    def insert_observation_if_absent(
        self, observation_id: str, observation: Mapping[str, object]
    ) -> bool: ...


class InMemoryMessageDedupeStore:
    """Fake message dedupe store for local tests."""

    def __init__(self) -> None:
        self._seen: dict[str, dict[str, object]] = {}

    def contains(self, observation_id: str) -> bool:
        return observation_id in self._seen

    def insert_observation_if_absent(
        self, observation_id: str, observation: Mapping[str, object]
    ) -> bool:
        if observation_id in self._seen:
            return False
        self._seen[observation_id] = dict(observation)
        return True


class GitHubAppDispatchAdapter:
    """Production dispatch target: GitHub App installation token + workflow_dispatch.

    Not implemented in Phase A. Constructing this class registers no GitHub
    App, mints no token, and performs no network call; it stores the given
    values only as opaque references and never logs or prints them.
    dispatch_workflow always raises NotImplementedError, so this class can
    never make a real network call regardless of how it is invoked.
    """

    def __init__(
        self,
        *,
        app_id: str,
        private_key_pem: str,
        installation_id: str,
        repository: str,
    ) -> None:
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._installation_id = installation_id
        self._repository = repository

    def dispatch_workflow(
        self, *, workflow_file: str, ref: str, inputs: Mapping[str, str]
    ) -> DispatchAttemptResult:
        raise NotImplementedError(
            "Phase A prohibits real GitHub API dispatch calls. "
            "GitHubAppDispatchAdapter is a production interface placeholder only."
        )

    def __repr__(self) -> str:  # never leak the key via default repr/logging
        return f"GitHubAppDispatchAdapter(repository={self._repository!r})"


class FakeDispatchAdapter:
    """Test double. Records every call; never performs network I/O."""

    def __init__(self, results: list[DispatchAttemptResult] | None = None) -> None:
        self._results = (
            list(results)
            if results is not None
            else [DispatchAttemptResult(success=True, http_status=204)]
        )
        self.calls: list[dict[str, object]] = []

    def dispatch_workflow(
        self, *, workflow_file: str, ref: str, inputs: Mapping[str, str]
    ) -> DispatchAttemptResult:
        self.calls.append(
            {"workflow_file": workflow_file, "ref": ref, "inputs": dict(inputs)}
        )
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


def classify_dispatch_failure(result: DispatchAttemptResult) -> DispatchErrorClass:
    """Pure classification of one failed dispatch attempt.

    Never inspects an attempt count; deciding how many times to retry is
    Pub/Sub subscription configuration's job, not this function's.
    """

    if result.http_status is not None:
        if result.http_status in _RETRYABLE_HTTP_STATUS:
            return DispatchErrorClass.RETRYABLE
        if result.http_status in _NON_RETRYABLE_HTTP_STATUS:
            return DispatchErrorClass.NON_RETRYABLE
    if result.error_kind in _RETRYABLE_ERROR_KINDS:
        return DispatchErrorClass.RETRYABLE
    if result.error_kind in _NON_RETRYABLE_ERROR_KINDS:
        return DispatchErrorClass.NON_RETRYABLE
    # Fail closed: an unrecognized failure is never retried automatically.
    return DispatchErrorClass.NON_RETRYABLE


def build_dispatch_payload(command: ApprovalCommand) -> dict[str, str]:
    """The exact, minimal workflow_dispatch inputs. No body text, no subject
    text, no credentials."""

    return {
        "stage": command.stage.value,
        "issue_date": command.issue_date,
        "command": command.command,
        "design_batch_id": command.design_batch_id or "",
        "idempotency_key": command.idempotency_key,
        "transition_key": command.transition_key,
        "source_event_id": command.source_event_id or "",
        "authorized_principal": command.authorized_principal,
    }


@dataclass(frozen=True)
class A2Outcome:
    decision: A2Decision
    classification: EventClassification
    reason: str
    command: ApprovalCommand | None = None
    payload: Mapping[str, str] | None = None
    dispatch_result: DispatchAttemptResult | None = None


def _observation_id(mailbox_identity: str, gmail_message_id: str) -> str:
    # Same construction as cloud/approval_receiver/observation.py's
    # create_sanitized_observation, so both modules derive the same id for
    # the same (mailbox, message) pair without sharing mutable state.
    mailbox_hash = hashlib.sha256(
        mailbox_identity.strip().lower().encode("utf-8")
    ).hexdigest()
    return hashlib.sha256(
        f"{mailbox_hash}:{gmail_message_id}".encode("utf-8")
    ).hexdigest()


def process_gmail_event(
    message: FetchedGmailMessage,
    *,
    mailbox_identity: str,
    allowed_senders: frozenset[str],
    gate_a_subject_pattern: str,
    design_subject_pattern: str,
    production_snapshot: Mapping[str, object],
    message_dedupe_store: MessageDedupeStore,
    transition_ledger: TransitionLedger,
    dispatch_adapter: GitHubDispatchAdapter,
) -> A2Outcome:
    """Classify one already-fetched Gmail message and, only for a validated,
    fresh, not-yet-dispatched approval, request exactly one GitHub Actions
    dispatch.

    Never performs Gmail IMAP access, never writes Daily Duck production
    state, and never calls the GitHub API directly (that is
    dispatch_adapter's responsibility).

    A RETRYABLE dispatch failure deliberately leaves the message unmarked in
    message_dedupe_store, so a Pub/Sub redelivery of the same Gmail message
    reprocesses it instead of being swallowed as a duplicate. Every other
    outcome marks the message so a redelivery of an already-fully-handled
    message becomes a fast no-op.
    """

    observation_id = _observation_id(mailbox_identity, message.gmail_message_id)
    if message_dedupe_store.contains(observation_id):
        return A2Outcome(
            decision=A2Decision.SKIPPED_DUPLICATE_EVENT,
            classification=EventClassification.DUPLICATE_EVENT,
            reason="gmail_message_id already observed",
        )

    outcome = _classify_and_dispatch(
        message,
        allowed_senders=allowed_senders,
        gate_a_subject_pattern=gate_a_subject_pattern,
        design_subject_pattern=design_subject_pattern,
        production_snapshot=production_snapshot,
        transition_ledger=transition_ledger,
        dispatch_adapter=dispatch_adapter,
    )

    if outcome.decision is not A2Decision.DISPATCH_FAILED_RETRYABLE:
        message_dedupe_store.insert_observation_if_absent(
            observation_id, {"gmail_message_id": message.gmail_message_id}
        )
    return outcome


def _classify_and_dispatch(
    message: FetchedGmailMessage,
    *,
    allowed_senders: frozenset[str],
    gate_a_subject_pattern: str,
    design_subject_pattern: str,
    production_snapshot: Mapping[str, object],
    transition_ledger: TransitionLedger,
    dispatch_adapter: GitHubDispatchAdapter,
) -> A2Outcome:
    if gate_a_subject_pattern in message.subject:
        stage = ApprovalStage.GATE_A
        base_pattern = gate_a_subject_pattern
    elif design_subject_pattern in message.subject:
        stage = ApprovalStage.DESIGN_SELECTION
        base_pattern = design_subject_pattern
    else:
        return A2Outcome(
            decision=A2Decision.SKIPPED_UNRELATED,
            classification=EventClassification.UNRELATED,
            reason="subject does not match Gate A or Design Selection pattern",
        )

    active_issue_date = str(production_snapshot.get("active_issue_date") or "")
    if active_issue_date:
        expected_subject = f"{base_pattern} — {active_issue_date}"
        if expected_subject not in message.subject:
            return A2Outcome(
                decision=A2Decision.SKIPPED_STALE,
                classification=EventClassification.STALE_REPLY,
                reason="subject matches pattern but not the active issue date",
            )

    try:
        if stage is ApprovalStage.GATE_A:
            wire_command = extract_gate_a_command_from_gmail(message.body)
            design_batch_id_value: str | None = None
            expected_design_batch_id_value: object | None = None
        else:
            wire_command = extract_design_command_from_gmail(message.body)
            raw_batch = production_snapshot.get("active_design_batch_id")
            design_batch_id_value = str(raw_batch) if raw_batch is not None else None
            expected_design_batch_id_value = raw_batch

        command = build_approval_command(
            stage=stage,
            issue_date=active_issue_date,
            command=wire_command,
            source_type=ApprovalSource.GMAIL_POLL,
            trusted_principal=trusted_principal_from_gmail_metadata(message.sender),
            allowed_principals=allowed_senders,
            message_id=message.gmail_message_id,
            design_batch_id=design_batch_id_value,
            expected_design_batch_id=expected_design_batch_id_value,
        )
    except ApprovalValidationError as exc:
        return A2Outcome(
            decision=A2Decision.SKIPPED_INVALID,
            classification=EventClassification.INVALID_REPLY,
            reason=exc.reason,
        )

    classification = (
        EventClassification.GATE_A_REPLY
        if stage is ApprovalStage.GATE_A
        else EventClassification.DESIGN_REPLY
    )

    transition = decide_transition(
        command,
        current_state=production_snapshot.get("current_state"),
        current_issue_date=production_snapshot.get("active_issue_date"),
        current_command=production_snapshot.get("current_command"),
        current_design_batch_id=production_snapshot.get("active_design_batch_id"),
    )

    if transition.outcome is TransitionOutcome.NO_OP_ALREADY_APPLIED:
        return A2Outcome(
            decision=A2Decision.SKIPPED_NO_OP,
            classification=classification,
            reason=transition.reason,
            command=command,
        )
    if transition.outcome in (
        TransitionOutcome.REJECT_STALE,
        TransitionOutcome.REJECT_CONFLICT,
    ):
        return A2Outcome(
            decision=A2Decision.SKIPPED_CONFLICT,
            classification=classification,
            reason=transition.reason,
            command=command,
        )
    if transition.outcome is TransitionOutcome.REJECT_INVALID:
        return A2Outcome(
            decision=A2Decision.SKIPPED_INVALID,
            classification=EventClassification.INVALID_REPLY,
            reason=transition.reason,
            command=command,
        )

    # TransitionOutcome.APPLY beyond this point. Reserve the transition
    # before dispatching so a second, concurrently classified delivery of an
    # equivalent command cannot request a second dispatch.
    if not transition_ledger.mark_applied_if_absent(command.transition_key):
        return A2Outcome(
            decision=A2Decision.SKIPPED_DUPLICATE_TRANSITION,
            classification=classification,
            reason="transition_key already dispatched",
            command=command,
        )

    payload = build_dispatch_payload(command)
    workflow_file = WORKFLOW_FILE_BY_STAGE[stage]
    result = dispatch_adapter.dispatch_workflow(
        workflow_file=workflow_file, ref=DISPATCH_REF, inputs=payload
    )

    if result.success:
        return A2Outcome(
            decision=A2Decision.DISPATCHED,
            classification=classification,
            reason="dispatch requested",
            command=command,
            payload=payload,
            dispatch_result=result,
        )

    # The dispatch did not happen; release the reservation so a legitimate
    # retry (this module's caller reprocessing after a Pub/Sub redelivery)
    # is not permanently blocked by this attempt's failure.
    transition_ledger.release(command.transition_key)
    error_class = classify_dispatch_failure(result)
    failure_decision = (
        A2Decision.DISPATCH_FAILED_RETRYABLE
        if error_class is DispatchErrorClass.RETRYABLE
        else A2Decision.DISPATCH_FAILED_NON_RETRYABLE
    )
    return A2Outcome(
        decision=failure_decision,
        classification=classification,
        reason=result.detail or "dispatch failed",
        command=command,
        payload=payload,
        dispatch_result=result,
    )
