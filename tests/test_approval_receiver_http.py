"""Flask HTTP-layer tests for the Phase 3B-2A1 receiver, through create_app.

These exercise the actual routing/status-code/JSON-response contract that
tests/test_approval_receiver.py does not: it tests ReceiverService and the
other pure classes directly, never the Flask adapter itself.

No network access, no live Google API, no real credentials, no cloud
resource: every dependency create_app() takes (config/gmail/store/verifier)
is injected with a local fake or an in-memory object.

Flask is a declared Phase 3B-2A1 A1 runtime dependency (see
cloud/approval_receiver/requirements.txt: "Flask>=3.0,<4"), not merely a
Cloud Run deployment-time detail, so it is also a test-time dependency of
this module. In a correctly prepared Phase 3B-2A1 test environment (the
project .venv with requirements.txt installed) these 8 tests are expected to
execute normally, not skip. Acceptance requires zero HTTP-test skips.

If Flask is missing, this module fails loudly at import time (see below)
instead of silently skipping all 8 tests, because a clean checkout that
silently skips this entire module could otherwise report an overall "OK"
while providing zero HTTP-layer coverage.
"""

import importlib.util
import unittest

if importlib.util.find_spec("flask") is None:
    raise ImportError(
        "Flask is a required Phase 3B-2A1 A1 test/runtime dependency "
        "(see cloud/approval_receiver/requirements.txt) but is not "
        "installed in this environment. Install it into the project "
        "virtualenv before running this test module, e.g.:\n"
        "  .venv/Scripts/python.exe -m pip install "
        "-r cloud/approval_receiver/requirements.txt\n"
        "This module intentionally fails at import time rather than "
        "skipping its 8 HTTP tests, so a missing dependency cannot be "
        "mistaken for passing coverage."
    )

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


class HealthEndpointTests(unittest.TestCase):
    def test_health_ok_without_authentication(self):
        client = _build_client()
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"status": "OK", "service": "approval-receiver-a1"},
        )


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
