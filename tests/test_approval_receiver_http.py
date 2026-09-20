"""Flask HTTP-layer tests for the Phase 3B-2A1 receiver, through create_app.

These exercise the actual routing/status-code/JSON-response contract that
tests/test_approval_receiver.py does not: it tests ReceiverService and the
other pure classes directly, never the Flask adapter itself.

No network access, no live Google API, no real credentials, no cloud
resource: every dependency create_app() takes (config/gmail/store/verifier)
is injected with a local fake or an in-memory object.

Flask is a Cloud Run deployment-time dependency (see
cloud/approval_receiver/requirements.txt); it is intentionally not part of
this project's local dev environment, and installing it here would be an
environment mutation outside this task's authorized, test-file-only scope.
When Flask is unavailable, these tests are skipped rather than reporting a
false pass or a false regression; they run for real in any environment
(such as the eventual Cloud Run build) where Flask is installed.
"""

import importlib.util
import unittest

FLASK_AVAILABLE = importlib.util.find_spec("flask") is not None

if FLASK_AVAILABLE:
    from cloud.approval_receiver.main import OIDCVerifier, create_app
    from cloud.approval_receiver.observation import InMemoryObservationStore
    from tests.test_approval_receiver import FakeGmail, config, pubsub_envelope


def _fake_verifier(cfg):
    """A verifier that never contacts Google: it accepts the configured
    audience/caller pair via an injected token_validator, exactly like
    tests/test_approval_receiver.py's AuthenticationTests does.
    """
    caller = next(iter(cfg.oidc_expected_callers))
    return OIDCVerifier(
        cfg.oidc_expected_audience,
        cfg.oidc_expected_callers,
        token_validator=lambda token, audience: {
            "aud": cfg.oidc_expected_audience,
            "email": caller,
            "email_verified": True,
        },
    )


def _build_client(*, gmail=None, store=None, verifier=None, cfg=None):
    cfg = cfg or config()
    app = create_app(
        config=cfg,
        gmail=gmail or FakeGmail(),
        store=store or InMemoryObservationStore(),
        verifier=verifier or _fake_verifier(cfg),
    )
    app.testing = True
    return app.test_client()


@unittest.skipUnless(FLASK_AVAILABLE, "flask is not installed in this local environment")
class HealthEndpointTests(unittest.TestCase):
    def test_health_ok_without_authentication(self):
        client = _build_client()
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"status": "OK", "service": "approval-receiver-a1"},
        )


@unittest.skipUnless(FLASK_AVAILABLE, "flask is not installed in this local environment")
class PubsubEndpointTests(unittest.TestCase):
    def test_missing_bearer_token_rejected(self):
        client = _build_client()
        response = client.post("/pubsub", json=pubsub_envelope())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["status"], "REJECTED")

    def test_wrong_audience_token_rejected(self):
        cfg = config()
        wrong_audience_verifier = OIDCVerifier(
            cfg.oidc_expected_audience,
            cfg.oidc_expected_callers,
            token_validator=lambda token, audience: {
                "aud": "https://wrong.example",
                "email": next(iter(cfg.oidc_expected_callers)),
                "email_verified": True,
            },
        )
        client = _build_client(cfg=cfg, verifier=wrong_audience_verifier)
        response = client.post(
            "/pubsub",
            json=pubsub_envelope(),
            headers={"Authorization": "Bearer token"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["status"], "REJECTED")

    def test_malformed_envelope_rejected(self):
        client = _build_client()
        response = client.post(
            "/pubsub",
            json={"not": "an envelope"},
            headers={"Authorization": "Bearer token"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["status"], "REJECTED")

    def test_valid_pubsub_message_is_acknowledged(self):
        gmail = FakeGmail()
        store = InMemoryObservationStore()
        client = _build_client(gmail=gmail, store=store)
        response = client.post(
            "/pubsub",
            json=pubsub_envelope(),
            headers={"Authorization": "Bearer token"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["status"], "ACKNOWLEDGED")
        self.assertEqual(body["observations_inserted"], 1)
        self.assertEqual(body["pubsub_message_id"], "p1")

    def test_gmail_failure_returns_retry_500(self):
        gmail = FakeGmail()
        gmail.fail_message = "abc123"
        client = _build_client(gmail=gmail)
        response = client.post(
            "/pubsub",
            json=pubsub_envelope(),
            headers={"Authorization": "Bearer token"},
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["status"], "RETRY")


@unittest.skipUnless(FLASK_AVAILABLE, "flask is not installed in this local environment")
class MaintenanceEndpointTests(unittest.TestCase):
    def test_missing_bearer_token_rejected(self):
        client = _build_client()
        response = client.post("/maintenance")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["status"], "REJECTED")

    def test_maintenance_ok(self):
        client = _build_client()
        response = client.post(
            "/maintenance", headers={"Authorization": "Bearer token"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "OK")


if __name__ == "__main__":
    unittest.main()
