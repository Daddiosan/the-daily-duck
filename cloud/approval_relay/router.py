"""Routing for cloud/approval_relay (Thin Relay, Phase M3A): decides which
existing GitHub Actions approval workflow, if any, a Gmail reply event
should wake up.

ROUTING ONLY -- READ THIS FIRST: the RelayStage this module returns is a
wake-up-signal target, never an approval decision. Nothing in this module
inspects, parses, or trusts a human-authored approval command (a story
number, an image/title selection, etc.) -- that stays the exclusive
responsibility of the existing GitHub Actions workflows
(approval-check-phase2.yml -> scripts/check_story_approval.py,
design-selection-check.yml -> scripts/check_design_selection.py), which
read the full mailbox and validate everything through
scripts/approval_domain.py themselves once dispatched. See
docs/phase3b2/THIN_RELAY_RUNBOOK.md's "Trust Rule" section.

Routing signal and why it is safe to use without production state: this
module classifies purely by the email Subject header, using the same
technique already reviewed and shipped in
cloud/approval_dispatcher/main.py's DispatcherService._resolve_stage_guess
and scripts/a2_dispatch.py's _classify_and_dispatch: `pattern in subject`,
config-driven substring matching. That precedent's own code path proves
stage selection there never touches production automation_state -- state
is only read afterwards, for staleness/no-op checks unrelated to which
stage a message belongs to. The two real outbound subjects are stable and
mutually exclusive substrings, confirmed by reading the literal
construction code: scripts/send_email.py's Gate A subject contains
"Choose Today's Story" and scripts/send_design_approval_email.py's Design
Selection subject contains "Choose Image + Title" -- these never overlap,
survive an "Re: "/"Re: Re: " reply prefix (still present as a substring),
and match the same GATE_A_PATTERN / DESIGN_PATTERN constants already used
as ground truth in tests/test_a2_dispatch.py and
tests/test_approval_dispatcher.py. See
docs/phase3b2/THIN_RELAY_RUNBOOK.md's "Routing Authority" section for the
full writeup this module is based on.

This is intentionally NOT imported from scripts/a2_dispatch.py: that
module's classification function is entangled with full approval-command
parsing, production-state staleness checks, and transition-key/
idempotency-key derivation from command text this relay must never trust
or touch. Reusing the *technique* (subject substring match) without
importing that entangled function is not "duplicating approval-command
parsing regex" -- that regex lives in scripts/approval_domain.py's
extract_gate_a_command_from_gmail / extract_design_command_from_gmail and
is never referenced anywhere in cloud/approval_relay/.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

from .github_dispatch import DESIGN_SELECTION_WORKFLOW, GATE_A_WORKFLOW


class RelayStage(str, Enum):
    GATE_A = "GATE_A"
    DESIGN_SELECTION = "DESIGN_SELECTION"
    UNRELATED = "UNRELATED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class RoutingConfig:
    gate_a_subject_pattern: str
    design_subject_pattern: str


@dataclass(frozen=True)
class RelayInboundEvent:
    """Minimal, already-resolved Gmail metadata this relay needs for
    routing and idempotency -- never a full message.

    mailbox_identity and gmail_message_id feed event_key_for() only.
    subject is consulted only by classify_stage() for this one routing
    decision and is never persisted (ledger.RelayLedgerRecord has no
    subject field) or logged in full -- see main.py's logging allowlist.
    Deliberately has no sender/body/full-subject-persistence field: this
    relay never receives or needs the sender address or message body at
    all.
    """

    mailbox_identity: str
    gmail_message_id: str
    subject: str


def classify_stage(subject: str, config: RoutingConfig) -> RelayStage:
    """Pure routing classification. See module docstring for why this is
    safe without any production-state read.

    Both patterns matching (a malformed/adversarial subject engineered to
    contain both marker phrases) resolves to AMBIGUOUS rather than
    silently preferring one stage -- this relay never guesses when its one
    authoritative signal is itself contradictory.
    """

    gate_a_match = config.gate_a_subject_pattern in subject
    design_match = config.design_subject_pattern in subject
    if gate_a_match and design_match:
        return RelayStage.AMBIGUOUS
    if gate_a_match:
        return RelayStage.GATE_A
    if design_match:
        return RelayStage.DESIGN_SELECTION
    return RelayStage.UNRELATED


_WORKFLOW_BY_STAGE: dict[RelayStage, str] = {
    RelayStage.GATE_A: GATE_A_WORKFLOW,
    RelayStage.DESIGN_SELECTION: DESIGN_SELECTION_WORKFLOW,
}


def workflow_for_stage(stage: RelayStage) -> str | None:
    """Returns the fixed allowlisted workflow name for a dispatchable
    stage, or None for UNRELATED/AMBIGUOUS (never dispatched)."""

    return _WORKFLOW_BY_STAGE.get(stage)


def event_key_for(mailbox_identity: str, gmail_message_id: str) -> str:
    """Deterministic idempotency key.

    Same construction as cloud/approval_receiver/observation.py's
    create_sanitized_observation and scripts/a2_dispatch.py's
    _observation_id (sha256(sha256(mailbox_identity):gmail_message_id)) --
    reused deliberately, not reinvented, so this relay's redelivery-dedupe
    assumption rests on the exact same, already-reviewed foundation those
    two components already depend on: Gmail's message id is stable and
    distinct per message, including across a Pub/Sub redelivery of the
    same notification. See
    docs/phase3b2/THIN_RELAY_RUNBOOK.md's "Event Key" section for the full
    collision/redelivery assumption writeup and why it is an inherited,
    not independently re-verified, assumption.

    Never hashes the message subject or body -- only these two stable,
    non-secret identifiers. Different (mailbox, gmail_message_id) pairs
    collide only on a SHA-256 collision, which this design treats as
    negligible, exactly as the two precedents it mirrors already do.
    """

    mailbox_hash = hashlib.sha256(
        mailbox_identity.strip().lower().encode("utf-8")
    ).hexdigest()
    return hashlib.sha256(
        f"{mailbox_hash}:{gmail_message_id}".encode("utf-8")
    ).hexdigest()
