"""Narrow GitHub App adapter for the Daily Duck approval relay.

This module owns the relay's entire outbound GitHub network surface. Targets
are application constants, installation tokens are short lived, and callers
cannot supply an owner, repository, endpoint, ref, or workflow input.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Callable, Mapping, Protocol

from .github_dispatch import (
    ALLOWED_WORKFLOWS,
    DISPATCH_REF,
    DispatchOutcome,
    DispatchResult,
    WorkflowNotAllowlistedError,
)


GITHUB_API_ROOT = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
GITHUB_OWNER = "Daddiosan"
GITHUB_REPOSITORY = "the-daily-duck"
GITHUB_ACCEPT = "application/vnd.github+json"
TOKEN_REFRESH_SKEW = timedelta(minutes=5)
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 15.0


class GitHubAppConfigurationError(ValueError):
    """GitHub App configuration is missing or malformed; contains no secret."""


class GitHubRequestNotSent(RuntimeError):
    """The transport proved that no HTTP connection was established."""


class GitHubRequestOutcomeUnknown(RuntimeError):
    """The transport cannot prove whether the request reached GitHub."""


class InstallationTokenRetryableError(RuntimeError):
    """Installation-token acquisition failed before any workflow dispatch."""


class InstallationTokenFinalError(RuntimeError):
    """Installation-token configuration or authorization is invalid."""


@dataclass(frozen=True)
class GitHubAppConfig:
    client_id: str
    installation_id: int
    private_key_pem: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GitHubAppConfig":
        values = os.environ if env is None else env

        def required(name: str) -> str:
            value = str(values.get(name, "")).strip()
            if not value:
                raise GitHubAppConfigurationError(f"{name} is required for LIVE mode.")
            return value

        client_id = required("RELAY_GITHUB_APP_CLIENT_ID")
        raw_installation_id = required("RELAY_GITHUB_APP_INSTALLATION_ID")
        if not raw_installation_id.isascii() or not raw_installation_id.isdecimal():
            raise GitHubAppConfigurationError(
                "RELAY_GITHUB_APP_INSTALLATION_ID must be a positive decimal integer."
            )
        installation_id = int(raw_installation_id)
        if installation_id <= 0:
            raise GitHubAppConfigurationError(
                "RELAY_GITHUB_APP_INSTALLATION_ID must be a positive decimal integer."
            )
        private_key_pem = required("RELAY_GITHUB_APP_PRIVATE_KEY")
        try:
            from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
            from cryptography.hazmat.primitives.serialization import load_pem_private_key

            private_key = load_pem_private_key(
                private_key_pem.encode("utf-8"), password=None
            )
            if not isinstance(private_key, RSAPrivateKey):
                raise ValueError("RSA private key required")
        except Exception as exc:  # noqa: BLE001 - sanitized fail-closed boundary
            raise GitHubAppConfigurationError(
                "RELAY_GITHUB_APP_PRIVATE_KEY is not a valid unencrypted PEM key."
            ) from exc
        return cls(
            client_id=client_id,
            installation_id=installation_id,
            private_key_pem=private_key_pem,
        )


@dataclass(frozen=True)
class GitHubHttpResponse:
    status_code: int
    headers: Mapping[str, str]
    json_body: object | None = field(repr=False)


class GitHubHttpTransport(Protocol):
    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> GitHubHttpResponse: ...


class RequestsGitHubHttpTransport:
    """One-shot HTTPS transport with redirects and internal retries disabled."""

    def __init__(self, session: Any | None = None) -> None:
        if session is None:
            try:
                import requests
            except ImportError as exc:  # pragma: no cover - container dependency
                raise GitHubAppConfigurationError(
                    "The requests dependency is unavailable."
                ) from exc
            session = requests.Session()
        self._session = session

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> GitHubHttpResponse:
        try:
            import requests

            response = self._session.post(
                url,
                headers=dict(headers),
                json=dict(payload),
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                allow_redirects=False,
            )
        except requests.ConnectTimeout as exc:
            raise GitHubRequestNotSent("GitHub connection timed out before send.") from exc
        except requests.RequestException as exc:
            raise GitHubRequestOutcomeUnknown("GitHub request outcome is unknown.") from exc
        try:
            body: object | None = response.json()
        except ValueError:
            body = None
        return GitHubHttpResponse(
            status_code=int(response.status_code),
            headers={str(k).lower(): str(v) for k, v in response.headers.items()},
            json_body=body,
        )


@dataclass(frozen=True)
class _CachedInstallationToken:
    value: str = field(repr=False)
    expires_at: datetime


class GitHubAppInstallationTokenProvider:
    def __init__(
        self,
        *,
        config: GitHubAppConfig,
        transport: GitHubHttpTransport,
        clock: Callable[[], datetime] | None = None,
        jwt_encoder: Callable[..., str] | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._jwt_encoder = jwt_encoder
        self._cached: _CachedInstallationToken | None = None
        self._lock = Lock()

    def _app_jwt(self, now: datetime) -> str:
        encoder = self._jwt_encoder
        if encoder is None:
            try:
                import jwt
            except ImportError as exc:  # pragma: no cover - container dependency
                raise InstallationTokenFinalError(
                    "The JWT dependency is unavailable."
                ) from exc
            encoder = jwt.encode
        try:
            encoded = encoder(
                {
                    "iat": int((now - timedelta(seconds=60)).timestamp()),
                    "exp": int((now + timedelta(minutes=9)).timestamp()),
                    "iss": self._config.client_id,
                },
                self._config.private_key_pem,
                algorithm="RS256",
            )
        except Exception as exc:  # noqa: BLE001 - never include key/JWT in error
            raise InstallationTokenFinalError("GitHub App JWT generation failed.") from exc
        if not isinstance(encoded, str) or not encoded:
            raise InstallationTokenFinalError("GitHub App JWT generation failed.")
        return encoded

    def get_token(self) -> str:
        with self._lock:
            now = self._clock().astimezone(timezone.utc)
            if (
                self._cached is not None
                and now + TOKEN_REFRESH_SKEW < self._cached.expires_at
            ):
                return self._cached.value
            response = self._mint(now)
            self._cached = response
            return response.value

    def _mint(self, now: datetime) -> _CachedInstallationToken:
        app_jwt = self._app_jwt(now)
        url = (
            f"{GITHUB_API_ROOT}/app/installations/"
            f"{self._config.installation_id}/access_tokens"
        )
        try:
            response = self._transport.post_json(
                url=url,
                headers={
                    "Accept": GITHUB_ACCEPT,
                    "Authorization": f"Bearer {app_jwt}",
                    "X-GitHub-Api-Version": GITHUB_API_VERSION,
                    "User-Agent": "daily-duck-approval-relay",
                },
                payload={
                    "repositories": [GITHUB_REPOSITORY],
                    "permissions": {"actions": "write"},
                },
            )
        except (GitHubRequestNotSent, GitHubRequestOutcomeUnknown) as exc:
            raise InstallationTokenRetryableError(
                "GitHub installation-token request failed."
            ) from exc
        if response.status_code == 429 or (
            response.status_code == 403
            and (
                "retry-after" in response.headers
                or response.headers.get("x-ratelimit-remaining") == "0"
            )
        ) or response.status_code >= 500:
            raise InstallationTokenRetryableError(
                "GitHub installation-token service is temporarily unavailable."
            )
        if response.status_code != 201 or not isinstance(response.json_body, Mapping):
            raise InstallationTokenFinalError(
                "GitHub installation-token request was rejected."
            )
        token = response.json_body.get("token")
        expires_at = response.json_body.get("expires_at")
        if not isinstance(token, str) or not token or not isinstance(expires_at, str):
            raise InstallationTokenFinalError(
                "GitHub installation-token response is malformed."
            )
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                raise ValueError("timezone required")
            expiry = expiry.astimezone(timezone.utc)
        except ValueError as exc:
            raise InstallationTokenFinalError(
                "GitHub installation-token expiry is malformed."
            ) from exc
        if expiry <= now:
            raise InstallationTokenFinalError(
                "GitHub installation token is already expired."
            )
        return _CachedInstallationToken(value=token, expires_at=expiry)


class RealGitHubDispatcher:
    """Dispatch exactly one fixed workflow request and classify its outcome."""

    def __init__(
        self,
        *,
        token_provider: GitHubAppInstallationTokenProvider,
        transport: GitHubHttpTransport,
    ) -> None:
        self._token_provider = token_provider
        self._transport = transport

    def dispatch(self, *, workflow: str, ref: str) -> DispatchResult:
        if workflow not in ALLOWED_WORKFLOWS:
            raise WorkflowNotAllowlistedError(
                f"workflow not allowlisted: {workflow!r}"
            )
        if ref != DISPATCH_REF:
            raise WorkflowNotAllowlistedError(f"ref not allowlisted: {ref!r}")
        try:
            token = self._token_provider.get_token()
        except InstallationTokenRetryableError:
            return DispatchResult(DispatchOutcome.CLEAR_RETRYABLE_FAILURE)
        except InstallationTokenFinalError:
            return DispatchResult(DispatchOutcome.CLEAR_FINAL_FAILURE)

        url = (
            f"{GITHUB_API_ROOT}/repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/"
            f"actions/workflows/{workflow}/dispatches"
        )
        try:
            response = self._transport.post_json(
                url=url,
                headers={
                    "Accept": GITHUB_ACCEPT,
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": GITHUB_API_VERSION,
                    "User-Agent": "daily-duck-approval-relay",
                },
                payload={"ref": DISPATCH_REF, "return_run_details": True},
            )
        except GitHubRequestNotSent:
            return DispatchResult(DispatchOutcome.CLEAR_RETRYABLE_FAILURE)
        except GitHubRequestOutcomeUnknown:
            return DispatchResult(DispatchOutcome.UNKNOWN_OUTCOME)

        status = response.status_code
        if status == 200 and isinstance(response.json_body, Mapping):
            run_id = response.json_body.get("workflow_run_id")
            if isinstance(run_id, int) and not isinstance(run_id, bool) and run_id > 0:
                return DispatchResult(DispatchOutcome.SUCCESS, str(run_id))
            if (
                isinstance(run_id, str)
                and run_id.isascii()
                and run_id.isdecimal()
                and int(run_id) > 0
            ):
                return DispatchResult(DispatchOutcome.SUCCESS, run_id)
            return DispatchResult(DispatchOutcome.UNKNOWN_OUTCOME)
        if status == 429 or (
            status == 403
            and (
                "retry-after" in response.headers
                or response.headers.get("x-ratelimit-remaining") == "0"
            )
        ):
            return DispatchResult(DispatchOutcome.CLEAR_RETRYABLE_FAILURE)
        if status == 408 or status >= 500 or 200 <= status < 300:
            return DispatchResult(DispatchOutcome.UNKNOWN_OUTCOME)
        return DispatchResult(DispatchOutcome.CLEAR_FINAL_FAILURE)


def build_github_dispatcher_from_env(
    env: Mapping[str, str] | None = None,
    *,
    transport: GitHubHttpTransport | None = None,
) -> RealGitHubDispatcher:
    config = GitHubAppConfig.from_env(env)
    actual_transport = transport or RequestsGitHubHttpTransport()
    token_provider = GitHubAppInstallationTokenProvider(
        config=config,
        transport=actual_transport,
    )
    return RealGitHubDispatcher(
        token_provider=token_provider,
        transport=actual_transport,
    )
