import base64
import json
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from cloud.approval_receiver.gmail_client import (
    GmailClientError,
    HistoryBatch,
    StaleHistoryError,
)
from cloud.approval_receiver.main import (
    AuthenticationError,
    ConfigurationError,
    EnvelopeError,
    OIDCVerifier,
    ReceiverConfig,
    ReceiverService,
    decode_pubsub_envelope,
)
from cloud.approval_receiver.observation import (
    CursorState,
    InMemoryObservationStore,
    ObservationError,
    create_sanitized_observation,
)


NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def valid_env():
    return {
        "GMAIL_MAILBOX_IDENTITY": "duck@example.com",
        "GMAIL_ALLOWED_SENDERS": "owner@example.com,editor@example.com",
        "GATE_A_SUBJECT_PATTERN": "Choose Today's Story",
        "DESIGN_SUBJECT_PATTERN": "Choose Final Design",
        "GMAIL_PUBSUB_TOPIC": "projects/daily-duck/topics/gmail-events",
        "OIDC_EXPECTED_AUDIENCE": "https://receiver.example.run.app/pubsub",
        "OIDC_EXPECTED_CALLERS": (
            "pubsub@example.iam.gserviceaccount.com,"
            "scheduler@example.iam.gserviceaccount.com"
        ),
        "FIRESTORE_CURSOR_COLLECTION": "approval_receiver",
        "FIRESTORE_CURSOR_DOCUMENT": "gmail_cursor",
        "FIRESTORE_OBSERVATION_COLLECTION": "approval_observations",
    }


def pubsub_envelope(email="duck@example.com", history_id="200", message_id="p1"):
    payload = json.dumps(
        {"emailAddress": email, "historyId": history_id}
    ).encode("utf-8")
    return {
        "message": {
            "data": base64.b64encode(payload).decode("ascii"),
            "messageId": message_id,
        }
    }


def gmail_message(
    message_id="abc123",
    *,
    sender="owner@example.com",
    subject="Re: Choose Today's Story",
    body="3\n",
    auth_results="mx.google.com; dkim=pass header.d=example.com; dmarc=pass",
    in_reply_to="<original@example.com>",
    references="<original@example.com>",
):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "duck@example.com"
    message["Subject"] = subject
    message["Message-ID"] = f"<{message_id}@example.com>"
    if auth_results is not None:
        message["Authentication-Results"] = auth_results
    if in_reply_to is not None:
        message["In-Reply-To"] = in_reply_to
    if references is not None:
        message["References"] = references
    message.set_content(body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
    return {
        "id": message_id,
        "threadId": "thread-1",
        "internalDate": "1789819200000",
        "raw": raw,
    }


class FakeGmail:
    def __init__(self):
        self.history = HistoryBatch(("abc123",), "200")
        self.messages = {"abc123": gmail_message()}
        self.profile = {"historyId": "200", "emailAddress": "duck@example.com"}
        self.watch_response = {"historyId": "300", "expiration": "1790424000000"}
        self.raise_stale = False
        self.fail_message = None
        self.history_calls = []
        self.message_calls = []
        self.list_queries = []
        self.watch_calls = []

    def watch(self, topic_name):
        self.watch_calls.append(topic_name)
        return self.watch_response

    def list_history(self, start_history_id):
        self.history_calls.append(start_history_id)
        if self.raise_stale:
            raise StaleHistoryError("stale")
        return self.history

    def get_message(self, message_id):
        self.message_calls.append(message_id)
        if message_id == self.fail_message or message_id not in self.messages:
            raise GmailClientError("fetch failed")
        return self.messages[message_id]

    def list_messages(self, query, max_results):
        self.list_queries.append((query, max_results))
        return tuple(list(self.messages)[:max_results])

    def get_profile(self):
        return self.profile


def config():
    return ReceiverConfig.from_env(valid_env())


class ReceiverConfigTests(unittest.TestCase):
    def test_valid_config(self):
        value = config()
        self.assertEqual(value.mailbox_identity, "duck@example.com")
        self.assertEqual(value.watch_renew_threshold_hours, 48)
        self.assertEqual(value.full_resync_newer_than, "7d")

    def test_missing_config_fails_closed(self):
        with self.assertRaises(ConfigurationError):
            ReceiverConfig.from_env({})

    def test_malformed_sender_config_fails(self):
        env = valid_env()
        env["GMAIL_ALLOWED_SENDERS"] = "owner@example.com,not-an-address"
        with self.assertRaises(ConfigurationError):
            ReceiverConfig.from_env(env)

    def test_malformed_topic_fails(self):
        env = valid_env()
        env["GMAIL_PUBSUB_TOPIC"] = "not-a-topic"
        with self.assertRaises(ConfigurationError):
            ReceiverConfig.from_env(env)

    def test_malformed_numeric_config_fails(self):
        env = valid_env()
        env["WATCH_RENEW_THRESHOLD_HOURS"] = "soon"
        with self.assertRaises(ConfigurationError):
            ReceiverConfig.from_env(env)

    def test_unbounded_resync_config_fails(self):
        env = valid_env()
        env["FULL_RESYNC_MAX_MESSAGES"] = "1000"
        with self.assertRaises(ConfigurationError):
            ReceiverConfig.from_env(env)


class EnvelopeTests(unittest.TestCase):
    def test_valid_pubsub_envelope(self):
        value = decode_pubsub_envelope(pubsub_envelope())
        self.assertEqual(value.email_address, "duck@example.com")
        self.assertEqual(value.history_id, "200")
        self.assertEqual(value.pubsub_message_id, "p1")

    def test_non_object_envelope_rejected(self):
        with self.assertRaises(EnvelopeError):
            decode_pubsub_envelope([])

    def test_missing_message_rejected(self):
        with self.assertRaises(EnvelopeError):
            decode_pubsub_envelope({})

    def test_invalid_base64_rejected(self):
        with self.assertRaises(EnvelopeError):
            decode_pubsub_envelope({"message": {"data": "%%%"}})

    def test_missing_email_rejected(self):
        data = base64.b64encode(json.dumps({"historyId": "2"}).encode()).decode()
        with self.assertRaises(EnvelopeError):
            decode_pubsub_envelope({"message": {"data": data}})

    def test_missing_history_rejected(self):
        data = base64.b64encode(
            json.dumps({"emailAddress": "duck@example.com"}).encode()
        ).decode()
        with self.assertRaises(EnvelopeError):
            decode_pubsub_envelope({"message": {"data": data}})

    def test_non_numeric_history_rejected(self):
        with self.assertRaises(EnvelopeError):
            decode_pubsub_envelope(pubsub_envelope(history_id="latest"))


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.claims = {
            "aud": "https://receiver.example.run.app/pubsub",
            "email": "pubsub@example.iam.gserviceaccount.com",
            "email_verified": True,
        }
        self.verifier = OIDCVerifier(
            self.claims["aud"],
            (self.claims["email"],),
            token_validator=lambda token, audience: dict(self.claims),
        )

    def test_signed_bearer_claims_are_accepted(self):
        self.assertEqual(
            self.verifier.verify("Bearer signed-token"), self.claims["email"]
        )

    def test_caller_controlled_identity_header_is_not_an_auth_input(self):
        with self.assertRaises(AuthenticationError):
            self.verifier.verify(None)

    def test_wrong_audience_rejected(self):
        self.claims["aud"] = "https://wrong.example"
        with self.assertRaises(AuthenticationError):
            self.verifier.verify("Bearer signed-token")

    def test_unverified_email_rejected(self):
        self.claims["email_verified"] = False
        with self.assertRaises(AuthenticationError):
            self.verifier.verify("Bearer signed-token")

    def test_unexpected_caller_rejected(self):
        self.claims["email"] = "attacker@example.com"
        with self.assertRaises(AuthenticationError):
            self.verifier.verify("Bearer signed-token")


class ObservationTests(unittest.TestCase):
    def make(self, message=None):
        return create_sanitized_observation(
            message or gmail_message(),
            mailbox_identity="duck@example.com",
            allowed_senders=("owner@example.com",),
            subject_patterns=("Choose Today's Story", "Choose Final Design"),
            history_id="200",
            notification_received_at="2026-09-19T11:59:58Z",
            recovery_path="PUSH",
            clock=lambda: "2026-09-19T12:00:00Z",
        )

    def test_message_identity_fields(self):
        value = self.make()
        self.assertEqual(value["gmail_message_id"], "abc123")
        self.assertEqual(value["thread_id"], "thread-1")
        self.assertEqual(value["internal_date"], "1789819200000")
        self.assertEqual(value["history_id"], "200")
        self.assertEqual(len(value["message_id_header_hash"]), 64)

    def test_body_hash_is_stable(self):
        first = self.make(gmail_message(body="3\n"))["body_sha256"]
        second = self.make(gmail_message(body="3\n"))["body_sha256"]
        changed = self.make(gmail_message(body="4\n"))["body_sha256"]
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_raw_body_and_sender_are_not_output(self):
        value = self.make(gmail_message(body="private command text"))
        serialized = json.dumps(value)
        self.assertNotIn("private command text", serialized)
        self.assertNotIn("owner@example.com", serialized)
        self.assertNotIn("raw", value)

    def test_allowlist_and_subject_match_are_boolean(self):
        value = self.make()
        self.assertIs(value["from_allowlist_match"], True)
        self.assertIs(value["subject_match"], True)

    def test_unmatched_sender_and_subject(self):
        value = self.make(
            gmail_message(sender="other@example.com", subject="Unrelated")
        )
        self.assertIs(value["from_allowlist_match"], False)
        self.assertIs(value["subject_match"], False)

    def test_dkim_or_dmarc_pass_observed(self):
        self.assertIs(self.make()["dkim_or_dmarc_pass"], True)

    def test_dkim_failure_observed(self):
        message = gmail_message(
            auth_results="mx.google.com; dkim=fail; dmarc=fail"
        )
        self.assertIs(self.make(message)["dkim_or_dmarc_pass"], False)

    def test_missing_authentication_results_is_unknown(self):
        message = gmail_message(auth_results=None)
        self.assertIsNone(self.make(message)["dkim_or_dmarc_pass"])

    def test_reply_relationship_observed(self):
        self.assertIs(self.make()["reply_thread_match"], True)

    def test_missing_reply_headers_is_unknown(self):
        message = gmail_message(in_reply_to=None, references=None)
        self.assertIsNone(self.make(message)["reply_thread_match"])

    def test_incomplete_gmail_identity_rejected(self):
        message = gmail_message()
        del message["threadId"]
        with self.assertRaises(ObservationError):
            self.make(message)


class ReceiverProcessingTests(unittest.TestCase):
    def setUp(self):
        self.gmail = FakeGmail()
        self.store = InMemoryObservationStore(
            CursorState(
                processing_history_id="100",
                watch_history_id="90",
                watch_expiration_ms=int((NOW + timedelta(days=6)).timestamp() * 1000),
            )
        )
        self.receiver = ReceiverService(
            config(),
            self.gmail,
            self.store,
            clock=lambda: "2026-09-19T12:00:00Z",
            now=lambda: NOW,
        )

    def test_push_fetches_exact_message_and_advances_cursor(self):
        result = self.receiver.process_pubsub(pubsub_envelope())
        self.assertEqual(self.gmail.history_calls, ["100"])
        self.assertEqual(self.gmail.message_calls, ["abc123"])
        self.assertEqual(result["observations_inserted"], 1)
        self.assertEqual(self.store.read_cursor().processing_history_id, "200")

    def test_mailbox_mismatch_rejected(self):
        with self.assertRaises(EnvelopeError):
            self.receiver.process_pubsub(pubsub_envelope(email="other@example.com"))
        self.assertEqual(self.store.read_cursor().processing_history_id, "100")

    def test_duplicate_gmail_message_does_not_create_second_observation(self):
        first = self.receiver.process_pubsub(pubsub_envelope(history_id="200"))
        self.gmail.history = HistoryBatch(("abc123",), "300")
        second = self.receiver.process_pubsub(pubsub_envelope(history_id="300"))
        self.assertEqual(first["observations_inserted"], 1)
        self.assertEqual(second["observations_inserted"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(len(self.store.observations), 1)

    def test_replayed_notification_acknowledged_without_history_call(self):
        self.store = InMemoryObservationStore(
            CursorState(processing_history_id="200")
        )
        receiver = ReceiverService(config(), self.gmail, self.store)
        result = receiver.process_pubsub(pubsub_envelope(history_id="200"))
        self.assertTrue(result["duplicate_notification"])
        self.assertEqual(self.gmail.history_calls, [])

    def test_partial_failure_does_not_advance_cursor(self):
        self.gmail.history = HistoryBatch(("abc123", "missing"), "200")
        self.gmail.fail_message = "missing"
        with self.assertRaises(GmailClientError):
            self.receiver.process_pubsub(pubsub_envelope())
        self.assertEqual(self.store.read_cursor().processing_history_id, "100")
        self.assertEqual(len(self.store.observations), 1)

    def test_retry_after_partial_failure_deduplicates_completed_prefix(self):
        self.gmail.history = HistoryBatch(("abc123", "second"), "200")
        self.gmail.fail_message = "second"
        with self.assertRaises(GmailClientError):
            self.receiver.process_pubsub(pubsub_envelope())
        self.gmail.messages["second"] = gmail_message("second", body="NEXT 3\n")
        self.gmail.fail_message = None
        result = self.receiver.process_pubsub(pubsub_envelope())
        self.assertEqual(result["observations_inserted"], 1)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(self.store.read_cursor().processing_history_id, "200")

    def test_stale_history_performs_bounded_full_resync(self):
        self.gmail.raise_stale = True
        self.gmail.profile = {"historyId": "250"}
        result = self.receiver.process_pubsub(pubsub_envelope())
        self.assertEqual(result["recovery_path"], "FULL_RESYNC")
        self.assertEqual(self.store.read_cursor().processing_history_id, "250")
        query, maximum = self.gmail.list_queries[0]
        self.assertIn('from:"owner@example.com"', query)
        self.assertIn('subject:"Choose Today\'s Story"', query)
        self.assertIn("newer_than:7d", query)
        self.assertEqual(maximum, 100)

    def test_missing_cursor_bootstraps_via_full_resync(self):
        store = InMemoryObservationStore()
        receiver = ReceiverService(config(), self.gmail, store)
        result = receiver.process_pubsub(pubsub_envelope())
        self.assertEqual(result["recovery_path"], "FULL_RESYNC")
        self.assertEqual(store.read_cursor().processing_history_id, "200")

    def test_catchup_advances_processing_cursor(self):
        result = self.receiver.maintenance()
        self.assertEqual(result["catchup"]["recovery_path"], "CATCHUP")
        self.assertEqual(self.store.read_cursor().processing_history_id, "200")
        self.assertFalse(result["watch_renewed"])

    def test_watch_renews_below_threshold(self):
        self.store = InMemoryObservationStore(
            CursorState(
                processing_history_id="100",
                watch_history_id="90",
                watch_expiration_ms=int((NOW + timedelta(hours=47)).timestamp() * 1000),
            )
        )
        receiver = ReceiverService(
            config(), self.gmail, self.store, now=lambda: NOW
        )
        result = receiver.maintenance()
        cursor = self.store.read_cursor()
        self.assertTrue(result["watch_renewed"])
        self.assertEqual(cursor.watch_history_id, "300")
        self.assertEqual(self.gmail.watch_calls, [config().pubsub_topic])

    def test_watch_response_never_replaces_processing_cursor(self):
        self.store = InMemoryObservationStore(
            CursorState(
                processing_history_id="200",
                watch_history_id="90",
                watch_expiration_ms=int((NOW + timedelta(hours=1)).timestamp() * 1000),
            )
        )
        self.gmail.profile = {"historyId": "200"}
        receiver = ReceiverService(
            config(), self.gmail, self.store, now=lambda: NOW
        )
        receiver.maintenance()
        cursor = self.store.read_cursor()
        self.assertEqual(cursor.processing_history_id, "200")
        self.assertEqual(cursor.watch_history_id, "300")

    def test_watch_is_not_renewed_unnecessarily(self):
        result = self.receiver.maintenance()
        self.assertFalse(result["watch_renewed"])
        self.assertEqual(self.gmail.watch_calls, [])


class InMemoryStoreTests(unittest.TestCase):
    def test_cursor_compare_and_set(self):
        store = InMemoryObservationStore(
            CursorState(processing_history_id="10")
        )
        self.assertFalse(store.compare_and_update_cursor("9", "11"))
        self.assertTrue(store.compare_and_update_cursor("10", "11"))
        self.assertEqual(store.read_cursor().processing_history_id, "11")

    def test_watch_update_is_separate(self):
        store = InMemoryObservationStore(
            CursorState(processing_history_id="10")
        )
        store.update_watch("99", 12345)
        value = store.read_cursor()
        self.assertEqual(value.processing_history_id, "10")
        self.assertEqual(value.watch_history_id, "99")


if __name__ == "__main__":
    unittest.main()
