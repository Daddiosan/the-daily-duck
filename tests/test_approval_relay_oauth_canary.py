from __future__ import annotations

import logging
import unittest

from cloud.approval_relay.auth import PushAuthenticator
from cloud.approval_relay.gmail_reader import (
    FakeGmailReader,
    GmailCanaryError,
    GmailCanaryFailureKind,
    RealGmailReader,
)
from cloud.approval_relay.main import RelayConfig, RelayMode, create_app
from cloud.approval_relay.router import RoutingConfig
from cloud.approval_relay.storage import InMemoryWatchStateStore
from cloud.approval_relay.watch_renewal import WatchRenewalConfig


AUDIENCE = "https://relay.example.run.app"
PRINCIPAL = "scheduler@example.iam.gserviceaccount.com"


class Response:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class Users:
    def __init__(self, value):
        self.value = value

    def getProfile(self, *, userId):
        assert userId == "me"
        return Response(self.value)


class Service:
    def __init__(self, value):
        self.value = value

    def users(self):
        return Users(self.value)


class HttpFailure(Exception):
    def __init__(self, status):
        self.resp = type("Resp", (), {"status": status})()


class RefreshError(Exception):
    pass


def auth():
    return PushAuthenticator(
        expected_issuer="https://accounts.google.com",
        expected_audience=AUDIENCE,
        expected_principals=frozenset({PRINCIPAL}),
        token_verifier=lambda *_: {
            "iss": "https://accounts.google.com",
            "aud": AUDIENCE,
            "email_verified": True,
            "email": PRINCIPAL,
        },
    )


def config():
    return RelayConfig(
        mailbox_identity="duck@example.com",
        allowed_senders=frozenset({"sender@example.com"}),
        routing=RoutingConfig("gate subject", "design subject"),
        oidc_expected_issuer="https://accounts.google.com",
        oidc_expected_audience=AUDIENCE,
        oidc_expected_principals=frozenset({PRINCIPAL}),
        initial_history_id="1",
        firestore_project="project",
        mode=RelayMode.DRY_RUN,
    )


class GmailCanaryAdapterTests(unittest.TestCase):
    def test_success_uses_profile_metadata_only(self):
        RealGmailReader(
            Service({"emailAddress": "duck@example.com", "historyId": "123"})
        ).check_profile()

    def test_failure_classification(self):
        cases = (
            (RefreshError("SECRET invalid_grant"), GmailCanaryFailureKind.AUTH_FAILURE),
            (TimeoutError("SECRET"), GmailCanaryFailureKind.TIMEOUT),
            (HttpFailure(429), GmailCanaryFailureKind.RATE_LIMITED),
            (HttpFailure(503), GmailCanaryFailureKind.RETRYABLE),
        )
        for failure, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(
                GmailCanaryError
            ) as raised:
                RealGmailReader(Service(failure)).check_profile()
            self.assertEqual(raised.exception.kind, expected)
            self.assertNotIn("SECRET", str(raised.exception))

    def test_malformed_response_is_sanitized(self):
        for response in (None, {}, {"emailAddress": "duck@example.com"}):
            with self.subTest(response=response), self.assertRaises(
                GmailCanaryError
            ) as raised:
                RealGmailReader(Service(response)).check_profile()
            self.assertEqual(
                raised.exception.kind, GmailCanaryFailureKind.MALFORMED_RESPONSE
            )


class GmailCanaryRouteTests(unittest.TestCase):
    def client(self, gmail):
        app = create_app(
            config=config(),
            gmail=gmail,
            authenticator=auth(),
            renewal_config=WatchRenewalConfig(
                mailbox_identity="duck@example.com",
                topic_name="projects/project/topics/gmail",
            ),
            renewal_authenticator=auth(),
            gmail_watch_factory=lambda: None,
            watch_state_store=InMemoryWatchStateStore(),
        )
        return app.test_client()

    def test_success_and_auth_failure(self):
        gmail = FakeGmailReader()
        client = self.client(gmail)
        self.assertEqual(client.post("/oauth-canary").status_code, 401)
        response = client.post(
            "/oauth-canary", headers={"Authorization": "Bearer valid"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "OK"})
        self.assertEqual(gmail.profile_calls, 1)

    def test_failure_response_and_log_never_leak_provider_text(self):
        secret = "SECRET-PROVIDER-PAYLOAD"
        gmail = FakeGmailReader(
            profile=GmailCanaryError(GmailCanaryFailureKind.AUTH_FAILURE)
        )
        client = self.client(gmail)
        with self.assertLogs("approval_relay", level=logging.INFO) as captured:
            response = client.post(
                "/oauth-canary",
                headers={"Authorization": f"Bearer {secret}"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["reason"], "AUTH_FAILURE")
        self.assertNotIn(secret, "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
