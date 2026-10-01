from __future__ import annotations

import logging
import unittest

from cloud.approval_fallback.main import (
    EXPECTED_ISSUER,
    FallbackConfig,
    FallbackConfigurationError,
    create_app,
    create_app_from_env,
)
from cloud.approval_relay.auth import PushAuthenticator
from cloud.approval_relay.github_dispatch import (
    DESIGN_SELECTION_WORKFLOW,
    DISPATCH_REF,
    GATE_A_WORKFLOW,
    DispatchOutcome,
    DispatchResult,
)


AUDIENCE = "https://fallback.example.run.app"
PRINCIPAL = "fallback-scheduler@example.iam.gserviceaccount.com"


class RecordingDispatcher:
    def __init__(self, outcome=DispatchOutcome.SUCCESS):
        self.outcome = outcome
        self.calls = []

    def dispatch(self, *, workflow, ref):
        self.calls.append((workflow, ref))
        run_id = "123" if self.outcome is DispatchOutcome.SUCCESS else None
        return DispatchResult(self.outcome, run_id)


def authenticator(principal=PRINCIPAL):
    def verify(_token, _audience):
        return {
            "iss": EXPECTED_ISSUER,
            "aud": AUDIENCE,
            "email_verified": True,
            "email": principal,
        }

    return PushAuthenticator(
        expected_issuer=EXPECTED_ISSUER,
        expected_audience=AUDIENCE,
        expected_principals=frozenset({PRINCIPAL}),
        token_verifier=verify,
    )


class ApprovalFallbackTests(unittest.TestCase):
    def client(self, dispatcher=None):
        dispatcher = dispatcher or RecordingDispatcher()
        app = create_app(authenticator=authenticator(), dispatcher=dispatcher)
        return app.test_client(), dispatcher

    def test_fixed_routes_dispatch_only_fixed_workflows_and_main(self):
        client, dispatcher = self.client()
        for route, workflow in (
            ("/wake/gate-a", GATE_A_WORKFLOW),
            ("/wake/design-selection", DESIGN_SELECTION_WORKFLOW),
        ):
            response = client.post(route, headers={"Authorization": "Bearer valid"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["status"], "DISPATCHED")
            self.assertEqual(dispatcher.calls[-1], (workflow, DISPATCH_REF))

    def test_request_body_cannot_override_workflow_repo_ref_or_url(self):
        client, dispatcher = self.client()
        response = client.post(
            "/wake/design-selection",
            headers={"Authorization": "Bearer valid"},
            json={
                "workflow": "evil.yml",
                "repository": "attacker/repo",
                "ref": "attacker-branch",
                "url": "https://attacker.invalid/",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            dispatcher.calls,
            [(DESIGN_SELECTION_WORKFLOW, DISPATCH_REF)],
        )
        self.assertEqual(
            client.post(
                "/wake/evil.yml", headers={"Authorization": "Bearer valid"}
            ).status_code,
            404,
        )

    def test_auth_failure_never_dispatches(self):
        client, dispatcher = self.client()
        response = client.post("/wake/gate-a")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(dispatcher.calls, [])

    def test_github_outcomes_have_bounded_transport_behavior(self):
        expectations = {
            DispatchOutcome.CLEAR_RETRYABLE_FAILURE: (503, "RETRY"),
            DispatchOutcome.CLEAR_FINAL_FAILURE: (500, "FAILED"),
            DispatchOutcome.UNKNOWN_OUTCOME: (202, "ACKNOWLEDGED"),
        }
        for outcome, (status, body_status) in expectations.items():
            with self.subTest(outcome=outcome):
                client, _ = self.client(RecordingDispatcher(outcome))
                response = client.post(
                    "/wake/gate-a", headers={"Authorization": "Bearer valid"}
                )
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.get_json()["status"], body_status)

    def test_health_has_no_dispatch_side_effect(self):
        client, dispatcher = self.client()
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(dispatcher.calls, [])

    def test_configuration_requires_audience_principal_and_github_app(self):
        with self.assertRaises(FallbackConfigurationError):
            FallbackConfig.from_env({})
        with self.assertRaises(FallbackConfigurationError):
            FallbackConfig.from_env(
                {
                    "FALLBACK_OIDC_EXPECTED_AUDIENCE": AUDIENCE,
                    "FALLBACK_OIDC_EXPECTED_PRINCIPALS": "not-an-email",
                }
            )
        with self.assertRaises(FallbackConfigurationError):
            create_app_from_env(
                env={
                    "FALLBACK_OIDC_EXPECTED_AUDIENCE": AUDIENCE,
                    "FALLBACK_OIDC_EXPECTED_PRINCIPALS": PRINCIPAL,
                },
                token_verifier_factory=lambda: lambda *_: {},
            )

    def test_logs_do_not_contain_authorization_or_injected_body(self):
        client, _ = self.client()
        secret = "SECRET-NEVER-LOG"
        with self.assertLogs("approval_fallback", level=logging.INFO) as captured:
            response = client.post(
                "/wake/gate-a",
                headers={"Authorization": f"Bearer {secret}"},
                json={"secret": secret},
            )
        self.assertEqual(response.status_code, 200)
        rendered = "\n".join(captured.output)
        self.assertNotIn(secret, rendered)
        self.assertIn(GATE_A_WORKFLOW, rendered)
