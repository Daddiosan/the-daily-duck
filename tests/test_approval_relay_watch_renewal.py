"""Focused, network-free tests for authenticated Gmail watch renewal."""

from __future__ import annotations

import logging
import threading
import unittest
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

from cloud.approval_relay.auth import PushAuthenticator
from cloud.approval_relay.gmail_reader import FakeGmailReader
from cloud.approval_relay.main import RelayConfig, create_app
from cloud.approval_relay.router import RoutingConfig, mailbox_hash_for
from cloud.approval_relay.storage import (
    InMemoryCursorStore,
    InMemoryWatchStateStore,
    WatchStateRecord,
    RelayStorageError,
)
from cloud.approval_relay.watch_renewal import (
    GmailWatchCallError,
    RealGmailWatchClient,
    RenewalFailureKind,
    RenewalOperationError,
    WatchRenewalConfig,
    WatchRenewalService,
)


FAKE_MAILBOX = "owner@example.com"
FAKE_TOPIC = "projects/fake-project/topics/fake-gmail-events"
FAKE_EXPIRATION = 1791000000000


class FakeWatchClient:
    def __init__(self, response=None, error=None) -> None:
        self.response = response or {
            "historyId": "301",
            "expiration": str(FAKE_EXPIRATION),
        }
        self.error = error
        self.calls = []

    def watch(self, topic_name):
        self.calls.append(topic_name)
        if self.error is not None:
            raise self.error
        return self.response


def renewal_service(client, store=None):
    return WatchRenewalService(
        config=WatchRenewalConfig(
            mailbox_identity=FAKE_MAILBOX,
            topic_name=FAKE_TOPIC,
        ),
        gmail_factory=lambda: client,
        state_store=store or InMemoryWatchStateStore(clock=lambda: "2026-09-22T00:00:00Z"),
    )


def claims(*, audience, principal):
    return {
        "iss": "https://accounts.google.com",
        "aud": audience,
        "email": principal,
        "email_verified": True,
    }


def authenticator(*, audience, principal, claimed_principal=None):
    return PushAuthenticator(
        expected_issuer="https://accounts.google.com",
        expected_audience=audience,
        expected_principals=frozenset({principal}),
        token_verifier=lambda token, expected_audience: claims(
            audience=expected_audience,
            principal=claimed_principal or principal,
        ),
    )


def app_with_renewal(*, client=None, renewal_auth=None, store=None, cursor_store=None):
    relay_audience = "https://relay.example/relay"
    renewal_audience = "https://relay.example/renew-watch"
    relay_principal = "push@example.iam.gserviceaccount.com"
    renewal_principal = "renew@example.iam.gserviceaccount.com"
    actual_client = client or FakeWatchClient()
    actual_store = store or InMemoryWatchStateStore(clock=lambda: "renewed-at")
    app = create_app(
        config=RelayConfig(
            mailbox_identity=FAKE_MAILBOX,
            allowed_senders=frozenset({FAKE_MAILBOX}),
            routing=RoutingConfig("gate-pattern", "design-pattern"),
            oidc_expected_issuer="https://accounts.google.com",
            oidc_expected_audience=relay_audience,
            oidc_expected_principals=frozenset({relay_principal}),
            initial_history_id="100",
            firestore_project="fake-project",
        ),
        gmail=FakeGmailReader(),
        authenticator=authenticator(
            audience=relay_audience, principal=relay_principal
        ),
        cursor_store=cursor_store,
        renewal_config=WatchRenewalConfig(FAKE_MAILBOX, FAKE_TOPIC),
        renewal_authenticator=renewal_auth
        or authenticator(audience=renewal_audience, principal=renewal_principal),
        gmail_watch_factory=lambda: actual_client,
        watch_state_store=actual_store,
    )
    return app, actual_store


class RenewalStateTests(unittest.TestCase):
    def test_success_stores_only_sanitized_strict_state(self):
        store = InMemoryWatchStateStore(clock=lambda: "renewed-at")
        client = FakeWatchClient()
        result = renewal_service(client, store).renew()
        state = store.read_watch_state(mailbox_hash_for(FAKE_MAILBOX))

        self.assertEqual(result, {"status": "RENEWED", "state_updated": True})
        self.assertEqual(client.calls, [FAKE_TOPIC])
        self.assertEqual(state.history_id, "301")
        self.assertIsInstance(state.history_id, str)
        self.assertEqual(state.expiration, FAKE_EXPIRATION)
        self.assertEqual(state.mailbox_hash, mailbox_hash_for(FAKE_MAILBOX))
        self.assertNotEqual(state.mailbox_hash, FAKE_MAILBOX)
        self.assertEqual(
            {field.name for field in fields(WatchStateRecord)},
            {"expiration", "history_id", "mailbox_hash", "updated_at"},
        )
        persisted_text = repr(state)
        self.assertNotIn(FAKE_MAILBOX, persisted_text)
        for forbidden in ("access_token", "refresh_token", "client_secret"):
            self.assertNotIn(forbidden, persisted_text)

    def test_numeric_history_id_is_normalized_to_string(self):
        store = InMemoryWatchStateStore()
        renewal_service(
            FakeWatchClient(
                response={"historyId": 302, "expiration": FAKE_EXPIRATION}
            ),
            store,
        ).renew()
        self.assertEqual(
            store.read_watch_state(mailbox_hash_for(FAKE_MAILBOX)).history_id,
            "302",
        )

    def test_missing_or_malformed_history_id_fails_closed(self):
        invalid = [None, True, False, -1, 1.5, [], {}, "", "+1", "1.0", "1e3", "abc1"]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(RenewalOperationError) as caught:
                renewal_service(
                    FakeWatchClient(
                        response={"historyId": value, "expiration": FAKE_EXPIRATION}
                    )
                ).renew()
            self.assertEqual(caught.exception.kind, RenewalFailureKind.MALFORMED_RESPONSE)

    def test_missing_or_malformed_expiration_fails_closed(self):
        invalid = [None, True, False, 0, -1, 1.5, [], {}, "", "+1", "1.0", "1e3"]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(RenewalOperationError) as caught:
                renewal_service(
                    FakeWatchClient(response={"historyId": "301", "expiration": value})
                ).renew()
            self.assertEqual(caught.exception.kind, RenewalFailureKind.MALFORMED_RESPONSE)

    def test_non_mapping_response_fails_closed(self):
        client = FakeWatchClient()
        client.response = []
        with self.assertRaises(RenewalOperationError) as caught:
            renewal_service(client).renew()
        self.assertEqual(caught.exception.kind, RenewalFailureKind.MALFORMED_RESPONSE)

    def test_gmail_failure_preserves_previous_successful_state(self):
        store = InMemoryWatchStateStore(clock=lambda: "t")
        renewal_service(FakeWatchClient(), store).renew()
        before = store.read_watch_state(mailbox_hash_for(FAKE_MAILBOX))
        failing = FakeWatchClient(
            error=GmailWatchCallError(RenewalFailureKind.RETRYABLE)
        )
        with self.assertRaises(RenewalOperationError):
            renewal_service(failing, store).renew()
        self.assertEqual(store.read_watch_state(mailbox_hash_for(FAKE_MAILBOX)), before)

    def test_repeated_success_is_idempotent(self):
        store = InMemoryWatchStateStore(clock=lambda: "t")
        service = renewal_service(FakeWatchClient(), store)
        first = service.renew()
        second = service.renew()
        self.assertTrue(first["state_updated"])
        self.assertEqual(
            second, {"status": "RENEWED_STATE_RETAINED", "state_updated": False}
        )

    def test_older_overlapping_result_cannot_overwrite_newer_state(self):
        a_started = threading.Event()
        b_done = threading.Event()
        store = InMemoryWatchStateStore(clock=lambda: "t")
        failures = []

        class BlockingClient:
            def __init__(self, name):
                self.name = name

            def watch(self, topic_name):
                if self.name == "A":
                    a_started.set()
                    if not b_done.wait(5):
                        raise AssertionError("newer renewal did not complete")
                    return {"historyId": "300", "expiration": FAKE_EXPIRATION}
                if not a_started.wait(5):
                    raise AssertionError("older renewal did not start")
                return {"historyId": "400", "expiration": FAKE_EXPIRATION + 1000}

        service = WatchRenewalService(
            config=WatchRenewalConfig(FAKE_MAILBOX, FAKE_TOPIC),
            gmail_factory=lambda: BlockingClient(threading.current_thread().name),
            state_store=store,
        )

        def run(name):
            try:
                service.renew()
            except Exception as exc:  # pragma: no cover - assertion aid
                failures.append(exc)
            finally:
                if name == "B":
                    b_done.set()

        thread_a = threading.Thread(target=run, args=("A",), name="A")
        thread_b = threading.Thread(target=run, args=("B",), name="B")
        thread_a.start()
        thread_b.start()
        thread_a.join(5)
        thread_b.join(5)
        self.assertFalse(thread_a.is_alive())
        self.assertFalse(thread_b.is_alive())
        self.assertEqual(failures, [])
        state = store.read_watch_state(mailbox_hash_for(FAKE_MAILBOX))
        self.assertEqual((state.expiration, state.history_id), (FAKE_EXPIRATION + 1000, "400"))

    def test_renewal_never_changes_relay_cursor(self):
        cursor = InMemoryCursorStore(clock=lambda: "cursor-at")
        mailbox_hash = mailbox_hash_for(FAKE_MAILBOX)
        self.assertTrue(cursor.compare_and_update_cursor(mailbox_hash, None, "250"))
        app, _ = app_with_renewal(cursor_store=cursor)
        response = app.test_client().post(
            "/renew-watch", headers={"Authorization": "Bearer fake-token"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(cursor.read_cursor(mailbox_hash).history_id, "250")


class RenewalHttpAndFailureTests(unittest.TestCase):
    def test_unauthenticated_and_wrong_principal_are_rejected_before_gmail(self):
        client = FakeWatchClient()
        app, _ = app_with_renewal(client=client)
        self.assertEqual(app.test_client().post("/renew-watch").status_code, 401)

        wrong_auth = authenticator(
            audience="https://relay.example/renew-watch",
            principal="renew@example.iam.gserviceaccount.com",
            claimed_principal="other@example.iam.gserviceaccount.com",
        )
        wrong_app, _ = app_with_renewal(client=client, renewal_auth=wrong_auth)
        response = wrong_app.test_client().post(
            "/renew-watch", headers={"Authorization": "Bearer fake-token"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(client.calls, [])

    def test_correct_separate_renewal_principal_is_accepted(self):
        app, _ = app_with_renewal()
        response = app.test_client().post(
            "/renew-watch", headers={"Authorization": "Bearer fake-token"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "RENEWED")

    def test_retryable_nonretryable_and_unknown_http_results(self):
        cases = [
            (RenewalFailureKind.RETRYABLE, 503, "RETRY"),
            (RenewalFailureKind.NON_RETRYABLE, 400, "REJECTED"),
            (RenewalFailureKind.UNKNOWN, 500, "RETRY"),
        ]
        for kind, expected_status, expected_body_status in cases:
            with self.subTest(kind=kind):
                app, _ = app_with_renewal(
                    client=FakeWatchClient(error=GmailWatchCallError(kind))
                )
                response = app.test_client().post(
                    "/renew-watch", headers={"Authorization": "Bearer fake-token"}
                )
                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.get_json()["status"], expected_body_status)
                self.assertEqual(response.get_json()["reason"], kind.value)

    def test_storage_failure_is_retryable_5xx_and_sanitized(self):
        class FailingStore(InMemoryWatchStateStore):
            def store_watch_state_if_newer(self, mailbox_hash, history_id, expiration):
                raise RelayStorageError("fake sensitive storage detail")

        app, _ = app_with_renewal(store=FailingStore())
        response = app.test_client().post(
            "/renew-watch", headers={"Authorization": "Bearer fake-token"}
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json(), {"status": "RETRY", "reason": "STORAGE_FAILURE"}
        )

    def test_each_invocation_gets_a_dedicated_gmail_client(self):
        clients = []

        def factory():
            client = FakeWatchClient(
                response={
                    "historyId": str(301 + len(clients)),
                    "expiration": FAKE_EXPIRATION + len(clients),
                }
            )
            clients.append(client)
            return client

        store = InMemoryWatchStateStore()
        service = WatchRenewalService(
            config=WatchRenewalConfig(FAKE_MAILBOX, FAKE_TOPIC),
            gmail_factory=factory,
            state_store=store,
        )
        service.renew()
        service.renew()
        self.assertEqual(len(clients), 2)
        self.assertIsNot(clients[0], clients[1])

    def test_logs_and_responses_are_sanitized(self):
        secret_token = "fake-sensitive-bearer-token"
        app, _ = app_with_renewal()
        with self.assertLogs("approval_relay", level=logging.INFO) as captured:
            response = app.test_client().post(
                "/renew-watch", headers={"Authorization": f"Bearer {secret_token}"}
            )
        combined = "\n".join(captured.output) + repr(response.get_json())
        self.assertNotIn(secret_token, combined)
        self.assertNotIn(FAKE_MAILBOX, combined)
        self.assertNotIn(FAKE_TOPIC, combined)


class GmailWatchAdapterTests(unittest.TestCase):
    class ExecuteRequest:
        def __init__(self, response=None, error=None):
            self.response = response
            self.error = error

        def execute(self):
            if self.error is not None:
                raise self.error
            return self.response

    class Users:
        def __init__(self, request):
            self.request = request
            self.kwargs = None

        def watch(self, **kwargs):
            self.kwargs = kwargs
            return self.request

    class Service:
        def __init__(self, request):
            self.api = GmailWatchAdapterTests.Users(request)

        def users(self):
            return self.api

    def test_exact_watch_topic_and_inbox_include_semantics(self):
        service = self.Service(
            self.ExecuteRequest(
                {"historyId": "301", "expiration": str(FAKE_EXPIRATION)}
            )
        )
        result = RealGmailWatchClient(service).watch(FAKE_TOPIC)
        self.assertEqual(result["historyId"], "301")
        self.assertEqual(
            service.api.kwargs,
            {
                "userId": "me",
                "body": {
                    "topicName": FAKE_TOPIC,
                    "labelIds": ["INBOX"],
                    "labelFilterBehavior": "include",
                },
            },
        )

    def test_http_and_network_failures_are_classified(self):
        class HttpError(Exception):
            def __init__(self, status):
                self.resp = SimpleNamespace(status=status)

        cases = [
            (HttpError(429), RenewalFailureKind.RETRYABLE),
            (HttpError(500), RenewalFailureKind.RETRYABLE),
            (HttpError(502), RenewalFailureKind.RETRYABLE),
            (HttpError(503), RenewalFailureKind.RETRYABLE),
            (HttpError(504), RenewalFailureKind.RETRYABLE),
            (HttpError(400), RenewalFailureKind.NON_RETRYABLE),
            (HttpError(401), RenewalFailureKind.NON_RETRYABLE),
            (HttpError(403), RenewalFailureKind.NON_RETRYABLE),
            (TimeoutError("fake timeout"), RenewalFailureKind.RETRYABLE),
            (RuntimeError("fake unknown"), RenewalFailureKind.UNKNOWN),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__, expected=expected):
                service = self.Service(self.ExecuteRequest(error=error))
                with self.assertRaises(GmailWatchCallError) as caught:
                    RealGmailWatchClient(service).watch(FAKE_TOPIC)
                self.assertEqual(caught.exception.kind, expected)

    def test_non_mapping_api_response_is_nonretryable_malformed_result(self):
        service = self.Service(self.ExecuteRequest([]))
        client = RealGmailWatchClient(service)
        with self.assertRaises(GmailWatchCallError) as caught:
            client.watch(FAKE_TOPIC)
        self.assertEqual(caught.exception.kind, RenewalFailureKind.MALFORMED_RESPONSE)
        with self.assertRaises(RenewalOperationError) as operation:
            renewal_service(client).renew()
        self.assertEqual(operation.exception.http_status, 400)


class RenewalCapabilityBoundaryTests(unittest.TestCase):
    def test_no_github_email_send_or_periodic_loop_capability_added(self):
        relay = Path(__file__).resolve().parents[1] / "cloud" / "approval_relay"
        source = "\n".join(path.read_text(encoding="utf-8") for path in relay.glob("*.py"))
        lowered = source.lower()
        for marker in ("api.github.com", "smtplib", "messages().send", "sendmail"):
            self.assertNotIn(marker, lowered)
        for marker in ("time.sleep(", "apscheduler", "croniter", "while True:"):
            self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()
