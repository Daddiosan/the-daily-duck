"""Wake-up-only routing and deterministic identifiers for the thin relay.

The sender decision supplied here comes from authenticated Gmail metadata.
It is a routing prerequisite, never approval authority. Downstream workflows
remain responsible for validating the actual approval command.
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
    """Sanitized per-message input; sender is reduced to a boolean."""

    mailbox_identity: str
    gmail_message_id: str
    subject: str | None
    from_allowlist_match: bool


def classify_stage(subject: str | None, config: RoutingConfig) -> RelayStage:
    """Pure routing classification; no approval-command interpretation."""

    if not isinstance(subject, str):
        return RelayStage.UNRELATED
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
    return _WORKFLOW_BY_STAGE.get(stage)


def mailbox_hash_for(mailbox_identity: str) -> str:
    """One-way mailbox identity for persisted keys."""

    return hashlib.sha256(
        mailbox_identity.strip().casefold().encode("utf-8")
    ).hexdigest()


def notification_key_for(mailbox_identity: str, history_id: str) -> str:
    """Stable notification-level dedupe key."""

    return hashlib.sha256(
        f"notification:{mailbox_hash_for(mailbox_identity)}:{history_id}".encode(
            "utf-8"
        )
    ).hexdigest()


def event_key_for(mailbox_identity: str, gmail_message_id: str) -> str:
    """Stable message-level routing/future-dispatch dedupe key."""

    return hashlib.sha256(
        f"message:{mailbox_hash_for(mailbox_identity)}:{gmail_message_id}".encode(
            "utf-8"
        )
    ).hexdigest()
