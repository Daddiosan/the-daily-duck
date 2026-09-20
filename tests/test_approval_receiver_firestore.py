"""Unit coverage for the CAS and idempotency logic in FirestoreObservationStore.

No Firestore emulator, no Google Cloud connection, no network access.

FirestoreObservationStore imports "from google.cloud import firestore"
lazily, inside compare_and_update_cursor() and insert_observation_if_absent()
only, to reach one symbol: the firestore.transactional decorator. That
package is a Cloud Run deployment-time dependency (see
cloud/approval_receiver/requirements.txt) that is intentionally not part of
this local dev environment; installing it here would be an environment
mutation outside the authorized, test-file-only scope of this task.

Rather than skip this coverage, this file stubs exactly that one decorator
into sys.modules for the duration of each test, restoring the previous
state afterward. A stub is appropriate here because the adapter under test
never relies on the transaction-retry machinery of the real Firestore
client; it only calls the transactional decorator to turn a function that
takes a transaction and returns a bool into a callable it can invoke once
with the transaction object it already built. A passthrough decorator
satisfies exactly that contract. Everything else under test, namely the
compare-and-set cursor logic, the separation between the watch fields and
the processing cursor, and the insert-if-absent idempotency, is code that
belongs to this project and runs for real. The fake Firestore client,
document reference, and transaction defined below model how the adapter
uses the Firestore API surface; they do not attempt to model the internals
of Firestore itself.
"""

import sys
import types
import unittest
from typing import Any

from cloud.approval_receiver.observation import FirestoreObservationStore


class FakeAlreadyExists(Exception):
    """Mirrors a real Firestore create conflict. It is never expected to be
    raised by the adapter logic under test here, because the adapter always
    checks document existence before creating, within one attempt of an
    unretried transaction."""


class FakeSnapshot:
    def __init__(self, data: dict | None) -> None:
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict | None:
        return dict(self._data) if self._data is not None else None


class FakeDocumentReference:
    def __init__(self, backing: dict[str, Any], key: str) -> None:
        self._backing = backing
        self._key = key

    def get(self, transaction=None) -> FakeSnapshot:
        return FakeSnapshot(self._backing.get(self._key))

    def set(self, data: dict, merge: bool = False) -> None:
        if merge and self._key in self._backing:
            self._backing[self._key] = {**self._backing[self._key], **data}
        else:
            self._backing[self._key] = dict(data)

    def create(self, data: dict) -> None:
        if self._key in self._backing:
            raise FakeAlreadyExists(self._key)
        self._backing[self._key] = dict(data)


class FakeCollection:
    def __init__(self, backing: dict[str, Any]) -> None:
        self._backing = backing

    def document(self, doc_id: str) -> FakeDocumentReference:
        return FakeDocumentReference(self._backing, doc_id)


class FakeTransaction:
    """Only the two methods the adapter calls on a transaction. The real
    google.cloud.firestore.Transaction batches writes and commits them
    atomically on success; this fake applies them immediately, which is
    sufficient to test the CAS and idempotency logic that belongs to this
    adapter, since every call here is a single, unretried attempt.
    """

    def set(self, ref: FakeDocumentReference, data: dict, merge: bool = False) -> None:
        ref.set(data, merge=merge)

    def create(self, ref: FakeDocumentReference, data: dict) -> None:
        ref.create(data)


class FakeFirestoreClient:
    """Backs each named collection with its own independent dict, matching
    the way real Firestore collections are independent of one another."""

    def __init__(self) -> None:
        self._collections: dict[str, dict[str, Any]] = {}

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self._collections.setdefault(name, {}))

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()


def install_fake_google_cloud_firestore() -> dict[str, Any]:
    """Stubs sys.modules so that "from google.cloud import firestore"
    resolves locally. Returns the prior entries so the caller can restore
    them exactly, leaving no residue for any other test module.
    """
    saved = {
        name: sys.modules.get(name)
        for name in ("google", "google.cloud", "google.cloud.firestore")
    }

    def transactional(func):
        def wrapper(transaction, *args, **kwargs):
            return func(transaction, *args, **kwargs)

        return wrapper

    google_module = sys.modules.get("google") or types.ModuleType("google")
    cloud_module = types.ModuleType("google.cloud")
    firestore_module = types.ModuleType("google.cloud.firestore")
    firestore_module.transactional = transactional
    cloud_module.firestore = firestore_module
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


class FirestoreObservationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_modules = install_fake_google_cloud_firestore()
        self.client = FakeFirestoreClient()
        self.store = FirestoreObservationStore(
            self.client,
            cursor_collection="approval_receiver",
            cursor_document="gmail_cursor",
            observation_collection="approval_observations",
        )

    def tearDown(self) -> None:
        restore_google_cloud_firestore(self._saved_modules)

    def test_read_cursor_defaults_when_document_absent(self):
        cursor = self.store.read_cursor()
        self.assertIsNone(cursor.processing_history_id)
        self.assertIsNone(cursor.watch_history_id)
        self.assertIsNone(cursor.watch_expiration_ms)

    def test_compare_and_update_cursor_succeeds_when_expected_matches(self):
        self.assertTrue(self.store.compare_and_update_cursor(None, "100"))
        self.assertEqual(self.store.read_cursor().processing_history_id, "100")

    def test_compare_and_update_cursor_fails_on_stale_expectation(self):
        self.assertTrue(self.store.compare_and_update_cursor(None, "100"))
        self.assertFalse(self.store.compare_and_update_cursor("99", "200"))
        self.assertEqual(self.store.read_cursor().processing_history_id, "100")

    def test_watch_update_is_independent_of_processing_cursor(self):
        self.store.compare_and_update_cursor(None, "100")
        self.store.update_watch("300", 1790424000000)
        cursor = self.store.read_cursor()
        self.assertEqual(cursor.processing_history_id, "100")
        self.assertEqual(cursor.watch_history_id, "300")
        self.assertEqual(cursor.watch_expiration_ms, 1790424000000)

    def test_insert_observation_if_absent_is_idempotent(self):
        first = self.store.insert_observation_if_absent("obs-1", {"a": 1})
        second = self.store.insert_observation_if_absent("obs-1", {"a": 2})
        self.assertTrue(first)
        self.assertFalse(second)
        stored = (
            self.client.collection("approval_observations")
            .document("obs-1")
            .get()
            .to_dict()
        )
        self.assertEqual(stored, {"a": 1})

    def test_distinct_observation_ids_do_not_collide(self):
        self.assertTrue(self.store.insert_observation_if_absent("obs-1", {"a": 1}))
        self.assertTrue(self.store.insert_observation_if_absent("obs-2", {"a": 2}))

    def test_observation_and_cursor_collections_are_independent(self):
        self.store.compare_and_update_cursor(None, "100")
        self.store.insert_observation_if_absent("obs-1", {"a": 1})
        cursor_doc = (
            self.client.collection("approval_receiver").document("gmail_cursor").get()
        )
        self.assertTrue(cursor_doc.exists)
        self.assertNotIn("a", cursor_doc.to_dict())


if __name__ == "__main__":
    unittest.main()
