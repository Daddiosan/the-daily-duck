"""M3B thin relay: authenticated Gmail push to sanitized wake-up intent.

The relay does not parse approval commands and has no real GitHub adapter.
Its production-shaped route authenticates Pub/Sub, walks Gmail history,
fetches only Subject/From metadata, applies exact sender authorization, and
records DRY_RUN routing intent in the configured transactional ledger.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from email.utils import parseaddr
from enum import Enum
from typing import Any, Callable, Mapping

from .auth import AuthenticationError, PushAuthenticator, google_oidc_token_verifier
from .github_dispatch import (
    DISPATCH_REF,
    DispatchOutcome,
    FakeGitHubDispatcher,
    GitHubDispatcher,
)
from .gmail_reader import (
    GmailReader,
    GmailReaderError,
    LazyGmailReader,
    build_gmail_reader_from_env,
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
    mailbox_hash_for,
    notification_key_for,
    workflow_for_stage,
)
from .storage import (
    CursorStore,
    FirestoreRelayStorage,
    InMemoryCursorStore,
    build_firestore_storage,
)


LOGGER = logging.getLogger("approval_relay")
_LOG_ALLOWED_FIELDS = frozenset(
    {
        "event_key_prefix",
        "notification_key_prefix",
        "stage",
        "workflow",
        "status",
        "attempt_count",
        "mode",
        "from_allowlist_match",
        "processed",
        "error_category",
    }
)


def _log_event(event: str, fields: Mapping[str, Any]) -> None:
    safe = {key: value for key, value in fields.items() if key in _LOG_ALLOWED_FIELDS}
    LOGGER.info(json.dumps({"event": event, **safe}, sort_keys=True, default=str))


MAX_BUSINESS_ATTEMPTS = 4


class ConfigurationError(ValueError):
    """Required application configuration is missing or malformed."""


class EnvelopeError(ValueError):
    """The Pub/Sub envelope or decoded Gmail notification is malformed."""


class CursorConflictError(RuntimeError):
    """A cursor CAS lost and no completed concurrent advance explains it."""


class RelayMode(str, Enum):
    DRY_RUN = "DRY_RUN"
    LIVE = "LIVE"


class RelayStatus(str, Enum):
    DISPATCHED = "DISPATCHED"
    DRY_RUN_ROUTED = "DRY_RUN_ROUTED"
    ALREADY_IN_PROGRESS = "ALREADY_IN_PROGRESS"
    DUPLICATE_TERMINAL_NO_OP = "DUPLICATE_TERMINAL_NO_OP"
    UNRELATED_NO_DISPATCH = "UNRELATED_NO_DISPATCH"
    UNAUTHORIZED_NO_DISPATCH = "UNAUTHORIZED_NO_DISPATCH"
    AMBIGUOUS_NO_DISPATCH = "AMBIGUOUS_NO_DISPATCH"
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


@dataclass(frozen=True)
class PubSubNotification:
    email_address: str
    history_id: str


@dataclass(frozen=True)
class RelayConfig:
    mailbox_identity: str
    allowed_senders: frozenset[str]
    routing: RoutingConfig
    oidc_expected_issuer: str
    oidc_expected_audience: str
    oidc_expected_principals: frozenset[str]
    initial_history_id: str
    firestore_project: str
    mode: RelayMode = RelayMode.DRY_RUN

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RelayConfig":
        values = os.environ if env is None else env

        def required(name: str) -> str:
            value = str(values.get(name, "")).strip()
            if not value:
                raise ConfigurationError(f"{name} is required.")
            return value

        mailbox = _configured_address(required("RELAY_MAILBOX_IDENTITY"))
        senders = frozenset(
            _configured_address(item)
            for item in required("RELAY_ALLOWED_SENDERS").split(",")
        )
        principals = frozenset(
            _configured_address(item)
            for item in required("RELAY_OIDC_EXPECTED_PRINCIPALS").split(",")
        )
        initial_history_id = required("RELAY_GMAIL_INITIAL_HISTORY_ID")
        if not initial_history_id.isascii() or not initial_history_id.isdecimal():
            raise ConfigurationError("RELAY_GMAIL_INITIAL_HISTORY_ID is malformed.")
        raw_mode = str(values.get("RELAY_MODE", RelayMode.DRY_RUN.value)).strip()
        if raw_mode != RelayMode.DRY_RUN.value:
            raise ConfigurationError("M3C production wiring permits DRY_RUN only.")
        # Validate secret presence now, but defer credential/client creation.
        required("RELAY_GMAIL_OAUTH_CLIENT_JSON")
        required("RELAY_GMAIL_OAUTH_REFRESH_TOKEN")
        return cls(
            mailbox_identity=mailbox,
            allowed_senders=senders,
            routing=RoutingConfig(
                gate_a_subject_pattern=required("RELAY_GATE_A_SUBJECT_PATTERN"),
                design_subject_pattern=required("RELAY_DESIGN_SUBJECT_PATTERN"),
            ),
            oidc_expected_issuer=required("RELAY_OIDC_EXPECTED_ISSUER"),
            oidc_expected_audience=required("RELAY_OIDC_EXPECTED_AUDIENCE"),
            oidc_expected_principals=principals,
            initial_history_id=initial_history_id,
            firestore_project=required("RELAY_FIRESTORE_PROJECT"),
        )


def _configured_address(value: object) -> str:
    text = str(value).strip().casefold()
    if (
        not text
        or "@" not in text
        or parseaddr(text, strict=True)[1].casefold() != text
    ):
        raise ConfigurationError("Configured email identity is malformed.")
    return text


def decode_pubsub_envelope(envelope: object) -> PubSubNotification:
    if not isinstance(envelope, Mapping):
        raise EnvelopeError("Pub/Sub envelope must be a JSON object.")
    message = envelope.get("message")
    if not isinstance(message, Mapping):
        raise EnvelopeError("Pub/Sub envelope is missing message.")
    data = message.get("data")
    if not isinstance(data, str) or not data:
        raise EnvelopeError("Pub/Sub message is missing data.")
    try:
        decoded = base64.b64decode(data, validate=True).decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise EnvelopeError("Pub/Sub data is not valid base64 JSON.") from exc
    if not isinstance(payload, Mapping):
        raise EnvelopeError("Decoded Gmail notification must be a JSON object.")
    raw_email_address = payload.get("emailAddress")
    history_id = payload.get("historyId")
    if not isinstance(raw_email_address, str) or not raw_email_address.strip():
        raise EnvelopeError("Gmail notification is missing emailAddress.")
    if (
        not isinstance(history_id, str)
        or not history_id
        or not history_id.isascii()
        or not history_id.isdecimal()
    ):
        raise EnvelopeError("Gmail notification is missing a valid historyId.")
    return PubSubNotification(
        email_address=raw_email_address.strip().casefold(), history_id=history_id
    )


class RelayService:
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
        if not event.from_allowlist_match:
            _log_event("relay_sender_rejected", {"from_allowlist_match": False})
            return RelayResult(
                RelayStatus.UNAUTHORIZED_NO_DISPATCH, RelayStage.UNRELATED, None
            )
        stage = classify_stage(event.subject, self.routing)
        if stage is RelayStage.UNRELATED:
            return RelayResult(RelayStatus.UNRELATED_NO_DISPATCH, stage, None)

        event_key = event_key_for(event.mailbox_identity, event.gmail_message_id)
        if stage is RelayStage.AMBIGUOUS:
            self.ledger.reserve_new(
                event_key, stage=stage.value, workflow=None, now=self.clock()
            )
            return RelayResult(
                RelayStatus.AMBIGUOUS_NO_DISPATCH, stage, None, event_key
            )

        workflow = workflow_for_stage(stage)
        assert workflow is not None
        created = self.ledger.reserve_new(
            event_key, stage=stage.value, workflow=workflow, now=self.clock()
        )
        existing = created if created is not None else self.ledger.get(event_key)
        assert existing is not None
        if self.mode is RelayMode.DRY_RUN:
            _log_event(
                "relay_dry_run_routed",
                {
                    "stage": stage.value,
                    "workflow": workflow,
                    "event_key_prefix": event_key[:12],
                    "mode": self.mode.value,
                    "from_allowlist_match": True,
                },
            )
            return RelayResult(
                RelayStatus.DRY_RUN_ROUTED,
                stage,
                workflow,
                event_key,
                existing.attempt_count,
            )

        attempt = self.ledger.begin_attempt(event_key, now=self.clock())
        if attempt is None:
            current = self.ledger.get(event_key)
            in_progress = bool(
                current and current.state is RelayLedgerState.DISPATCH_ATTEMPTING
            )
            return RelayResult(
                RelayStatus.ALREADY_IN_PROGRESS
                if in_progress
                else RelayStatus.DUPLICATE_TERMINAL_NO_OP,
                stage,
                workflow,
                event_key,
                current.attempt_count if current else None,
                current.workflow_run_id if current else None,
            )

        dispatch_result = self.dispatcher.dispatch(workflow=workflow, ref=DISPATCH_REF)
        if dispatch_result.outcome is DispatchOutcome.SUCCESS:
            next_state, status = RelayLedgerState.DISPATCH_CONFIRMED, RelayStatus.DISPATCHED
        elif dispatch_result.outcome is DispatchOutcome.CLEAR_RETRYABLE_FAILURE:
            if attempt.attempt_count < self.max_business_attempts:
                next_state, status = RelayLedgerState.SAFE_TO_RETRY, RelayStatus.SAFE_TO_RETRY
            else:
                next_state, status = RelayLedgerState.FAILED_FINAL, RelayStatus.FAILED_FINAL
        elif dispatch_result.outcome is DispatchOutcome.CLEAR_FINAL_FAILURE:
            next_state, status = RelayLedgerState.FAILED_FINAL, RelayStatus.FAILED_FINAL
        else:
            next_state, status = RelayLedgerState.UNKNOWN_OUTCOME, RelayStatus.UNKNOWN_OUTCOME
        try:
            final = self.ledger.set_state(
                event_key,
                expected_state=RelayLedgerState.DISPATCH_ATTEMPTING,
                next_state=next_state,
                now=self.clock(),
                workflow_run_id=dispatch_result.workflow_run_id,
            )
        except LedgerStateConflict:
            try:
                self.ledger.set_state(
                    event_key,
                    expected_state=RelayLedgerState.DISPATCH_ATTEMPTING,
                    next_state=RelayLedgerState.UNKNOWN_OUTCOME,
                    now=self.clock(),
                )
            except LedgerStateConflict:
                pass
            return RelayResult(
                RelayStatus.CRITICAL_UNKNOWN_OUTCOME_UNRECORDED,
                stage,
                workflow,
                event_key,
                attempt.attempt_count,
            )
        return RelayResult(
            status,
            stage,
            workflow,
            event_key,
            final.attempt_count,
            final.workflow_run_id,
        )


class RelayIngressService:
    """Expand one Gmail notification and route every changed message."""

    def __init__(
        self,
        *,
        config: RelayConfig,
        gmail: GmailReader,
        cursor_store: CursorStore,
        relay: RelayService,
    ) -> None:
        self.config = config
        self.gmail = gmail
        self.cursor_store = cursor_store
        self.relay = relay

    def _read_or_initialize_cursor(self, mailbox_hash: str) -> str:
        persisted = self.cursor_store.read_cursor(mailbox_hash)
        if persisted is not None:
            return persisted.history_id
        if self.cursor_store.compare_and_update_cursor(
            mailbox_hash, None, self.config.initial_history_id
        ):
            return self.config.initial_history_id
        # A concurrent instance initialized first. Its persisted value wins;
        # the environment floor is never written over it.
        persisted = self.cursor_store.read_cursor(mailbox_hash)
        if persisted is None:
            raise CursorConflictError(
                "Relay cursor initialization lost without a persisted winner."
            )
        return persisted.history_id

    def process_pubsub(self, envelope: object) -> dict[str, Any]:
        notification = decode_pubsub_envelope(envelope)
        if notification.email_address != self.config.mailbox_identity:
            raise EnvelopeError("Gmail notification mailbox mismatch.")
        mailbox_hash = mailbox_hash_for(notification.email_address)
        notification_key = notification_key_for(
            notification.email_address, notification.history_id
        )
        start_history_id = self._read_or_initialize_cursor(mailbox_hash)
        if int(notification.history_id) <= int(start_history_id):
            return {"status": "DUPLICATE_NOTIFICATION", "processed": 0}

        batch = self.gmail.list_history(start_history_id)
        results: list[RelayResult] = []
        for message_id in batch.message_ids:
            metadata = self.gmail.get_message_metadata(message_id)
            allowed = (
                metadata.sender is not None
                and metadata.sender in self.config.allowed_senders
            )
            results.append(
                self.relay.process_event(
                    RelayInboundEvent(
                        mailbox_identity=self.config.mailbox_identity,
                        gmail_message_id=message_id,
                        subject=metadata.subject,
                        from_allowlist_match=allowed,
                    )
                )
            )
        if not self.cursor_store.compare_and_update_cursor(
            mailbox_hash, start_history_id, notification.history_id
        ):
            refreshed = self.cursor_store.read_cursor(mailbox_hash)
            if refreshed is None or int(refreshed.history_id) < int(
                notification.history_id
            ):
                raise CursorConflictError(
                    "Relay cursor CAS failed without a completed concurrent advance."
                )
        _log_event(
            "relay_notification_processed",
            {
                "notification_key_prefix": notification_key[:12],
                "processed": len(results),
                "mode": self.relay.mode.value,
            },
        )
        return {
            "status": "ACKNOWLEDGED",
            "processed": len(results),
            "results": [result.status.value for result in results],
        }


def create_app(
    *,
    config: RelayConfig | None = None,
    gmail: GmailReader | None = None,
    authenticator: PushAuthenticator | None = None,
    cursor_store: CursorStore | None = None,
    ledger: RelayLedger | None = None,
    dispatcher: GitHubDispatcher | None = None,
) -> Any:
    """Create an authenticated app; missing dependencies fail closed."""

    try:
        from flask import Flask, jsonify, request
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Flask dependency is unavailable.") from exc
    if config is None or gmail is None or authenticator is None:
        raise ConfigurationError("config, gmail, and authenticator are required.")
    actual_ledger = ledger or InMemoryRelayLedger()
    actual_dispatcher = dispatcher or FakeGitHubDispatcher()
    actual_cursor_store = cursor_store or InMemoryCursorStore()
    relay_service = RelayService(
        ledger=actual_ledger,
        dispatcher=actual_dispatcher,
        routing=config.routing,
        mode=config.mode,
    )
    ingress = RelayIngressService(
        config=config,
        gmail=gmail,
        cursor_store=actual_cursor_store,
        relay=relay_service,
    )
    app = Flask(__name__)
    app.extensions["relay_components"] = {
        "config": config,
        "gmail": gmail,
        "authenticator": authenticator,
        "cursor_store": actual_cursor_store,
        "ledger": actual_ledger,
        "dispatcher": actual_dispatcher,
        "relay": relay_service,
    }

    @app.post("/relay")
    def relay_route() -> Any:
        try:
            authenticator.verify(request.headers.get("Authorization"))
            result = ingress.process_pubsub(request.get_json(silent=True))
            return jsonify(result), 200
        except AuthenticationError as exc:
            return jsonify({"status": "REJECTED", "reason": str(exc)}), 401
        except EnvelopeError as exc:
            return jsonify({"status": "REJECTED", "reason": str(exc)}), 400
        except (GmailReaderError, RuntimeError) as exc:
            _log_event("relay_retryable_failure", {"error_category": type(exc).__name__})
            return jsonify({"status": "RETRY", "reason": type(exc).__name__}), 500
        except Exception as exc:  # noqa: BLE001 - fail closed and request redelivery
            _log_event("relay_unexpected_failure", {"error_category": type(exc).__name__})
            return jsonify({"status": "RETRY", "reason": type(exc).__name__}), 500

    @app.get("/health")
    def health() -> Any:
        return jsonify({"status": "OK"}), 200

    return app


def create_app_from_env(
    *,
    env: Mapping[str, str] | None = None,
    gmail_reader_factory: Callable[[], GmailReader] | None = None,
    firestore_client_factory: Callable[[], Any] | None = None,
    token_verifier_factory: Callable[[], Callable[[str, str], Mapping[str, object]]]
    | None = None,
) -> Any:
    """Construct production dependencies without a Firestore read or write."""

    values = os.environ if env is None else env
    config = RelayConfig.from_env(values)
    gmail_factory = gmail_reader_factory or (lambda: build_gmail_reader_from_env(values))
    gmail = LazyGmailReader(gmail_factory)
    if firestore_client_factory is not None:
        storage = FirestoreRelayStorage(firestore_client_factory())
    else:
        storage = build_firestore_storage(config.firestore_project)
    verifier = (token_verifier_factory or google_oidc_token_verifier)()
    authenticator = PushAuthenticator(
        expected_issuer=config.oidc_expected_issuer,
        expected_audience=config.oidc_expected_audience,
        expected_principals=config.oidc_expected_principals,
        token_verifier=verifier,
    )
    return create_app(
        config=config,
        gmail=gmail,
        authenticator=authenticator,
        cursor_store=storage,
        ledger=storage,
    )
