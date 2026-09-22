"""Authenticated, idempotent Gmail watch renewal for the R1 relay."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Protocol

from .router import mailbox_hash_for
from .storage import RelayStorageError, WatchStateStore


WATCH_LABEL_IDS = ("INBOX",)
WATCH_LABEL_FILTER_BEHAVIOR = "include"


class RenewalFailureKind(str, Enum):
    RETRYABLE = "GMAIL_RETRYABLE"
    NON_RETRYABLE = "GMAIL_NON_RETRYABLE"
    UNKNOWN = "GMAIL_UNKNOWN"
    MALFORMED_RESPONSE = "MALFORMED_GMAIL_RESPONSE"
    STORAGE = "STORAGE_FAILURE"


class GmailWatchCallError(RuntimeError):
    def __init__(self, kind: RenewalFailureKind) -> None:
        super().__init__(kind.value)
        self.kind = kind


class RenewalOperationError(RuntimeError):
    def __init__(self, kind: RenewalFailureKind, http_status: int) -> None:
        super().__init__(kind.value)
        self.kind = kind
        self.http_status = http_status


class GmailWatchClient(Protocol):
    def watch(self, topic_name: str) -> Mapping[str, Any]: ...


class RealGmailWatchClient:
    """One-request Gmail users.watch adapter; never shared between requests."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def watch(self, topic_name: str) -> Mapping[str, Any]:
        try:
            response = (
                self._service.users()
                .watch(
                    userId="me",
                    body={
                        "topicName": topic_name,
                        "labelIds": list(WATCH_LABEL_IDS),
                        "labelFilterBehavior": WATCH_LABEL_FILTER_BEHAVIOR,
                    },
                )
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 - classify without leaking details
            raise GmailWatchCallError(_classify_gmail_failure(exc)) from exc
        if not isinstance(response, Mapping):
            raise GmailWatchCallError(RenewalFailureKind.MALFORMED_RESPONSE)
        return response


def _classify_gmail_failure(exc: BaseException) -> RenewalFailureKind:
    response = getattr(exc, "resp", None)
    raw_status = getattr(response, "status", None)
    try:
        status = int(raw_status) if raw_status is not None else None
    except (TypeError, ValueError):
        status = None
    if status in {429, 500, 502, 503, 504}:
        return RenewalFailureKind.RETRYABLE
    if status in {400, 401, 403}:
        return RenewalFailureKind.NON_RETRYABLE
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return RenewalFailureKind.RETRYABLE
    return RenewalFailureKind.UNKNOWN


@dataclass(frozen=True)
class WatchRenewalConfig:
    mailbox_identity: str
    topic_name: str


def _normalize_watch_history_id(value: object) -> str:
    if isinstance(value, bool):
        raise RenewalOperationError(RenewalFailureKind.MALFORMED_RESPONSE, 400)
    if isinstance(value, int):
        if value < 0:
            raise RenewalOperationError(RenewalFailureKind.MALFORMED_RESPONSE, 400)
        return format(value, "d")
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or not value.isdecimal()
    ):
        raise RenewalOperationError(RenewalFailureKind.MALFORMED_RESPONSE, 400)
    return value


def _normalize_watch_expiration(value: object) -> int:
    if isinstance(value, bool):
        raise RenewalOperationError(RenewalFailureKind.MALFORMED_RESPONSE, 400)
    if isinstance(value, int):
        expiration = value
    elif (
        isinstance(value, str)
        and value
        and value.isascii()
        and value.isdecimal()
    ):
        expiration = int(value)
    else:
        raise RenewalOperationError(RenewalFailureKind.MALFORMED_RESPONSE, 400)
    if expiration <= 0:
        raise RenewalOperationError(RenewalFailureKind.MALFORMED_RESPONSE, 400)
    return expiration


class WatchRenewalService:
    def __init__(
        self,
        *,
        config: WatchRenewalConfig,
        gmail_factory: Callable[[], GmailWatchClient],
        state_store: WatchStateStore,
    ) -> None:
        self._config = config
        self._gmail_factory = gmail_factory
        self._state_store = state_store

    def renew(self) -> dict[str, object]:
        try:
            response = self._gmail_factory().watch(self._config.topic_name)
        except GmailWatchCallError as exc:
            if exc.kind in {
                RenewalFailureKind.NON_RETRYABLE,
                RenewalFailureKind.MALFORMED_RESPONSE,
            }:
                status = 400
            else:
                status = 503 if exc.kind is RenewalFailureKind.RETRYABLE else 500
            raise RenewalOperationError(exc.kind, status) from exc

        if not isinstance(response, Mapping):
            raise RenewalOperationError(RenewalFailureKind.MALFORMED_RESPONSE, 400)

        history_id = _normalize_watch_history_id(response.get("historyId"))
        expiration = _normalize_watch_expiration(response.get("expiration"))
        mailbox_hash = mailbox_hash_for(self._config.mailbox_identity)
        try:
            updated = self._state_store.store_watch_state_if_newer(
                mailbox_hash, history_id, expiration
            )
        except RelayStorageError as exc:
            raise RenewalOperationError(RenewalFailureKind.STORAGE, 500) from exc
        return {
            "status": "RENEWED" if updated else "RENEWED_STATE_RETAINED",
            "state_updated": updated,
        }
