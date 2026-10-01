"""Authenticated external wake-up dispatcher for Daily Duck approvals.

The service never reads approval content.  Each route maps to one compile-time
workflow name, while the reused GitHub App adapter fixes the repository and ref.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from cloud.approval_relay.auth import (
    AuthenticationError,
    PushAuthenticator,
    google_oidc_token_verifier,
)
from cloud.approval_relay.github_app_dispatch import (
    GitHubAppConfigurationError,
    build_github_dispatcher_from_env,
)
from cloud.approval_relay.github_dispatch import (
    DESIGN_SELECTION_WORKFLOW,
    DISPATCH_REF,
    GATE_A_WORKFLOW,
    DispatchOutcome,
    GitHubDispatcher,
)


LOGGER = logging.getLogger("approval_fallback")
LOGGER.setLevel(logging.INFO)
EXPECTED_ISSUER = "https://accounts.google.com"


class FallbackConfigurationError(ValueError):
    """Configuration is absent or malformed; never contains a secret value."""


@dataclass(frozen=True)
class FallbackConfig:
    expected_audience: str
    expected_principals: frozenset[str]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "FallbackConfig":
        values = os.environ if env is None else env

        def required(name: str) -> str:
            value = str(values.get(name, "")).strip()
            if not value:
                raise FallbackConfigurationError(f"{name} is required.")
            return value

        principals = frozenset(
            item.strip().casefold()
            for item in required("FALLBACK_OIDC_EXPECTED_PRINCIPALS").split(",")
            if item.strip()
        )
        if not principals or any("@" not in item for item in principals):
            raise FallbackConfigurationError(
                "FALLBACK_OIDC_EXPECTED_PRINCIPALS is malformed."
            )
        return cls(
            expected_audience=required("FALLBACK_OIDC_EXPECTED_AUDIENCE"),
            expected_principals=principals,
        )


def _log(event: str, *, workflow: str, outcome: str) -> None:
    LOGGER.info(
        json.dumps(
            {"event": event, "workflow": workflow, "outcome": outcome},
            sort_keys=True,
        )
    )


def create_app(
    *,
    authenticator: PushAuthenticator,
    dispatcher: GitHubDispatcher,
) -> Any:
    try:
        from flask import Flask, jsonify, request
    except ImportError as exc:  # pragma: no cover - container dependency
        raise RuntimeError("Flask is unavailable.") from exc

    app = Flask(__name__)

    def wake(workflow: str) -> tuple[Any, int]:
        try:
            authenticator.verify(request.headers.get("Authorization"))
        except AuthenticationError:
            return jsonify({"status": "REJECTED", "reason": "UNAUTHORIZED"}), 401

        result = dispatcher.dispatch(workflow=workflow, ref=DISPATCH_REF)
        _log("fallback_dispatch_completed", workflow=workflow, outcome=result.outcome.value)
        if result.outcome is DispatchOutcome.SUCCESS:
            return (
                jsonify(
                    {
                        "status": "DISPATCHED",
                        "workflow": workflow,
                        "workflow_run_id": result.workflow_run_id,
                    }
                ),
                200,
            )
        if result.outcome is DispatchOutcome.CLEAR_RETRYABLE_FAILURE:
            return jsonify({"status": "RETRY", "reason": "GITHUB_RETRYABLE"}), 503
        if result.outcome is DispatchOutcome.UNKNOWN_OUTCOME:
            # Blind transport retries could duplicate an accepted dispatch.
            # Acknowledge this tick and let the next scheduled tick wake it again.
            return (
                jsonify({"status": "ACKNOWLEDGED", "reason": "UNKNOWN_OUTCOME"}),
                202,
            )
        return jsonify({"status": "FAILED", "reason": "GITHUB_FINAL"}), 500

    @app.post("/wake/gate-a")
    def wake_gate_a() -> tuple[Any, int]:
        return wake(GATE_A_WORKFLOW)

    @app.post("/wake/design-selection")
    def wake_design_selection() -> tuple[Any, int]:
        return wake(DESIGN_SELECTION_WORKFLOW)

    @app.get("/health")
    def health() -> tuple[Any, int]:
        return jsonify({"status": "OK"}), 200

    return app


def create_app_from_env(
    *,
    env: Mapping[str, str] | None = None,
    token_verifier_factory: Callable[
        [], Callable[[str, str], Mapping[str, object]]
    ]
    | None = None,
    dispatcher_factory: Callable[[Mapping[str, str]], GitHubDispatcher] | None = None,
) -> Any:
    values = os.environ if env is None else env
    config = FallbackConfig.from_env(values)
    verifier = (token_verifier_factory or google_oidc_token_verifier)()
    authenticator = PushAuthenticator(
        expected_issuer=EXPECTED_ISSUER,
        expected_audience=config.expected_audience,
        expected_principals=config.expected_principals,
        token_verifier=verifier,
    )
    try:
        dispatcher = (dispatcher_factory or build_github_dispatcher_from_env)(values)
    except GitHubAppConfigurationError as exc:
        raise FallbackConfigurationError(str(exc)) from exc
    return create_app(authenticator=authenticator, dispatcher=dispatcher)
