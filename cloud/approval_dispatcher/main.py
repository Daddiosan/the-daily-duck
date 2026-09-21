"""Phase 3B-2 A2 shadow sibling service: Gmail push -> classification ->
sanitized shadow persistence. ZERO GitHub dispatch, ZERO publishing, ZERO
email sending in this phase.

This service is an independent sibling of cloud/approval_receiver/ (A1),
not its downstream: it subscribes to the same existing Gmail Pub/Sub topic
via its own separate push subscription and performs its own independent
Gmail API read (see gmail_reader.py and Phase B.7's design review for why
direct reuse of cloud/approval_receiver/gmail_client.py was rejected). It
never imports from cloud.approval_receiver and A1 is never modified or
even aware this service exists.

All approval-command classification, validation, and idempotency decisions
are delegated entirely to scripts.approval_domain and
scripts.a2_dispatch.process_gmail_event -- this module duplicates none of
that logic. The dispatch_adapter passed to process_gmail_event is
scripts.a2_dispatch.FakeDispatchAdapter, used as-is (not a new class): it
already does exactly what shadow mode needs -- record a decision without
ever performing a real network call -- so no new "shadow adapter" needed
to be invented.

PRODUCTION_STATE_GAP: process_gmail_event() requires a production_snapshot
(the current automation_state/*.json content: active_issue_date,
current_state, current_command, active_design_batch_id) to correctly
detect staleness and no-ops. In real GitHub Actions workflows this comes
from a git checkout; this standalone Cloud Run service has none. This
module defines ProductionStateReader as an injected Protocol and ships
only FakeProductionStateReader (for tests). How a real deployment resolves
this (a read-only GitHub Contents API call, some other sync mechanism, or
accepting degraded staleness/no-op detection) is an explicitly unresolved
open question -- see docs/phase3b2/A2_SHADOW_RUNBOOK.md -- and is not
invented here to avoid guessing at a design that has not been reviewed.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

try:
    from .gmail_reader import (
        DecodedMessageFields,
        GmailReader,
        GmailReaderError,
        HistoryBatch,
        PermanentGmailError,
        StaleHistoryError,
        TemporaryGmailError,
        build_gmail_reader_from_env,
        decode_message_fields,
    )
    from .storage import (
        CursorState,
        CursorStore,
        FirestoreDispatcherStorage,
        build_shadow_record,
        utc_now_iso,
    )
except ImportError:  # pragma: no cover - direct Cloud Run module loading
    from gmail_reader import (  # type: ignore
        DecodedMessageFields,
        GmailReader,
        GmailReaderError,
        HistoryBatch,
        PermanentGmailError,
        StaleHistoryError,
        TemporaryGmailError,
        build_gmail_reader_from_env,
        decode_message_fields,
    )
    from storage import (  # type: ignore
        CursorState,
        CursorStore,
        FirestoreDispatcherStorage,
        build_shadow_record,
        utc_now_iso,
    )

try:
    from scripts.a2_dispatch import (
        A2Decision,
        A2Outcome,
        FakeDispatchAdapter,
        FetchedGmailMessage,
        MessageDedupeStore,
        TransitionLedger,
        process_gmail_event,
        _observation_id,
    )
    from scripts.approval_domain import ApprovalStage
except ModuleNotFoundError:  # pragma: no cover - direct script invocation
    from a2_dispatch import (  # type: ignore
        A2Decision,
        A2Outcome,
        FakeDispatchAdapter,
        FetchedGmailMessage,
        MessageDedupeStore,
        TransitionLedger,
        process_gmail_event,
        _observation_id,
    )
    from approval_domain import ApprovalStage  # type: ignore


LOGGER = logging.getLogger("approval_dispatcher")

# Strict allowlist logging: only these keys are ever emitted. Never the
# raw email body, subject, sender address, or any credential/token.
_LOG_ALLOWED_FIELDS = frozenset(
    {
        "observation_id",
        "stage",
        "classification",
        "issue_date",
        "transition_key_prefix",
        "result",
        "error_category",
        "from_allowlist_match",
        "recovery_path",
        "processed",
    }
)


class ConfigurationError(ValueError):
    """Deployment configuration is absent or malformed."""


class EnvelopeError(ValueError):
    """A Pub/Sub envelope or Gmail notification is malformed."""


class AuthenticationError(ValueError):
    """The signed caller identity cannot be verified."""


class CursorConflictError(RuntimeError):
    """A cursor compare_and_update_cursor() write lost to a conflicting
    concurrent update that did not resolve to a benign already-advanced
    state (see DispatcherService._apply_cursor_cas). Must never be
    silently treated as a successful ACK. Subclasses RuntimeError so it
    is retried (HTTP 500, bounded by Pub/Sub maxDeliveryAttempts/
    dead-letter) via create_app's existing
    (TemporaryGmailError, GmailReaderError, RuntimeError) route branch,
    with no new route-handling code required."""


def _log_event(event: str, fields: Mapping[str, Any]) -> None:
    safe = {key: value for key, value in fields.items() if key in _LOG_ALLOWED_FIELDS}
    LOGGER.info(json.dumps({"event": event, **safe}, sort_keys=True, default=str))


@dataclass(frozen=True)
class DispatcherConfig:
    mailbox_identity: str
    allowed_senders: frozenset[str]
    gate_a_subject_pattern: str
    design_subject_pattern: str
    oidc_expected_audience: str
    oidc_expected_callers: frozenset[str]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DispatcherConfig":
        source: Mapping[str, str] = env if env is not None else os.environ

        def required(name: str) -> str:
            value = str(source.get(name, "")).strip()
            if not value:
                raise ConfigurationError(f"{name} is required.")
            return value

        return cls(
            mailbox_identity=required("A2_MAILBOX_IDENTITY"),
            allowed_senders=frozenset(
                item.strip().lower()
                for item in required("A2_ALLOWED_SENDERS").split(",")
                if item.strip()
            ),
            gate_a_subject_pattern=required("A2_GATE_A_SUBJECT_PATTERN"),
            design_subject_pattern=required("A2_DESIGN_SUBJECT_PATTERN"),
            oidc_expected_audience=required("A2_OIDC_EXPECTED_AUDIENCE"),
            oidc_expected_callers=frozenset(
                item.strip().lower()
                for item in required("A2_OIDC_EXPECTED_CALLERS").split(",")
                if item.strip()
            ),
        )


@dataclass(frozen=True)
class PubSubNotification:
    email_address: str
    history_id: str
    pubsub_message_id: str | None = None


def decode_pubsub_envelope(envelope: object) -> PubSubNotification:
    """Decode a standard Gmail-watch Pub/Sub push envelope.

    This is generic Pub/Sub wire-format decoding (a fixed protocol, not
    business logic); it duplicates none of scripts.approval_domain's
    approval-command parsing.
    """

    if not isinstance(envelope, Mapping):
        raise EnvelopeError("Pub/Sub envelope must be a JSON object.")
    message = envelope.get("message")
    if not isinstance(message, Mapping):
        raise EnvelopeError("Pub/Sub envelope is missing 'message'.")
    data = message.get("data")
    if not isinstance(data, str) or not data:
        raise EnvelopeError("Pub/Sub message has no data payload.")
    try:
        payload = json.loads(base64.b64decode(data))
    except (ValueError, TypeError) as exc:
        raise EnvelopeError("Pub/Sub message data is not valid base64 JSON.") from exc
    if not isinstance(payload, Mapping):
        raise EnvelopeError("Decoded Gmail notification must be a JSON object.")
    email_address = str(payload.get("emailAddress", "")).strip()
    history_id = str(payload.get("historyId", "")).strip()
    if not email_address or not history_id:
        raise EnvelopeError("Gmail notification is missing emailAddress/historyId.")
    return PubSubNotification(
        email_address=email_address,
        history_id=history_id,
        pubsub_message_id=str(message.get("messageId", "")).strip() or None,
    )


def _history_not_newer(candidate: str, current: str) -> bool:
    try:
        return int(candidate) <= int(current)
    except ValueError:
        return False


class PushAuthenticator:
    """Verifies the Authorization header of an incoming Pub/Sub push
    request.

    token_verifier is an injected dependency so tests never need
    google-auth installed or a real network call; a production deployment
    wires in a real OIDC verification function (e.g.
    google.oauth2.id_token.verify_oauth2_token). The raw bearer token is
    never logged.
    """

    def __init__(
        self,
        *,
        expected_audience: str,
        expected_callers: frozenset[str],
        token_verifier: Callable[[str, str], Mapping[str, object]],
    ) -> None:
        self._expected_audience = expected_audience
        self._expected_callers = expected_callers
        self._token_verifier = token_verifier

    def verify(self, authorization_header: str | None) -> None:
        if not authorization_header or not authorization_header.startswith("Bearer "):
            raise AuthenticationError("Missing or malformed Authorization header.")
        token = authorization_header[len("Bearer ") :]
        try:
            claims = self._token_verifier(token, self._expected_audience)
        except Exception as exc:  # noqa: BLE001 - any verification failure is fatal
            raise AuthenticationError("Token verification failed.") from exc
        if not claims.get("email_verified"):
            raise AuthenticationError("Caller email is not verified.")
        email = str(claims.get("email", "")).strip().lower()
        if email not in self._expected_callers:
            raise AuthenticationError("Caller is not an authorized push identity.")


class ProductionStateReader(Protocol):
    def read_snapshot(self, stage: ApprovalStage) -> Mapping[str, object]: ...


class FakeProductionStateReader:
    """Test double. See module docstring's PRODUCTION_STATE_GAP: no real
    implementation exists yet."""

    def __init__(
        self, snapshots: Mapping[ApprovalStage, Mapping[str, object]] | None = None
    ) -> None:
        self._snapshots = dict(snapshots or {})

    def read_snapshot(self, stage: ApprovalStage) -> Mapping[str, object]:
        return self._snapshots.get(stage, {})


class DispatcherService:
    """Transport processor with no production or GitHub capabilities.

    Owns exactly one thing beyond classification: its own independent
    Gmail history cursor. It never writes Daily Duck production state,
    never commits to git, never calls the GitHub API, and never sends
    email.
    """

    def __init__(
        self,
        config: DispatcherConfig,
        gmail: GmailReader,
        cursor_store: CursorStore,
        observation_store: MessageDedupeStore,
        transition_ledger: TransitionLedger,
        state_reader: ProductionStateReader,
        *,
        clock: Callable[[], str] = utc_now_iso,
    ) -> None:
        self.config = config
        self.gmail = gmail
        self.cursor_store = cursor_store
        self.observation_store = observation_store
        self.transition_ledger = transition_ledger
        self.state_reader = state_reader
        self.clock = clock

    def process_pubsub(self, envelope: object) -> dict[str, Any]:
        notification = decode_pubsub_envelope(envelope)
        if notification.email_address != self.config.mailbox_identity:
            raise EnvelopeError("Gmail notification mailbox mismatch.")
        return self._process_to_history(notification.history_id)

    def _process_to_history(self, target_history_id: str) -> dict[str, Any]:
        cursor = self.cursor_store.read_cursor()
        current = cursor.processing_history_id

        if current is None:
            # First run: start tracking forward from here. Shadow mode
            # does not attempt to backfill messages predating A2's first
            # observed history id -- A2 is a comparison/validation tool,
            # not the system of record, so perfect backfill is not
            # required the way it would be for A1.
            return self._apply_cursor_cas(
                None, target_history_id, recovery_path="INITIAL_CURSOR", processed=0
            )

        if _history_not_newer(target_history_id, current):
            result = {
                "status": "ACKNOWLEDGED",
                "recovery_path": "DUPLICATE_NOTIFICATION",
                "processed": 0,
            }
            _log_event("a2_history_processed", result)
            return result

        try:
            batch = self.gmail.list_history(current)
        except StaleHistoryError:
            return self._apply_cursor_cas(
                current,
                target_history_id,
                recovery_path="STALE_HISTORY_RESYNC",
                processed=0,
            )

        processed = self._process_messages(batch.message_ids)
        return self._apply_cursor_cas(
            current, target_history_id, recovery_path="PUSH", processed=processed
        )

    def _apply_cursor_cas(
        self,
        expected_history_id: str | None,
        target_history_id: str,
        *,
        recovery_path: str,
        processed: int,
    ) -> dict[str, Any]:
        """Apply the cursor compare_and_update_cursor() result explicitly.

        A False result is never silently treated as success. It is first
        checked against a re-read of the current cursor: if that re-read
        proves another worker already advanced the cursor to
        target_history_id or beyond, this is a benign concurrent
        completion (this call's own message-level writes, if any, already
        happened via _process_messages before this CAS attempt, so they
        are already idempotently persisted regardless of which worker's
        CAS "won"). Any other outcome -- the cursor still behind, or a
        malformed/unparseable history id that cannot be proven to be at or
        beyond target -- is a genuine, not-yet-resolved concurrency
        conflict: raise CursorConflictError so the caller retries rather
        than acknowledging a cursor advance that did not actually happen.

        Does not rely on the CPython GIL or on a single Cloud Run
        instance -- the only trust placed in the cursor store is its own
        compare_and_update_cursor() atomicity contract, and the fallback
        re-read/compare below is safe even if that store is shared across
        many concurrent instances.
        """

        if self.cursor_store.compare_and_update_cursor(
            expected_history_id, target_history_id
        ):
            result = {
                "status": "ACKNOWLEDGED",
                "recovery_path": recovery_path,
                "processed": processed,
            }
            _log_event("a2_history_processed", result)
            return result

        refreshed = self.cursor_store.read_cursor().processing_history_id
        if refreshed is not None and _history_not_newer(target_history_id, refreshed):
            result = {
                "status": "ACKNOWLEDGED",
                "recovery_path": "BENIGN_CONCURRENT_ADVANCE",
                "processed": processed,
            }
            _log_event("a2_history_processed", result)
            return result

        raise CursorConflictError(
            "Gmail history cursor CAS failed and did not resolve to a benign "
            "concurrent advance."
        )

    def _process_messages(self, message_ids: tuple[str, ...]) -> int:
        count = 0
        for message_id in message_ids:
            raw = self.gmail.get_message(message_id)
            fields = decode_message_fields(raw)
            message = FetchedGmailMessage(
                gmail_message_id=message_id,
                sender=fields.sender,
                subject=fields.subject,
                body=fields.body,
            )
            self._classify_and_record(message)
            count += 1
        return count

    def _resolve_stage_guess(self, subject: str) -> ApprovalStage | None:
        if self.config.gate_a_subject_pattern in subject:
            return ApprovalStage.GATE_A
        if self.config.design_subject_pattern in subject:
            return ApprovalStage.DESIGN_SELECTION
        return None

    def _classify_and_record(self, message: FetchedGmailMessage) -> A2Outcome:
        stage_guess = self._resolve_stage_guess(message.subject)
        snapshot = (
            self.state_reader.read_snapshot(stage_guess)
            if stage_guess is not None
            else {}
        )

        outcome = process_gmail_event(
            message,
            mailbox_identity=self.config.mailbox_identity,
            allowed_senders=self.config.allowed_senders,
            gate_a_subject_pattern=self.config.gate_a_subject_pattern,
            design_subject_pattern=self.config.design_subject_pattern,
            production_snapshot=snapshot,
            message_dedupe_store=self.observation_store,
            transition_ledger=self.transition_ledger,
            dispatch_adapter=FakeDispatchAdapter(),
        )

        observation_id = _observation_id(
            self.config.mailbox_identity, message.gmail_message_id
        )
        command = outcome.command
        _log_event(
            "a2_shadow_classified",
            {
                "observation_id": observation_id,
                "stage": command.stage.value if command is not None else None,
                "classification": outcome.classification.value,
                "issue_date": command.issue_date if command is not None else None,
                "transition_key_prefix": (
                    command.transition_key[:12] if command is not None else None
                ),
                "result": outcome.decision.value,
            },
        )

        if outcome.decision is not A2Decision.SKIPPED_DUPLICATE_EVENT:
            record = build_shadow_record(
                observation_id=observation_id,
                outcome=outcome,
                timestamp=self.clock(),
            )
            self.observation_store.write_shadow_record(observation_id, record)  # type: ignore[attr-defined]

        return outcome


def create_app(
    *,
    config: DispatcherConfig | None = None,
    gmail: GmailReader | None = None,
    cursor_store: CursorStore | None = None,
    observation_store: MessageDedupeStore | None = None,
    transition_ledger: TransitionLedger | None = None,
    state_reader: ProductionStateReader | None = None,
    authenticator: PushAuthenticator | None = None,
) -> Any:
    """Create the Flask adapter. Dependencies are loaded only at
    deployment; local tests always supply fakes explicitly."""

    try:
        from flask import Flask, jsonify, request
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("Flask dependency is unavailable.") from exc

    if gmail is None or cursor_store is None or observation_store is None:
        raise ConfigurationError(
            "gmail, cursor_store, and observation_store must be provided."
        )
    if transition_ledger is None or authenticator is None:
        raise ConfigurationError(
            "transition_ledger and authenticator must be provided."
        )

    actual_config = config or DispatcherConfig.from_env()
    actual_state_reader = state_reader or FakeProductionStateReader()
    service = DispatcherService(
        actual_config,
        gmail,
        cursor_store,
        observation_store,
        transition_ledger,
        actual_state_reader,
    )
    app = Flask(__name__)

    @app.post("/pubsub")
    def pubsub() -> Any:
        try:
            authenticator.verify(request.headers.get("Authorization"))
            result = service.process_pubsub(request.get_json(silent=True))
            return jsonify(result), 200
        except AuthenticationError as exc:
            return jsonify({"status": "REJECTED", "reason": str(exc)}), 401
        except EnvelopeError as exc:
            return jsonify({"status": "REJECTED", "reason": str(exc)}), 400
        except PermanentGmailError as exc:
            # Never retried automatically: acknowledging stops an
            # infinite Pub/Sub redelivery loop for a failure retrying
            # cannot fix. Still surfaced for a human/reconciliation via
            # the logged error_category.
            _log_event(
                "a2_permanent_failure",
                {"error_category": type(exc).__name__},
            )
            return jsonify({"status": "FAILED_PERMANENT", "reason": str(exc)}), 200
        except (TemporaryGmailError, GmailReaderError, RuntimeError) as exc:
            _log_event(
                "a2_temporary_failure",
                {"error_category": type(exc).__name__},
            )
            return jsonify({"status": "RETRY", "reason": str(exc)}), 500
        except Exception as exc:  # noqa: BLE001 - fail-safe: unknown errors retry, bounded by the subscription's maxDeliveryAttempts
            _log_event(
                "a2_unexpected_failure",
                {"error_category": type(exc).__name__},
            )
            return jsonify({"status": "RETRY", "reason": type(exc).__name__}), 500

    @app.get("/health")
    def health() -> Any:
        return jsonify({"status": "OK"}), 200

    return app


def create_app_from_env(
    *,
    gmail_reader_factory: Callable[[], GmailReader] | None = None,
    firestore_client_factory: Callable[[], Any] | None = None,
    token_verifier_factory: Callable[[], Callable[[str, str], Mapping[str, object]]]
    | None = None,
) -> Any:
    """Production wiring for the Dockerfile's CMD.

    KNOWN LIMITATION (PRODUCTION_STATE_GAP, see module docstring): this
    wires FakeProductionStateReader(), which always returns an empty
    snapshot. That is a fail-safe default, not a working implementation --
    an empty snapshot has no active_issue_date, so
    scripts.a2_dispatch.process_gmail_event will treat every Gate A/Design
    subject match as missing an active issue and reject it as INVALID
    rather than silently misclassifying it against stale or wrong state.
    Resolving this (a read-only GitHub Contents API read of
    automation_state/*.json, or another mechanism) is an explicit
    prerequisite for A2_SHADOW_RUNBOOK.md's real deployment Human Gate,
    not something this module invents on its own.

    The three *_factory parameters are a minimal dependency-injection seam
    for local production-wiring tests only (see
    tests/test_approval_dispatcher.py's ProductionWiringTests): supplying
    all three replaces every external constructor (Gmail API service
    build, Firestore client, OIDC token verifier) with a controlled fake,
    so no google.cloud.firestore / googleapiclient / google.oauth2 import
    ever executes and no network call is possible. The real Dockerfile CMD
    always calls this with no arguments, so all three default to exactly
    the production builders this function used before the seam existed.
    """

    config = DispatcherConfig.from_env()
    gmail = (gmail_reader_factory or build_gmail_reader_from_env)()

    if firestore_client_factory is not None:
        firestore_client = firestore_client_factory()
    else:
        try:
            from google.cloud import firestore
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("Firestore dependency is unavailable.") from exc
        firestore_client = firestore.Client()

    storage = FirestoreDispatcherStorage(
        firestore_client,
        cursor_collection=os.environ.get("A2_CURSOR_COLLECTION", "a2_cursor"),
        cursor_document=os.environ.get("A2_CURSOR_DOCUMENT", "gmail_cursor"),
        observation_collection=os.environ.get(
            "A2_OBSERVATION_COLLECTION", "a2_shadow_observations"
        ),
        transition_collection=os.environ.get(
            "A2_TRANSITION_COLLECTION", "a2_transition_ledger"
        ),
    )

    if token_verifier_factory is not None:
        verify_token = token_verifier_factory()
    else:
        try:
            from google.oauth2 import id_token as google_id_token
            from google.auth.transport import requests as google_auth_requests
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("Google auth dependencies are unavailable.") from exc
        request_adapter = google_auth_requests.Request()

        def verify_token(token: str, audience: str) -> Mapping[str, object]:
            return google_id_token.verify_oauth2_token(
                token, request_adapter, audience
            )

    authenticator = PushAuthenticator(
        expected_audience=config.oidc_expected_audience,
        expected_callers=config.oidc_expected_callers,
        token_verifier=verify_token,
    )

    return create_app(
        config=config,
        gmail=gmail,
        cursor_store=storage,
        observation_store=storage,
        transition_ledger=storage,
        state_reader=FakeProductionStateReader(),
        authenticator=authenticator,
    )
