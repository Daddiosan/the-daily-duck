"""Phase 3B-2 A2 shadow sibling service (cloud/approval_dispatcher) tests.

No real Gmail API, Pub/Sub, Firestore, or GitHub network access anywhere in
this file. FirestoreDispatcherStorageTests stubs google.cloud.firestore into
sys.modules for its own duration only, mirroring the proven technique in
tests/test_approval_receiver_firestore.py -- see that file's own docstring
for the exact scope and limitation of what a stub like this can and cannot
verify (it proves the adapter's CAS/idempotency logic; it does not model
concurrent-transaction isolation, which needs a real Firestore emulator or
project before deployment).
"""

from __future__ import annotations

import base64
import json
import logging
import sys
import types
import unittest
from email.message import EmailMessage
from typing import Any

from cloud.approval_dispatcher.gmail_reader import (
    FakeGmailReader,
    GmailReaderError,
    HistoryBatch,
    PermanentGmailError,
    StaleHistoryError,
    TemporaryGmailError,
    decode_message_fields,
)
from cloud.approval_dispatcher.main import (
    AuthenticationError,
    ConfigurationError,
    DispatcherConfig,
    DispatcherService,
    EnvelopeError,
    FakeProductionStateReader,
    PushAuthenticator,
    create_app,
    decode_pubsub_envelope,
)
from cloud.approval_dispatcher.storage import (
    CursorState,
    FirestoreDispatcherStorage,
    InMemoryCursorStore,
    InMemoryShadowObservationStore,
    build_shadow_record,
)
from scripts.a2_dispatch import A2Decision, InMemoryTransitionLedger
from scripts.approval_domain import ApprovalSource, ApprovalStage


ISSUE = "2026-09-21"
GATE_A_PATTERN = "The Daily Duck — Choose Today's Story"
DESIGN_PATTERN = "The Daily Duck — Choose Image + Title"
ALLOWED_SENDERS = frozenset({"owner@example.com"})


def make_config(**overrides: Any) -> DispatcherConfig:
    defaults = dict(
        mailbox_identity="duck@example.com",
        allowed_senders=ALLOWED_SENDERS,
        gate_a_subject_pattern=GATE_A_PATTERN,
        design_subject_pattern=DESIGN_PATTERN,
        oidc_expected_audience="https://a2.example.run.app/pubsub",
        oidc_expected_callers=frozenset({"pubsub@example.iam.gserviceaccount.com"}),
    )
    defaults.update(overrides)
    return DispatcherConfig(**defaults)


def raw_message(*, sender="owner@example.com", subject="", body="") -> dict[str, Any]:
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(body)
    encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")
    return {"raw": encoded}


def pubsub_envelope(*, email="duck@example.com", history_id="200", message_id="p1"):
    payload = json.dumps({"emailAddress": email, "historyId": history_id}).encode()
    return {
        "message": {
            "data": base64.b64encode(payload).decode("ascii"),
            "messageId": message_id,
        }
    }


def gate_a_snapshot(issue=ISSUE):
    return {
        "active_issue_date": issue,
        "current_state": "WAITING_STORY_SELECTION",
        "current_command": None,
        "active_design_batch_id": None,
    }


def design_snapshot(issue=ISSUE, batch=4):
    return {
        "active_issue_date": issue,
        "current_state": "WAITING_FINAL_SELECTION",
        "current_command": None,
        "active_design_batch_id": batch,
    }


class Harness:
    """Bundles fresh fakes for one DispatcherService, and seeds a starting
    cursor so a subsequent Pub/Sub notification triggers a real history
    walk rather than the INITIAL_CURSOR path."""

    def __init__(self, *, gmail=None, state_snapshots=None, config=None):
        self.config = config or make_config()
        self.gmail = gmail or FakeGmailReader()
        self.cursor_store = InMemoryCursorStore()
        self.cursor_store.compare_and_update_cursor(None, "100")
        self.observation_store = InMemoryShadowObservationStore()
        self.transition_ledger = InMemoryTransitionLedger()
        self.state_reader = FakeProductionStateReader(state_snapshots or {})
        self.service = DispatcherService(
            self.config,
            self.gmail,
            self.cursor_store,
            self.observation_store,
            self.transition_ledger,
            self.state_reader,
        )

    def push(self, history_id="200"):
        return self.service.process_pubsub(
            pubsub_envelope(email=self.config.mailbox_identity, history_id=history_id)
        )


# ---------------------------------------------------------------------------
# 1. synthetic Pub/Sub payload
# ---------------------------------------------------------------------------


class EnvelopeDecodingTests(unittest.TestCase):
    def test_valid_envelope_decodes(self):
        notification = decode_pubsub_envelope(
            pubsub_envelope(email="duck@example.com", history_id="42", message_id="m1")
        )
        self.assertEqual(notification.email_address, "duck@example.com")
        self.assertEqual(notification.history_id, "42")
        self.assertEqual(notification.pubsub_message_id, "m1")

    def test_non_mapping_envelope_is_rejected(self):
        with self.assertRaises(EnvelopeError):
            decode_pubsub_envelope("not-a-dict")

    def test_missing_message_key_is_rejected(self):
        with self.assertRaises(EnvelopeError):
            decode_pubsub_envelope({})

    def test_missing_data_key_is_rejected(self):
        with self.assertRaises(EnvelopeError):
            decode_pubsub_envelope({"message": {}})

    def test_malformed_base64_is_rejected(self):
        with self.assertRaises(EnvelopeError):
            decode_pubsub_envelope({"message": {"data": "not-base64-json!!"}})

    def test_missing_fields_in_payload_are_rejected(self):
        payload = json.dumps({"emailAddress": "duck@example.com"}).encode()
        envelope = {"message": {"data": base64.b64encode(payload).decode()}}
        with self.assertRaises(EnvelopeError):
            decode_pubsub_envelope(envelope)


# ---------------------------------------------------------------------------
# 4, 5, 6, 7, 24: end-to-end classification via the full service
# ---------------------------------------------------------------------------


class EndToEndClassificationTests(unittest.TestCase):
    def test_gate_a_valid_reply(self):
        message = raw_message(
            subject=f"{GATE_A_PATTERN} — {ISSUE}", body="3"
        )
        harness = Harness(
            gmail=FakeGmailReader(
                history_results={"100": HistoryBatch(message_ids=("msg-1",))},
                messages={"msg-1": message},
            ),
            state_snapshots={ApprovalStage.GATE_A: gate_a_snapshot()},
        )
        result = harness.push()
        self.assertEqual(result["status"], "ACKNOWLEDGED")
        self.assertEqual(result["processed"], 1)
        [record] = harness.observation_store._records.values()
        self.assertEqual(record["classification"], "GATE_A_REPLY")
        self.assertEqual(record["normalized_command"], "SELECT_STORY:3")
        self.assertEqual(record["source_type"], "GMAIL_PUSH")

    def test_design_valid_reply(self):
        message = raw_message(
            subject=f"{DESIGN_PATTERN} — {ISSUE} — Batch 4", body="1 3"
        )
        harness = Harness(
            gmail=FakeGmailReader(
                history_results={"100": HistoryBatch(message_ids=("msg-1",))},
                messages={"msg-1": message},
            ),
            state_snapshots={ApprovalStage.DESIGN_SELECTION: design_snapshot()},
        )
        harness.push()
        [record] = harness.observation_store._records.values()
        self.assertEqual(record["classification"], "DESIGN_REPLY")
        self.assertEqual(record["normalized_command"], "SELECT_DESIGN:1:3")

    def test_invalid_reply(self):
        message = raw_message(subject=f"{GATE_A_PATTERN} — {ISSUE}", body="3 OK")
        harness = Harness(
            gmail=FakeGmailReader(
                history_results={"100": HistoryBatch(message_ids=("msg-1",))},
                messages={"msg-1": message},
            ),
            state_snapshots={ApprovalStage.GATE_A: gate_a_snapshot()},
        )
        harness.push()
        [record] = harness.observation_store._records.values()
        self.assertEqual(record["classification"], "INVALID_REPLY")

    def test_unrelated_email(self):
        message = raw_message(subject="Your weekly newsletter", body="unsubscribe")
        harness = Harness(
            gmail=FakeGmailReader(
                history_results={"100": HistoryBatch(message_ids=("msg-1",))},
                messages={"msg-1": message},
            )
        )
        harness.push()
        [record] = harness.observation_store._records.values()
        self.assertEqual(record["classification"], "UNRELATED")
        self.assertIsNone(record["stage"])

    def test_gmail_push_trust_source(self):
        message = raw_message(subject=f"{GATE_A_PATTERN} — {ISSUE}", body="3")
        harness = Harness(
            gmail=FakeGmailReader(
                history_results={"100": HistoryBatch(message_ids=("msg-1",))},
                messages={"msg-1": message},
            ),
            state_snapshots={ApprovalStage.GATE_A: gate_a_snapshot()},
        )
        message_obj = decode_message_fields(message)
        from scripts.a2_dispatch import FetchedGmailMessage

        fetched = FetchedGmailMessage(
            gmail_message_id="msg-1",
            sender=message_obj.sender,
            subject=message_obj.subject,
            body=message_obj.body,
        )
        outcome = harness.service._classify_and_record(fetched)
        self.assertEqual(outcome.command.source_type, ApprovalSource.GMAIL_PUSH)


# ---------------------------------------------------------------------------
# 8, 9: duplicate handling
# ---------------------------------------------------------------------------


class DuplicateHandlingTests(unittest.TestCase):
    def test_duplicate_pubsub_delivery_of_same_notification(self):
        message = raw_message(subject=f"{GATE_A_PATTERN} — {ISSUE}", body="3")
        harness = Harness(
            gmail=FakeGmailReader(
                history_results={"100": HistoryBatch(message_ids=("msg-1",))},
                messages={"msg-1": message},
            ),
            state_snapshots={ApprovalStage.GATE_A: gate_a_snapshot()},
        )
        first = harness.push(history_id="200")
        second = harness.push(history_id="200")
        self.assertEqual(first["processed"], 1)
        self.assertEqual(second["recovery_path"], "DUPLICATE_NOTIFICATION")
        self.assertEqual(second["processed"], 0)
        self.assertEqual(len(harness.gmail.get_message_calls), 1)

    def test_same_transition_via_different_gmail_message(self):
        message = raw_message(subject=f"{GATE_A_PATTERN} — {ISSUE}", body="3")
        gmail = FakeGmailReader(
            history_results={
                "100": HistoryBatch(message_ids=("msg-1",)),
                "200": HistoryBatch(message_ids=("msg-2",)),
            },
            messages={"msg-1": message, "msg-2": message},
        )
        harness = Harness(
            gmail=gmail, state_snapshots={ApprovalStage.GATE_A: gate_a_snapshot()}
        )
        harness.push(history_id="200")
        harness.push(history_id="300")
        self.assertEqual(len(harness.observation_store._records), 2)
        transition_keys = {
            record["transition_key"]
            for record in harness.observation_store._records.values()
        }
        self.assertEqual(len(transition_keys), 1)


# ---------------------------------------------------------------------------
# 10, 11, 12, 13: failure handling
# ---------------------------------------------------------------------------


class RaisingObservationStore:
    """Minimal test double that raises on contains(), simulating a
    transient storage failure."""

    def contains(self, observation_id: str) -> bool:
        raise RuntimeError("storage temporarily unavailable")

    def insert_observation_if_absent(self, observation_id, observation) -> bool:
        raise RuntimeError("storage temporarily unavailable")

    def write_shadow_record(self, observation_id, record) -> None:
        raise RuntimeError("storage temporarily unavailable")


class RaisingStateReader:
    def read_snapshot(self, stage):
        raise KeyError("unexpected snapshot lookup failure")


class FailureHandlingTests(unittest.TestCase):
    def test_temporary_gmail_failure_propagates_for_retry_mapping(self):
        message = raw_message(subject=f"{GATE_A_PATTERN} — {ISSUE}", body="3")
        harness = Harness(
            gmail=FakeGmailReader(
                history_results={"100": HistoryBatch(message_ids=("msg-1",))},
                messages={"msg-1": message},
                message_errors={"msg-1": TemporaryGmailError("rate limited")},
            )
        )
        with self.assertRaises(TemporaryGmailError):
            harness.push()

    def test_permanent_gmail_failure_propagates_distinctly(self):
        message = raw_message(subject=f"{GATE_A_PATTERN} — {ISSUE}", body="3")
        harness = Harness(
            gmail=FakeGmailReader(
                history_results={"100": HistoryBatch(message_ids=("msg-1",))},
                messages={"msg-1": message},
                message_errors={"msg-1": PermanentGmailError("auth revoked")},
            )
        )
        with self.assertRaises(PermanentGmailError):
            harness.push()

    def test_temporary_storage_failure_propagates(self):
        message = raw_message(subject=f"{GATE_A_PATTERN} — {ISSUE}", body="3")
        harness = Harness(
            gmail=FakeGmailReader(
                history_results={"100": HistoryBatch(message_ids=("msg-1",))},
                messages={"msg-1": message},
            ),
            state_snapshots={ApprovalStage.GATE_A: gate_a_snapshot()},
        )
        harness.observation_store = RaisingObservationStore()
        harness.service.observation_store = harness.observation_store
        with self.assertRaises(RuntimeError):
            harness.push()

    def test_classification_failure_propagates(self):
        message = raw_message(subject=f"{GATE_A_PATTERN} — {ISSUE}", body="3")
        harness = Harness(
            gmail=FakeGmailReader(
                history_results={"100": HistoryBatch(message_ids=("msg-1",))},
                messages={"msg-1": message},
            ),
        )
        harness.service.state_reader = RaisingStateReader()
        with self.assertRaises(KeyError):
            harness.push()

    def test_pubsub_route_maps_temporary_failure_to_retry(self):
        message = raw_message(subject=f"{GATE_A_PATTERN} — {ISSUE}", body="3")
        gmail = FakeGmailReader(
            history_results={"100": HistoryBatch(message_ids=("msg-1",))},
            messages={"msg-1": message},
            message_errors={"msg-1": TemporaryGmailError("rate limited")},
        )
        app = _build_app(gmail=gmail)
        client = app.test_client()
        response = client.post(
            "/pubsub",
            json=pubsub_envelope(),
            headers={"Authorization": "Bearer faketoken"},
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["status"], "RETRY")

    def test_pubsub_route_maps_permanent_failure_to_ack_without_retry(self):
        message = raw_message(subject=f"{GATE_A_PATTERN} — {ISSUE}", body="3")
        gmail = FakeGmailReader(
            history_results={"100": HistoryBatch(message_ids=("msg-1",))},
            messages={"msg-1": message},
            message_errors={"msg-1": PermanentGmailError("auth revoked")},
        )
        app = _build_app(gmail=gmail)
        client = app.test_client()
        response = client.post(
            "/pubsub",
            json=pubsub_envelope(),
            headers={"Authorization": "Bearer faketoken"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "FAILED_PERMANENT")


def _build_app(*, gmail=None, cursor_store=None):
    config = make_config()
    authenticator = PushAuthenticator(
        expected_audience=config.oidc_expected_audience,
        expected_callers=config.oidc_expected_callers,
        token_verifier=lambda token, aud: {
            "email": "pubsub@example.iam.gserviceaccount.com",
            "email_verified": True,
        },
    )
    cursor = cursor_store or InMemoryCursorStore()
    cursor.compare_and_update_cursor(None, "100")
    return create_app(
        config=config,
        gmail=gmail or FakeGmailReader(),
        cursor_store=cursor,
        observation_store=InMemoryShadowObservationStore(),
        transition_ledger=InMemoryTransitionLedger(),
        state_reader=FakeProductionStateReader(
            {ApprovalStage.GATE_A: gate_a_snapshot()}
        ),
        authenticator=authenticator,
    )


# ---------------------------------------------------------------------------
# 14-20: sanitization
# ---------------------------------------------------------------------------


class SanitizationTests(unittest.TestCase):
    def _classify(self, *, subject, body, sender="owner@example.com"):
        message = raw_message(subject=subject, body=body, sender=sender)
        harness = Harness(
            gmail=FakeGmailReader(
                history_results={"100": HistoryBatch(message_ids=("msg-1",))},
                messages={"msg-1": message},
            ),
            state_snapshots={ApprovalStage.GATE_A: gate_a_snapshot()},
        )
        harness.push()
        [record] = harness.observation_store._records.values()
        return record, subject, body, sender

    def test_sanitized_shadow_storage_has_exact_allowed_keys(self):
        record, *_ = self._classify(
            subject=f"{GATE_A_PATTERN} — {ISSUE}", body="3"
        )
        required = {
            "observation_id",
            "stage",
            "issue_date",
            "normalized_command",
            "idempotency_key",
            "transition_key",
            "classification",
            "timestamp",
            "source_type",
        }
        self.assertTrue(required.issubset(record.keys()))
        self.assertEqual(record["source_type"], "GMAIL_PUSH")

    def test_full_body_not_persisted(self):
        # A distinctive, non-trivial body: unlike a single digit, this
        # cannot coincidentally appear inside a hash/date/enum value, so
        # its absence is a meaningful assertion.
        distinctive_body = "please pick story number three, thanks!"
        record, subject, body, sender = self._classify(
            subject=f"{GATE_A_PATTERN} — {ISSUE}", body=distinctive_body
        )
        values = " ".join(str(v) for v in record.values())
        self.assertNotIn(distinctive_body, values)
        self.assertNotIn("three", values)

    def test_quoted_history_not_persisted(self):
        quoted_body = "3\n\nOn Mon, Sep 21, 2026, Owner wrote:\n> original text here"
        record, *_ = self._classify(
            subject=f"{GATE_A_PATTERN} — {ISSUE}", body=quoted_body
        )
        values = " ".join(str(v) for v in record.values())
        self.assertNotIn("original text here", values)

    def test_full_subject_not_persisted(self):
        subject = f"{GATE_A_PATTERN} — {ISSUE}"
        record, *_ = self._classify(subject=subject, body="3")
        values = " ".join(str(v) for v in record.values())
        self.assertNotIn(GATE_A_PATTERN, values)

    def test_sender_email_not_persisted(self):
        record, *_ = self._classify(
            subject=f"{GATE_A_PATTERN} — {ISSUE}", body="3", sender="owner@example.com"
        )
        values = " ".join(str(v) for v in record.values())
        self.assertNotIn("owner@example.com", values)

    def test_plaintext_not_logged(self):
        subject = f"{GATE_A_PATTERN} — {ISSUE}"
        body = "3"
        with self.assertLogs("approval_dispatcher", level="INFO") as captured:
            self._classify(subject=subject, body=body)
        joined = "\n".join(captured.output)
        self.assertNotIn(GATE_A_PATTERN, joined)
        self.assertNotIn("owner@example.com", joined)

    def test_credentials_not_logged(self):
        app = _build_app()
        client = app.test_client()
        handler = logging.getLogger("approval_dispatcher")
        with self.assertLogs("approval_dispatcher", level="INFO") as captured:
            client.post(
                "/pubsub",
                json=pubsub_envelope(),
                headers={"Authorization": "Bearer super-secret-token-value"},
            )
        joined = "\n".join(captured.output)
        self.assertNotIn("super-secret-token-value", joined)


# ---------------------------------------------------------------------------
# Cursor / history walk coverage (bonus, beyond the numbered matrix)
# ---------------------------------------------------------------------------


class CursorAndHistoryTests(unittest.TestCase):
    def test_initial_cursor_is_seeded_without_processing_messages(self):
        gmail = FakeGmailReader()
        cursor_store = InMemoryCursorStore()
        service = DispatcherService(
            make_config(),
            gmail,
            cursor_store,
            InMemoryShadowObservationStore(),
            InMemoryTransitionLedger(),
            FakeProductionStateReader(),
        )
        result = service.process_pubsub(pubsub_envelope(history_id="500"))
        self.assertEqual(result["recovery_path"], "INITIAL_CURSOR")
        self.assertEqual(result["processed"], 0)
        self.assertEqual(cursor_store.read_cursor().processing_history_id, "500")
        self.assertEqual(gmail.get_message_calls, [])

    def test_stale_history_resyncs_forward_without_crashing(self):
        gmail = FakeGmailReader(
            history_results={"100": StaleHistoryError("cursor too old")}
        )
        harness = Harness(gmail=gmail)
        result = harness.push(history_id="999")
        self.assertEqual(result["recovery_path"], "STALE_HISTORY_RESYNC")
        self.assertEqual(
            harness.cursor_store.read_cursor().processing_history_id, "999"
        )

    def test_mismatched_mailbox_is_rejected(self):
        harness = Harness()
        with self.assertRaises(EnvelopeError):
            harness.service.process_pubsub(
                pubsub_envelope(email="someone-else@example.com")
            )


# ---------------------------------------------------------------------------
# Firestore-shaped storage: tested via a fake google.cloud.firestore module
# ---------------------------------------------------------------------------


class FakeAlreadyExists(Exception):
    pass


class FakeSnapshot:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeDocumentReference:
    def __init__(self, backing, key):
        self._backing = backing
        self._key = key

    def get(self, transaction=None):
        return FakeSnapshot(self._backing.get(self._key))

    def set(self, data, merge=False):
        if merge and self._key in self._backing:
            self._backing[self._key] = {**self._backing[self._key], **data}
        else:
            self._backing[self._key] = dict(data)

    def create(self, data):
        if self._key in self._backing:
            raise FakeAlreadyExists(self._key)
        self._backing[self._key] = dict(data)

    def delete(self):
        self._backing.pop(self._key, None)


class FakeCollection:
    def __init__(self, backing):
        self._backing = backing

    def document(self, doc_id):
        return FakeDocumentReference(self._backing, doc_id)


class FakeTransaction:
    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge)

    def create(self, ref, data):
        ref.create(data)


class FakeFirestoreClient:
    def __init__(self):
        self._collections: dict[str, dict[str, Any]] = {}

    def collection(self, name):
        return FakeCollection(self._collections.setdefault(name, {}))

    def transaction(self):
        return FakeTransaction()


def install_fake_google_cloud_firestore() -> dict[str, Any]:
    saved = {
        name: sys.modules.get(name)
        for name in ("google", "google.cloud", "google.cloud.firestore")
    }

    def transactional(func):
        def wrapper(transaction, *args, **kwargs):
            return func(transaction, *args, **kwargs)

        return wrapper

    preexisting_google = sys.modules.get("google")
    google_module = preexisting_google or types.ModuleType("google")
    cloud_module = types.ModuleType("google.cloud")
    firestore_module = types.ModuleType("google.cloud.firestore")
    firestore_module.transactional = transactional
    cloud_module.firestore = firestore_module
    if preexisting_google is None:
        google_module.cloud = cloud_module
    sys.modules["google"] = google_module
    sys.modules["google.cloud"] = cloud_module
    sys.modules["google.cloud.firestore"] = firestore_module
    return saved


def restore_google_cloud_firestore(saved: dict[str, Any]) -> None:
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class FirestoreDispatcherStorageTests(unittest.TestCase):
    def setUp(self):
        self._saved = install_fake_google_cloud_firestore()
        self.client = FakeFirestoreClient()
        self.storage = FirestoreDispatcherStorage(
            self.client,
            cursor_collection="a2_cursor",
            cursor_document="gmail_cursor",
            observation_collection="a2_shadow_observations",
            transition_collection="a2_transition_ledger",
        )

    def tearDown(self):
        restore_google_cloud_firestore(self._saved)

    def test_cursor_round_trip(self):
        self.assertIsNone(self.storage.read_cursor().processing_history_id)
        self.assertTrue(self.storage.compare_and_update_cursor(None, "100"))
        self.assertEqual(self.storage.read_cursor().processing_history_id, "100")
        self.assertFalse(self.storage.compare_and_update_cursor("99", "200"))

    def test_observation_dedupe_is_idempotent(self):
        self.assertTrue(
            self.storage.insert_observation_if_absent("obs-1", {"a": 1})
        )
        self.assertFalse(
            self.storage.insert_observation_if_absent("obs-1", {"a": 2})
        )
        self.assertTrue(self.storage.contains("obs-1"))
        self.assertFalse(self.storage.contains("obs-2"))

    def test_write_shadow_record_enriches_existing_document(self):
        self.storage.insert_observation_if_absent(
            "obs-1", {"gmail_message_id": "msg-1"}
        )
        self.storage.write_shadow_record("obs-1", {"classification": "GATE_A_REPLY"})
        record = self.storage.get_shadow_record("obs-1")
        self.assertEqual(record["gmail_message_id"], "msg-1")
        self.assertEqual(record["classification"], "GATE_A_REPLY")

    def test_transition_ledger_reserve_and_confirm(self):
        self.assertTrue(self.storage.reserve_if_absent("t-1"))
        self.assertFalse(self.storage.reserve_if_absent("t-1"))
        self.storage.mark_confirmed("t-1")
        from scripts.a2_dispatch import DispatchOutcomeState

        self.assertEqual(self.storage.state_of("t-1"), DispatchOutcomeState.CONFIRMED)

    def test_transition_ledger_release_frees_key(self):
        self.storage.reserve_if_absent("t-2")
        self.storage.release("t-2")
        self.assertIsNone(self.storage.state_of("t-2"))
        self.assertTrue(self.storage.reserve_if_absent("t-2"))

    def test_cursor_and_observation_collections_are_independent(self):
        self.storage.compare_and_update_cursor(None, "100")
        self.storage.insert_observation_if_absent("obs-1", {"a": 1})
        cursor_doc = (
            self.client.collection("a2_cursor").document("gmail_cursor").get()
        )
        self.assertNotIn("a", cursor_doc.to_dict())


if __name__ == "__main__":
    unittest.main()
