"""Pure approval-domain primitives for the Phase 3 hybrid transition.

This module is intentionally not connected to a production workflow in Phase 3A.
It provides a shared, side-effect-free contract that a future Gmail poll path and
event path can both use after an independent validator review.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Collection, Iterable, Mapping


class ApprovalStage(str, Enum):
    GATE_A = "GATE_A"
    DESIGN_SELECTION = "DESIGN_SELECTION"


class ApprovalSource(str, Enum):
    GMAIL_POLL = "GMAIL_POLL"
    # A Gmail message delivered via Gmail push notification / Pub/Sub,
    # rather than found by periodic IMAP search. Its trust origin is the
    # same authenticated Gmail message (GMAIL_MESSAGE_METADATA) as
    # GMAIL_POLL, so it carries an identical authorization contract in
    # _validate_principal_source -- this is a delivery-mechanism label, not
    # a different trust boundary. It is deliberately distinct from EVENT,
    # whose only current consumer (scripts/approval_shadow.py) asserts a
    # GitHub Actions actor identity (TrustedPrincipalSource.
    # GITHUB_WORKFLOW_CONTEXT), a categorically different trust origin.
    GMAIL_PUSH = "GMAIL_PUSH"
    EVENT = "EVENT"
    RECONCILIATION = "RECONCILIATION"


class TrustedPrincipalSource(str, Enum):
    GITHUB_WORKFLOW_CONTEXT = "GITHUB_WORKFLOW_CONTEXT"
    GMAIL_MESSAGE_METADATA = "GMAIL_MESSAGE_METADATA"
    VERIFIED_RECONCILIATION_RECORD = "VERIFIED_RECONCILIATION_RECORD"


class TransitionOutcome(str, Enum):
    APPLY = "APPLY"
    NO_OP_ALREADY_APPLIED = "NO_OP_ALREADY_APPLIED"
    REJECT_INVALID = "REJECT_INVALID"
    REJECT_STALE = "REJECT_STALE"
    REJECT_CONFLICT = "REJECT_CONFLICT"


class DispatchOutcome(str, Enum):
    DISPATCH = "DISPATCH"
    NO_OP_ALREADY_COMPLETED = "NO_OP_ALREADY_COMPLETED"
    NOT_READY = "NOT_READY"


class ApprovalValidationError(ValueError):
    """Fail-closed validation error with a stable machine-readable reason."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class TrustedPrincipalContext:
    """Identity asserted by an authenticated ingress, not by business payload.

    This value does not authenticate a transport by itself. The ingress must
    construct it only from GitHub execution context, Gmail message metadata
    read through the authenticated mailbox, or a previously verified record.
    """

    principal: str
    source: TrustedPrincipalSource


@dataclass(frozen=True)
class AuthorizedPrincipal:
    principal: str
    source: TrustedPrincipalSource
    claimed_principal: str | None = None


@dataclass(frozen=True)
class ApprovalCommand:
    """Validated approval data produced by :func:`build_approval_command`.

    Authorization-sensitive callers MUST use ``build_approval_command``.
    Constructing this dataclass directly only creates a data representation; it
    does not authenticate a transport, validate provenance, or authorize a
    principal. Direct construction is reserved for isolated representation tests.
    """

    stage: ApprovalStage
    issue_date: str
    command: str
    source_type: ApprovalSource
    authorized_principal: str
    principal_source: TrustedPrincipalSource
    claimed_principal: str | None = None
    source_event_id: str | None = None
    upstream_run_id: str | None = None
    design_batch_id: str | None = None
    message_id: str | None = None
    legacy_metadata: bool = False

    @property
    def source_identity(self) -> str:
        # Prefer a mailbox Message-ID when an event bridge and the legacy poller
        # observed the same underlying approval. This lets both transports derive
        # the same delivery key when they carry the same message identity.
        if self.message_id:
            return f"message:{self.message_id}"
        if self.source_event_id:
            return f"event:{self.source_event_id}"
        if self.upstream_run_id:
            return f"run:{self.upstream_run_id}"
        return f"trusted-principal:{self.authorized_principal}"

    @property
    def idempotency_key(self) -> str:
        """Key for duplicate delivery of the same source approval."""

        return _digest(
            {
                "stage": self.stage.value,
                "issue_date": self.issue_date,
                "command": self.command,
                "source_identity": self.source_identity,
                "upstream_run_id": self.upstream_run_id,
                "design_batch_id": self.design_batch_id,
            }
        )

    @property
    def transition_key(self) -> str:
        """Transport-independent key for one business state transition."""

        # Delivery metadata belongs in idempotency_key, not here. In
        # particular, a Gmail poll and an event bridge can observe the same
        # approval while carrying different upstream run/source identifiers.
        return _digest(
            {
                "stage": self.stage.value,
                "issue_date": self.issue_date,
                "command": self.command,
                "design_batch_id": self.design_batch_id,
            }
        )


@dataclass(frozen=True)
class TransitionDecision:
    outcome: TransitionOutcome
    reason: str
    next_state: str | None = None
    dispatch_target: str | None = None
    idempotency_key: str | None = None
    transition_key: str | None = None


@dataclass(frozen=True)
class DispatchDecision:
    outcome: DispatchOutcome
    target: str | None = None


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@-]{0,253}$")
_GATE_COMMAND_RE = re.compile(r"^[1-5]$")
_DESIGN_FINAL_RE = re.compile(r"^([1-3])\s+([1-3])$")
_DESIGN_NEXT_RE = re.compile(r"^NEXT\s+3$", flags=re.IGNORECASE)

# A 32-digit positive integer is vastly beyond any realistic preview batch
# counter while remaining cheap and deterministic to parse on every supported
# Python runtime. Validate the length/value before int<->str conversion so this
# domain boundary never depends on the interpreter's large-integer digit limit.
MAX_DESIGN_BATCH_DIGITS = 32
_MAX_DESIGN_BATCH_VALUE = (10**MAX_DESIGN_BATCH_DIGITS) - 1

_GATE_ADVANCED_STATES = frozenset(
    {
        "APPROVED_STORY",
        "DESIGN_OPTIONS_READY",
        "WAITING_FINAL_SELECTION",
        "DESIGN_SELECTED_READY_TO_PUBLISH",
        "READY_TO_PUBLISH",
        "PUBLISHED",
        "X_POSTED",
    }
)
_DESIGN_ADVANCED_STATES = frozenset(
    {
        "DESIGN_SELECTED_READY_TO_PUBLISH",
        "READY_TO_PUBLISH",
        "PUBLISHED",
        "X_POSTED",
    }
)


def _digest(value: Mapping[str, object]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _required_text(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ApprovalValidationError(
            f"MISSING_{field.upper()}",
            f"{field} is required.",
        )
    return text


def _optional_safe_id(value: object, field: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not _SAFE_ID_RE.fullmatch(text):
        raise ApprovalValidationError(
            f"MALFORMED_{field.upper()}",
            f"{field} contains unsupported characters or is too long.",
        )
    return text


def _optional_message_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > 512 or any(ord(char) < 32 for char in text):
        raise ApprovalValidationError(
            "MALFORMED_MESSAGE_ID",
            "message_id contains control characters or is too long.",
        )
    return text


def _normalize_design_batch_id(
    value: object,
    field: str = "design_batch_id",
) -> str:
    """Canonicalize the existing positive preview_batch_number identity."""

    reason = f"INVALID_{field.upper()}"
    message = f"{field} must be a positive preview batch number."
    if isinstance(value, bool):
        raise ApprovalValidationError(
            reason,
            message,
        )
    if isinstance(value, int):
        if value < 1 or value > _MAX_DESIGN_BATCH_VALUE:
            raise ApprovalValidationError(reason, message)
        return str(value)
    if not isinstance(value, str):
        missing_or_invalid = "MISSING" if value is None else "INVALID"
        raise ApprovalValidationError(
            f"{missing_or_invalid}_{field.upper()}",
            message,
        )

    text = value.strip()
    if not text:
        raise ApprovalValidationError(f"MISSING_{field.upper()}", message)
    if len(text) > MAX_DESIGN_BATCH_DIGITS or not text.isdecimal():
        raise ApprovalValidationError(reason, message)
    try:
        number = int(text)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ApprovalValidationError(reason, message) from exc
    if number < 1:
        raise ApprovalValidationError(reason, message)
    return str(number)


def _normalize_issue_date(value: object) -> str:
    text = _required_text(value, "issue_date")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ApprovalValidationError(
            "INVALID_ISSUE_DATE",
            "issue_date must be an ISO date in YYYY-MM-DD form.",
        ) from exc
    if parsed.isoformat() != text:
        raise ApprovalValidationError(
            "INVALID_ISSUE_DATE",
            "issue_date must be an ISO date in YYYY-MM-DD form.",
        )
    return text


def _normalize_principal_value(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ApprovalValidationError(
            f"MALFORMED_{field.upper()}",
            f"{field} must be a principal string.",
        )
    principal = value.strip()
    if not principal:
        raise ApprovalValidationError(
            f"MISSING_{field.upper()}",
            f"{field} is required.",
        )
    if not _PRINCIPAL_RE.fullmatch(principal):
        raise ApprovalValidationError(
            f"MALFORMED_{field.upper()}",
            f"{field} contains unsupported characters or is too long.",
        )
    # Validate the raw identity as ASCII first. Case folding is safe only after
    # validation; otherwise Unicode characters such as ß or K could collapse
    # into an allowed ASCII principal.
    return principal.casefold()


def trusted_principal_from_github_context(value: object) -> TrustedPrincipalContext:
    """Wrap an identity read directly from authenticated GitHub context.

    For ``workflow_dispatch``, the value MUST come from ``github.actor`` (or an
    authenticated event sender), never from a workflow input. Dispatch inputs
    are untrusted payload claims and, if retained for audit, belong only in
    ``claimed_principal``. This wrapper records provenance; it does not by itself
    prove that a caller obtained the value from GitHub's trusted context.
    """

    if not isinstance(value, str):
        raise ApprovalValidationError(
            "MALFORMED_TRUSTED_PRINCIPAL",
            "GitHub context principal must be a string.",
        )
    return TrustedPrincipalContext(value, TrustedPrincipalSource.GITHUB_WORKFLOW_CONTEXT)


def trusted_principal_from_gmail_metadata(value: object) -> TrustedPrincipalContext:
    """Wrap From metadata read through the authenticated Gmail mailbox."""

    if not isinstance(value, str):
        raise ApprovalValidationError(
            "MALFORMED_TRUSTED_PRINCIPAL",
            "Gmail metadata principal must be a string.",
        )
    return TrustedPrincipalContext(value, TrustedPrincipalSource.GMAIL_MESSAGE_METADATA)


def authorize_principal(
    *,
    trusted_context: TrustedPrincipalContext | None,
    allowed_principals: Collection[str] | None,
    claimed_principal: object | None = None,
) -> AuthorizedPrincipal:
    """Authorize only trusted ingress identity; payload claims cannot elevate."""

    if trusted_context is None:
        raise ApprovalValidationError(
            "MISSING_TRUSTED_PRINCIPAL",
            "Authenticated ingress principal context is required.",
        )
    if not isinstance(trusted_context, TrustedPrincipalContext):
        raise ApprovalValidationError(
            "INVALID_TRUSTED_PRINCIPAL_CONTEXT",
            "trusted_context must come from the authenticated ingress contract.",
        )

    trusted = _normalize_principal_value(
        trusted_context.principal, "trusted_principal"
    )
    try:
        source = TrustedPrincipalSource(trusted_context.source)
    except (TypeError, ValueError) as exc:
        raise ApprovalValidationError(
            "INVALID_TRUSTED_PRINCIPAL_SOURCE",
            "Trusted principal source is not supported.",
        ) from exc

    if allowed_principals is None or isinstance(allowed_principals, (str, bytes)):
        raise ApprovalValidationError(
            "MALFORMED_PRINCIPAL_ALLOWLIST",
            "allowed_principals must be a collection of principal strings.",
        )
    try:
        allowlist_iterator = iter(allowed_principals)
    except TypeError as exc:
        raise ApprovalValidationError(
            "MALFORMED_PRINCIPAL_ALLOWLIST",
            "allowed_principals must be a collection of principal strings.",
        ) from exc
    allowed: set[str] = set()
    for value in allowlist_iterator:
        try:
            allowed.add(_normalize_principal_value(value, "allowlist_principal"))
        except ApprovalValidationError as exc:
            raise ApprovalValidationError(
                "MALFORMED_PRINCIPAL_ALLOWLIST",
                "allowed_principals contains a malformed principal.",
            ) from exc
    if not allowed:
        raise ApprovalValidationError(
            "EMPTY_PRINCIPAL_ALLOWLIST",
            "Authorization fails closed when the principal allowlist is empty.",
        )

    claim: str | None = None
    if claimed_principal is not None:
        claim = _normalize_principal_value(claimed_principal, "claimed_principal")
        if claim != trusted:
            raise ApprovalValidationError(
                "PRINCIPAL_MISMATCH",
                "Payload principal claim does not match trusted ingress identity.",
            )

    if trusted not in allowed:
        raise ApprovalValidationError(
            "UNAUTHORIZED_TRUSTED_PRINCIPAL",
            "Trusted ingress principal is not authorized for this approval.",
        )
    return AuthorizedPrincipal(trusted, source, claim)


def _validate_principal_source(
    source_type: ApprovalSource,
    principal_source: TrustedPrincipalSource,
) -> None:
    allowed_sources = {
        ApprovalSource.EVENT: {TrustedPrincipalSource.GITHUB_WORKFLOW_CONTEXT},
        ApprovalSource.GMAIL_POLL: {TrustedPrincipalSource.GMAIL_MESSAGE_METADATA},
        ApprovalSource.GMAIL_PUSH: {TrustedPrincipalSource.GMAIL_MESSAGE_METADATA},
        ApprovalSource.RECONCILIATION: {
            TrustedPrincipalSource.GMAIL_MESSAGE_METADATA,
            TrustedPrincipalSource.VERIFIED_RECONCILIATION_RECORD,
        },
    }
    if principal_source not in allowed_sources[source_type]:
        raise ApprovalValidationError(
            "TRUSTED_PRINCIPAL_SOURCE_MISMATCH",
            "Trusted principal provenance does not match approval source type.",
        )


def normalize_gate_command(value: object) -> str:
    # The production Gmail path accepts one exact ASCII digit only. Do not NFKC
    # normalize here: that would silently broaden the accepted Gate A syntax.
    if not isinstance(value, str):
        raise ApprovalValidationError(
            "INVALID_GATE_A_COMMAND",
            "Gate A command must be text containing one ASCII digit.",
        )
    text = value.strip()
    if not _GATE_COMMAND_RE.fullmatch(text):
        raise ApprovalValidationError(
            "INVALID_GATE_A_COMMAND",
            "Gate A command must be exactly one ASCII digit from 1 through 5.",
        )
    return f"SELECT_STORY:{text}"


def normalize_design_command(value: object) -> str:
    if not isinstance(value, str):
        raise ApprovalValidationError(
            "INVALID_DESIGN_COMMAND",
            "Design command must be text.",
        )
    normalized = unicodedata.normalize("NFKC", value)
    normalized = re.sub(r"[\u00a0\t]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    match = _DESIGN_FINAL_RE.fullmatch(normalized)
    if match:
        return f"SELECT_DESIGN:{match.group(1)}:{match.group(2)}"
    if _DESIGN_NEXT_RE.fullmatch(normalized):
        return "NEXT_3"
    raise ApprovalValidationError(
        "INVALID_DESIGN_COMMAND",
        "Design command must be 'IMAGE TITLE' (1-3 each) or 'NEXT 3'.",
    )


def extract_gate_a_command_from_gmail(value: object) -> str:
    """Extract the strict Gate A wire command using legacy Gmail semantics."""

    if not isinstance(value, str):
        raise ApprovalValidationError(
            "INVALID_GATE_A_GMAIL_REPLY",
            "Gate A Gmail reply body must be text.",
        )

    kept: list[str] = []
    normalized_text = value.replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized_text.split("\n"):
        stripped = line.strip()
        if stripped.startswith(">"):
            break
        if re.match(r"^On .+ wrote:$", stripped, flags=re.IGNORECASE):
            break
        if re.match(
            r"^\d{4}年\d{1,2}月\d{1,2}日" r".+<.+>:$",
            stripped,
        ):
            break
        if stripped in (
            "-----Original Message-----",
            "-----元のメッセージ-----",
        ):
            break
        if re.match(
            r"^(From|Sent|To|Subject):\s",
            stripped,
            flags=re.IGNORECASE,
        ):
            break
        kept.append(line)

    lines = [line.strip() for line in "\n".join(kept).strip().splitlines() if line.strip()]
    normalized = re.sub(r"\s+", " ", " ".join(lines)).strip()
    try:
        normalize_gate_command(normalized)
    except ApprovalValidationError as exc:
        raise ApprovalValidationError(
            "INVALID_GATE_A_GMAIL_REPLY",
            "Gmail reply does not contain one exact fresh Gate A command.",
        ) from exc
    return normalized


def extract_design_command_from_gmail(value: object) -> str:
    """Extract one unique design wire command using legacy Gmail semantics."""

    if not isinstance(value, str):
        raise ApprovalValidationError(
            "INVALID_DESIGN_GMAIL_REPLY",
            "Design Gmail reply body must be text.",
        )

    fresh: list[str] = []
    for line in value.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            break
        if re.match(r"^On .+ wrote:$", stripped, flags=re.IGNORECASE):
            break
        if re.match(r"^.+ wrote:$", stripped, flags=re.IGNORECASE):
            break
        if stripped in (
            "-----Original Message-----",
            "-----元のメッセージ-----",
            "----- 引用元メッセージ -----",
            "---------- Forwarded message ---------",
        ):
            break
        if re.match(
            r"^(From|Sent|To|Subject):\s",
            stripped,
            flags=re.IGNORECASE,
        ):
            break
        fresh.append(stripped)

    commands: list[tuple[str, str]] = []
    for line in fresh:
        normalized = unicodedata.normalize("NFKC", line)
        normalized = re.sub(r"[\u00a0\t]+", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        match = _DESIGN_FINAL_RE.fullmatch(normalized)
        if match:
            wire_command = f"{match.group(1)} {match.group(2)}"
            commands.append((f"SELECT_DESIGN:{match.group(1)}:{match.group(2)}", wire_command))
            continue
        if _DESIGN_NEXT_RE.fullmatch(normalized):
            commands.append(("NEXT_3", "NEXT 3"))

    unique = {canonical for canonical, _ in commands}
    if len(unique) != 1:
        raise ApprovalValidationError(
            "INVALID_DESIGN_GMAIL_REPLY",
            "Gmail reply must contain exactly one unique fresh design command.",
        )
    canonical = next(iter(unique))
    return next(wire for item, wire in commands if item == canonical)


def build_approval_command(
    *,
    stage: ApprovalStage | str,
    issue_date: object,
    command: object,
    source_type: ApprovalSource | str,
    trusted_principal: TrustedPrincipalContext | None,
    allowed_principals: Collection[str] | None,
    claimed_principal: object | None = None,
    expected_issue_date: object | None = None,
    source_event_id: object | None = None,
    upstream_run_id: object | None = None,
    design_batch_id: object | None = None,
    expected_design_batch_id: object | None = None,
    message_id: object | None = None,
) -> ApprovalCommand:
    """Validate, authorize, and construct an :class:`ApprovalCommand`.

    This is the mandatory construction boundary for authorization-sensitive
    code. ``trusted_principal`` must be derived by the ingress from authenticated
    transport context; a payload identity may be supplied only as
    ``claimed_principal`` and can never grant authority.
    """

    try:
        normalized_stage = ApprovalStage(stage)
    except (TypeError, ValueError) as exc:
        raise ApprovalValidationError(
            "INVALID_STAGE", "Unsupported approval stage."
        ) from exc
    try:
        normalized_source = ApprovalSource(source_type)
    except (TypeError, ValueError) as exc:
        raise ApprovalValidationError(
            "INVALID_SOURCE_TYPE", "Unsupported approval source type."
        ) from exc

    normalized_date = _normalize_issue_date(issue_date)
    if expected_issue_date is not None:
        expected_date = _normalize_issue_date(expected_issue_date)
        if normalized_date != expected_date:
            raise ApprovalValidationError(
                "STALE_ISSUE",
                "Approval issue_date does not match the active issue.",
            )

    authorization = authorize_principal(
        trusted_context=trusted_principal,
        allowed_principals=allowed_principals,
        claimed_principal=claimed_principal,
    )
    _validate_principal_source(normalized_source, authorization.source)
    event_id = _optional_safe_id(source_event_id, "source_event_id")
    run_id = _optional_safe_id(upstream_run_id, "upstream_run_id")
    normalized_message_id = _optional_message_id(message_id)

    if normalized_source is ApprovalSource.EVENT and not event_id:
        raise ApprovalValidationError(
            "MISSING_SOURCE_EVENT_ID",
            "EVENT approvals require a stable source_event_id.",
        )
    if normalized_source is ApprovalSource.RECONCILIATION and not (
        event_id or normalized_message_id or run_id
    ):
        raise ApprovalValidationError(
            "MISSING_SOURCE_IDENTITY",
            "RECONCILIATION approvals require a stable source identity.",
        )

    if normalized_stage is ApprovalStage.GATE_A:
        normalized_command = normalize_gate_command(command)
        if (
            design_batch_id is not None
            and str(design_batch_id).strip()
        ) or expected_design_batch_id is not None:
            raise ApprovalValidationError(
                "UNEXPECTED_DESIGN_BATCH",
                "Gate A approvals cannot carry a design batch.",
            )
        batch_id = None
    else:
        normalized_command = normalize_design_command(command)
        batch_id = _normalize_design_batch_id(design_batch_id)
        if expected_design_batch_id is not None:
            expected_batch = _normalize_design_batch_id(
                expected_design_batch_id, "expected_design_batch_id"
            )
            if batch_id != expected_batch:
                raise ApprovalValidationError(
                    "STALE_DESIGN_BATCH",
                    "Approval design batch does not match the active batch.",
                )

    legacy = normalized_source is ApprovalSource.GMAIL_POLL and not (
        event_id or normalized_message_id or run_id
    )
    return ApprovalCommand(
        stage=normalized_stage,
        issue_date=normalized_date,
        command=normalized_command,
        source_type=normalized_source,
        authorized_principal=authorization.principal,
        principal_source=authorization.source,
        claimed_principal=authorization.claimed_principal,
        source_event_id=event_id,
        upstream_run_id=run_id,
        design_batch_id=batch_id,
        message_id=normalized_message_id,
        legacy_metadata=legacy,
    )


def decide_transition(
    command: ApprovalCommand,
    *,
    current_state: object,
    current_issue_date: object,
    current_command: str | None = None,
    current_design_batch_id: object | None = None,
    applied_idempotency_keys: Iterable[str] = (),
    applied_transition_keys: Iterable[str] = (),
) -> TransitionDecision:
    """Return a decision without writing state or invoking external services."""

    try:
        active_date = _normalize_issue_date(current_issue_date)
    except ApprovalValidationError as exc:
        return TransitionDecision(TransitionOutcome.REJECT_INVALID, exc.reason)
    if command.issue_date != active_date:
        return TransitionDecision(
            TransitionOutcome.REJECT_STALE,
            "approval issue does not match current state",
        )

    state = str(current_state or "").strip().upper()
    if not state:
        return TransitionDecision(
            TransitionOutcome.REJECT_INVALID, "current state is missing"
        )

    # A completed NEXT_3 rotates the active batch. Check the trusted ledger
    # before comparing against that newer batch so a duplicate delivery for
    # the consumed batch remains a no-op. Unrecorded old-batch commands still
    # fail the freshness check below.
    if command.idempotency_key in set(applied_idempotency_keys):
        return _no_op(command, "source delivery was already applied")
    if command.transition_key in set(applied_transition_keys):
        return _no_op(command, "business transition was already applied")

    if command.stage is ApprovalStage.DESIGN_SELECTION:
        try:
            active_batch = _normalize_design_batch_id(
                current_design_batch_id, "current_design_batch_id"
            )
        except ApprovalValidationError as exc:
            return TransitionDecision(TransitionOutcome.REJECT_INVALID, exc.reason)
        if command.design_batch_id != active_batch:
            return TransitionDecision(
                TransitionOutcome.REJECT_STALE,
                "approval design batch does not match current state",
            )

    if command.stage is ApprovalStage.GATE_A:
        if state == "WAITING_STORY_SELECTION":
            return _apply(
                command,
                next_state="APPROVED_STORY",
                dispatch_target="design-options.yml",
            )
        if state in _GATE_ADVANCED_STATES:
            return _advanced_decision(command, current_command)
        return _conflict(command, f"Gate A cannot advance from {state}")

    if state == "WAITING_FINAL_SELECTION":
        if command.command == "NEXT_3":
            return _apply(command, next_state="DESIGN_OPTIONS_READY")
        return _apply(
            command,
            next_state="DESIGN_SELECTED_READY_TO_PUBLISH",
            dispatch_target="website-publish.yml",
        )
    if state in _DESIGN_ADVANCED_STATES:
        return _advanced_decision(command, current_command)
    return _conflict(command, f"Design selection cannot advance from {state}")


def decide_downstream_dispatch(
    stage: ApprovalStage | str,
    *,
    current_state: object,
    downstream_completed: bool,
) -> DispatchDecision:
    """Permit recovery after state commit succeeded but dispatch failed."""

    normalized_stage = ApprovalStage(stage)
    state = str(current_state or "").strip().upper()
    target = (
        "design-options.yml"
        if normalized_stage is ApprovalStage.GATE_A
        else "website-publish.yml"
    )
    ready_states = (
        {"APPROVED_STORY"}
        if normalized_stage is ApprovalStage.GATE_A
        else {"DESIGN_SELECTED_READY_TO_PUBLISH", "READY_TO_PUBLISH"}
    )
    if state not in ready_states:
        return DispatchDecision(DispatchOutcome.NOT_READY)
    if downstream_completed:
        return DispatchDecision(DispatchOutcome.NO_OP_ALREADY_COMPLETED, target)
    return DispatchDecision(DispatchOutcome.DISPATCH, target)


def _apply(
    command: ApprovalCommand,
    *,
    next_state: str,
    dispatch_target: str | None = None,
) -> TransitionDecision:
    return TransitionDecision(
        TransitionOutcome.APPLY,
        "validated transition may be applied",
        next_state=next_state,
        dispatch_target=dispatch_target,
        idempotency_key=command.idempotency_key,
        transition_key=command.transition_key,
    )


def _no_op(command: ApprovalCommand, reason: str) -> TransitionDecision:
    return TransitionDecision(
        TransitionOutcome.NO_OP_ALREADY_APPLIED,
        reason,
        idempotency_key=command.idempotency_key,
        transition_key=command.transition_key,
    )


def _conflict(command: ApprovalCommand, reason: str) -> TransitionDecision:
    return TransitionDecision(
        TransitionOutcome.REJECT_CONFLICT,
        reason,
        idempotency_key=command.idempotency_key,
        transition_key=command.transition_key,
    )


def _advanced_decision(
    command: ApprovalCommand,
    current_command: str | None,
) -> TransitionDecision:
    if current_command and current_command != command.command:
        return _conflict(command, "state was advanced by a different command")
    return _no_op(command, "state is already at or beyond this transition")
