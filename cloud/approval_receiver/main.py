"""Cloud Run HTTP receiver for Phase 3B-2A1 Gmail change observations.

Cloud Run IAM is the primary request authorization boundary.  The application
also validates the signed Google OIDC bearer token's audience and caller as a
defense in depth measure.  No caller-controlled identity header is trusted.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Any, Callable, Mapping

try:
    from .gmail_client import (
        GmailClientError,
        GmailReader,
        StaleHistoryError,
        build_gmail_client_from_env,
    )
    from .observation import (
        CursorState,
        ObservationStore,
        build_firestore_store,
        create_sanitized_observation,
    )
except ImportError:  # pragma: no cover - direct Cloud Run module loading
    from gmail_client import (  # type: ignore
        GmailClientError,
        GmailReader,
        StaleHistoryError,
        build_gmail_client_from_env,
    )
    from observation import (  # type: ignore
        CursorState,
        ObservationStore,
        build_firestore_store,
        create_sanitized_observation,
    )


LOGGER = logging.getLogger("approval_receiver")
HISTORY_ID_PATTERN = re.compile(r"^[0-9]+$")


class ConfigurationError(ValueError):
    """Deployment configuration is absent or malformed."""


class EnvelopeError(ValueError):
    """A Pub/Sub envelope or Gmail notification is malformed."""


class AuthenticationError(ValueError):
    """The signed caller identity cannot be verified."""


class ProcessingError(RuntimeError):
    """The receiver could not safely complete processing."""


@dataclass(frozen=True)
class ReceiverConfig:
    mailbox_identity: str
    allowed_senders: tuple[str, ...]
    subject_patterns: tuple[str, ...]
    pubsub_topic: str
    oidc_expected_audience: str
    oidc_expected_callers: tuple[str, ...]
    cursor_collection: str
    cursor_document: str
    observation_collection: str
    watch_renew_threshold_hours: int = 48
    full_resync_newer_than: str = "7d"
    full_resync_max_messages: int = 100

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "ReceiverConfig":
        values = os.environ if env is None else env

        def required(name: str) -> str:
            value = str(values.get(name, "")).strip()
            if not value:
                raise ConfigurationError(f"Missing required configuration: {name}")
            return value

        mailbox = required("GMAIL_MAILBOX_IDENTITY").lower()
        if parseaddr(mailbox)[1].lower() != mailbox or "@" not in mailbox:
            raise ConfigurationError("GMAIL_MAILBOX_IDENTITY is malformed.")

        sender_items = [
            item.strip().lower()
            for item in required("GMAIL_ALLOWED_SENDERS").split(",")
        ]
        if any(
            not item
            or parseaddr(item)[1].lower() != item
            or "@" not in item
            for item in sender_items
        ):
            raise ConfigurationError("GMAIL_ALLOWED_SENDERS is malformed.")

        subject_patterns = (
            required("GATE_A_SUBJECT_PATTERN"),
            required("DESIGN_SUBJECT_PATTERN"),
        )
        topic = required("GMAIL_PUBSUB_TOPIC")
        if not re.fullmatch(r"projects/[^/]+/topics/[^/]+", topic):
            raise ConfigurationError("GMAIL_PUBSUB_TOPIC is malformed.")

        callers = tuple(
            dict.fromkeys(
                item.strip().lower()
                for item in required("OIDC_EXPECTED_CALLERS").split(",")
                if item.strip()
            )
        )
        if not callers or any("@" not in item for item in callers):
            raise ConfigurationError("OIDC_EXPECTED_CALLERS is malformed.")

        try:
            threshold = int(values.get("WATCH_RENEW_THRESHOLD_HOURS", "48"))
            max_messages = int(values.get("FULL_RESYNC_MAX_MESSAGES", "100"))
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("Numeric receiver configuration is malformed.") from exc
        if threshold < 1 or max_messages < 1 or max_messages > 500:
            raise ConfigurationError("Numeric receiver configuration is out of range.")

        newer_than = str(values.get("FULL_RESYNC_NEWER_THAN", "7d")).strip()
        if not re.fullmatch(r"[1-9][0-9]*[dhm]", newer_than):
            raise ConfigurationError("FULL_RESYNC_NEWER_THAN is malformed.")

        return cls(
            mailbox_identity=mailbox,
            allowed_senders=tuple(dict.fromkeys(sender_items)),
            subject_patterns=subject_patterns,
            pubsub_topic=topic,
            oidc_expected_audience=required("OIDC_EXPECTED_AUDIENCE"),
            oidc_expected_callers=callers,
            cursor_collection=required("FIRESTORE_CURSOR_COLLECTION"),
            cursor_document=required("FIRESTORE_CURSOR_DOCUMENT"),
            observation_collection=required("FIRESTORE_OBSERVATION_COLLECTION"),
            watch_renew_threshold_hours=threshold,
            full_resync_newer_than=newer_than,
            full_resync_max_messages=max_messages,
        )


@dataclass(frozen=True)
class GmailNotification:
    email_address: str
    history_id: str
    pubsub_message_id: str | None = None


def decode_pubsub_envelope(envelope: object) -> GmailNotification:
    """Validate and decode only emailAddress/historyId from Pub/Sub data."""

    if not isinstance(envelope, dict):
        raise EnvelopeError("Pub/Sub envelope must be an object.")
    message = envelope.get("message")
    if not isinstance(message, dict):
        raise EnvelopeError("Pub/Sub message is missing.")
    data = message.get("data")
    if not isinstance(data, str) or not data:
        raise EnvelopeError("Pub/Sub message data is missing.")
    try:
        decoded = base64.b64decode(data, validate=True).decode("utf-8")
        notification = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvelopeError("Pub/Sub message data is malformed.") from exc
    if not isinstance(notification, dict):
        raise EnvelopeError("Gmail notification must be an object.")
    email_address = notification.get("emailAddress")
    history_id = notification.get("historyId")
    if not isinstance(email_address, str) or not email_address.strip():
        raise EnvelopeError("Gmail notification emailAddress is missing.")
    history_text = str(history_id).strip() if history_id is not None else ""
    if not HISTORY_ID_PATTERN.fullmatch(history_text):
        raise EnvelopeError("Gmail notification historyId is missing or invalid.")
    message_id = message.get("messageId")
    return GmailNotification(
        email_address=email_address.strip().lower(),
        history_id=history_text,
        pubsub_message_id=(
            str(message_id).strip() if message_id is not None else None
        )
        or None,
    )


class OIDCVerifier:
    """Cryptographically verify the Google-signed bearer assertion."""

    def __init__(
        self,
        expected_audience: str,
        expected_callers: tuple[str, ...],
        token_validator: Callable[[str, str], Mapping[str, Any]] | None = None,
    ) -> None:
        self._audience = expected_audience
        self._callers = frozenset(item.lower() for item in expected_callers)
        self._token_validator = token_validator or self._google_validator

    @staticmethod
    def _google_validator(token: str, audience: str) -> Mapping[str, Any]:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import id_token
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise AuthenticationError("Google auth dependency is unavailable.") from exc
        try:
            return id_token.verify_oauth2_token(token, Request(), audience)
        except Exception as exc:
            raise AuthenticationError("OIDC token verification failed.") from exc

    def verify(self, authorization: str | None) -> str:
        if not authorization:
            raise AuthenticationError("Bearer token is missing.")
        scheme, separator, token = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token.strip():
            raise AuthenticationError("Bearer token is malformed.")
        claims = self._token_validator(token.strip(), self._audience)
        if str(claims.get("aud", "")) != self._audience:
            raise AuthenticationError("OIDC audience mismatch.")
        caller = str(claims.get("email", "")).strip().lower()
        verified = claims.get("email_verified")
        if verified not in (True, "true", "True"):
            raise AuthenticationError("OIDC caller email is not verified.")
        if caller not in self._callers:
            raise AuthenticationError("OIDC caller is not authorized.")
        return caller


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _history_not_newer(candidate: str, current: str) -> bool:
    try:
        return int(candidate) <= int(current)
    except ValueError:
        return candidate == current


def _quote_search(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ReceiverService:
    """Transport processor with no production or GitHub capabilities."""

    def __init__(
        self,
        config: ReceiverConfig,
        gmail: GmailReader,
        store: ObservationStore,
        *,
        clock: Callable[[], str] = _iso_now,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.gmail = gmail
        self.store = store
        self.clock = clock
        self.now = now or (lambda: datetime.now(timezone.utc))

    def process_pubsub(self, envelope: object) -> dict[str, Any]:
        received_at = self.clock()
        notification = decode_pubsub_envelope(envelope)
        if notification.email_address != self.config.mailbox_identity:
            raise EnvelopeError("Gmail notification mailbox mismatch.")
        result = self._process_to_history(
            notification.history_id,
            notification_received_at=received_at,
            recovery_path="PUSH",
        )
        result["pubsub_message_id"] = notification.pubsub_message_id
        return result

    def _process_to_history(
        self,
        target_history_id: str,
        *,
        notification_received_at: str,
        recovery_path: str,
    ) -> dict[str, Any]:
        cursor = self.store.read_cursor()
        current = cursor.processing_history_id
        if current is None:
            return self._full_resync(notification_received_at)
        if _history_not_newer(target_history_id, current):
            return {
                "status": "ACKNOWLEDGED",
                "recovery_path": recovery_path,
                "observations_inserted": 0,
                "duplicates": 0,
                "duplicate_notification": True,
                "processing_history_id": current,
            }
        try:
            batch = self.gmail.list_history(current)
        except StaleHistoryError:
            return self._full_resync(notification_received_at)

        inserted, duplicates = self._observe_messages(
            batch.message_ids,
            history_id=target_history_id,
            notification_received_at=notification_received_at,
            recovery_path=recovery_path,
        )
        if not self.store.compare_and_update_cursor(current, target_history_id):
            latest = self.store.read_cursor().processing_history_id
            if latest is None or not _history_not_newer(target_history_id, latest):
                raise ProcessingError("Processing cursor compare-and-set failed.")
        return {
            "status": "ACKNOWLEDGED",
            "recovery_path": recovery_path,
            "observations_inserted": inserted,
            "duplicates": duplicates,
            "duplicate_notification": False,
            "processing_history_id": target_history_id,
        }

    def _observe_messages(
        self,
        message_ids: tuple[str, ...],
        *,
        history_id: str,
        notification_received_at: str,
        recovery_path: str,
    ) -> tuple[int, int]:
        inserted = 0
        duplicates = 0
        for message_id in message_ids:
            message = self.gmail.get_message(message_id)
            observation = create_sanitized_observation(
                message,
                mailbox_identity=self.config.mailbox_identity,
                allowed_senders=self.config.allowed_senders,
                subject_patterns=self.config.subject_patterns,
                history_id=history_id,
                notification_received_at=notification_received_at,
                recovery_path=recovery_path,
                clock=self.clock,
            )
            if self.store.insert_observation_if_absent(
                observation["observation_id"], observation
            ):
                inserted += 1
            else:
                duplicates += 1
        return inserted, duplicates

    def _bounded_recovery_query(self) -> str:
        senders = " ".join(
            f"from:{_quote_search(sender)}"
            for sender in self.config.allowed_senders
        )
        subjects = " ".join(
            f"subject:{_quote_search(pattern)}"
            for pattern in self.config.subject_patterns
        )
        return (
            f"{{{senders}}} {{{subjects}}} "
            f"newer_than:{self.config.full_resync_newer_than}"
        )

    def _full_resync(self, notification_received_at: str) -> dict[str, Any]:
        profile = self.gmail.get_profile()
        profile_history_id = str(profile.get("historyId", "")).strip()
        if not HISTORY_ID_PATTERN.fullmatch(profile_history_id):
            raise ProcessingError("Gmail profile historyId is missing or invalid.")
        cursor = self.store.read_cursor()
        message_ids = self.gmail.list_messages(
            self._bounded_recovery_query(),
            self.config.full_resync_max_messages,
        )
        inserted, duplicates = self._observe_messages(
            message_ids,
            history_id=profile_history_id,
            notification_received_at=notification_received_at,
            recovery_path="FULL_RESYNC",
        )
        if not self.store.compare_and_update_cursor(
            cursor.processing_history_id, profile_history_id
        ):
            latest = self.store.read_cursor().processing_history_id
            if latest is None or not _history_not_newer(profile_history_id, latest):
                raise ProcessingError("Full-resync cursor compare-and-set failed.")
        return {
            "status": "ACKNOWLEDGED",
            "recovery_path": "FULL_RESYNC",
            "observations_inserted": inserted,
            "duplicates": duplicates,
            "duplicate_notification": False,
            "processing_history_id": profile_history_id,
        }

    def maintenance(self) -> dict[str, Any]:
        received_at = self.clock()
        profile = self.gmail.get_profile()
        target = str(profile.get("historyId", "")).strip()
        if not HISTORY_ID_PATTERN.fullmatch(target):
            raise ProcessingError("Gmail profile historyId is missing or invalid.")
        catchup = self._process_to_history(
            target,
            notification_received_at=received_at,
            recovery_path="CATCHUP",
        )

        cursor = self.store.read_cursor()
        threshold = self.now() + timedelta(
            hours=self.config.watch_renew_threshold_hours
        )
        renew = cursor.watch_expiration_ms is None or (
            cursor.watch_expiration_ms
            <= int(threshold.timestamp() * 1000)
        )
        renewed = False
        if renew:
            watch = self.gmail.watch(self.config.pubsub_topic)
            watch_history_id = str(watch.get("historyId", "")).strip()
            try:
                expiration_ms = int(watch.get("expiration"))
            except (TypeError, ValueError) as exc:
                raise ProcessingError("Gmail watch response is malformed.") from exc
            if not HISTORY_ID_PATTERN.fullmatch(watch_history_id):
                raise ProcessingError("Gmail watch historyId is malformed.")
            self.store.update_watch(watch_history_id, expiration_ms)
            renewed = True
        return {
            "status": "OK",
            "catchup": catchup,
            "watch_renewed": renewed,
            "processing_history_id": self.store.read_cursor().processing_history_id,
        }


def _log_result(event: str, result: Mapping[str, Any]) -> None:
    allowed = {
        "status": result.get("status"),
        "recovery_path": result.get("recovery_path"),
        "observations_inserted": result.get("observations_inserted"),
        "duplicates": result.get("duplicates"),
        "duplicate_notification": result.get("duplicate_notification"),
        "processing_history_id": result.get("processing_history_id"),
    }
    LOGGER.info(json.dumps({"event": event, **allowed}, sort_keys=True))


def create_app(
    *,
    config: ReceiverConfig | None = None,
    gmail: GmailReader | None = None,
    store: ObservationStore | None = None,
    verifier: OIDCVerifier | None = None,
) -> Any:
    """Create the Flask adapter; dependencies are loaded only at deployment."""

    try:
        from flask import Flask, jsonify, request
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("Flask dependency is unavailable.") from exc

    actual_config = config or ReceiverConfig.from_env()
    actual_gmail = gmail or build_gmail_client_from_env()
    actual_store = store or build_firestore_store(
        cursor_collection=actual_config.cursor_collection,
        cursor_document=actual_config.cursor_document,
        observation_collection=actual_config.observation_collection,
    )
    actual_verifier = verifier or OIDCVerifier(
        actual_config.oidc_expected_audience,
        actual_config.oidc_expected_callers,
    )
    service = ReceiverService(actual_config, actual_gmail, actual_store)
    app = Flask(__name__)

    def authenticate() -> None:
        actual_verifier.verify(request.headers.get("Authorization"))

    @app.post("/pubsub")
    def pubsub() -> Any:
        try:
            authenticate()
            result = service.process_pubsub(request.get_json(silent=True))
            _log_result("pubsub_processed", result)
            return jsonify(result), 200
        except AuthenticationError as exc:
            return jsonify({"status": "REJECTED", "reason": str(exc)}), 401
        except EnvelopeError as exc:
            return jsonify({"status": "REJECTED", "reason": str(exc)}), 400
        except (GmailClientError, ProcessingError, RuntimeError) as exc:
            LOGGER.error(json.dumps({"event": "pubsub_error", "type": type(exc).__name__}))
            return jsonify({"status": "RETRY", "reason": type(exc).__name__}), 500

    @app.post("/maintenance")
    def maintenance() -> Any:
        try:
            authenticate()
            result = service.maintenance()
            _log_result("maintenance_completed", result.get("catchup", {}))
            return jsonify(result), 200
        except AuthenticationError as exc:
            return jsonify({"status": "REJECTED", "reason": str(exc)}), 401
        except (GmailClientError, ProcessingError, RuntimeError) as exc:
            LOGGER.error(json.dumps({"event": "maintenance_error", "type": type(exc).__name__}))
            return jsonify({"status": "RETRY", "reason": type(exc).__name__}), 500

    @app.get("/health")
    def health() -> Any:
        return jsonify({"status": "OK", "service": "approval-receiver-a1"}), 200

    return app
