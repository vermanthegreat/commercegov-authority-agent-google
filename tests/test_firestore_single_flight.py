from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import google.cloud
import pytest

from app.models import ClaimResult, ChangeEvent, WorkflowStatus
from app.services.firestore_store import FirestoreRunStore


SERVER_TIMESTAMP = object()


class FakeSnapshot:
    def __init__(self, data, read_time):
        self.exists = data is not None
        self._data = deepcopy(data)
        self.read_time = read_time

    def to_dict(self):
        return deepcopy(self._data)


class FakeDocument:
    def __init__(self, client, event_id):
        self.client = client
        self.event_id = event_id

    def get(self, transaction=None):
        return FakeSnapshot(self.client.documents.get(self.event_id), self.client.server_time)


class FakeCollection:
    def __init__(self, client):
        self.client = client

    def document(self, event_id):
        return FakeDocument(self.client, event_id)


class FakeTransaction:
    def __init__(self, client):
        self.client = client

    def _materialize(self, fields):
        return {
            key: self.client.server_time if value is SERVER_TIMESTAMP else deepcopy(value)
            for key, value in fields.items()
        }

    def create(self, doc, fields):
        if doc.event_id in self.client.documents:
            raise RuntimeError("transaction conflict")
        self.client.documents[doc.event_id] = self._materialize(fields)

    def update(self, doc, fields):
        self.client.documents[doc.event_id].update(self._materialize(fields))


class FakeFirestoreClient:
    def __init__(self):
        self.documents = {}
        self.server_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def collection(self, name):
        return FakeCollection(self)

    def transaction(self):
        return FakeTransaction(self)


def transactional(function):
    def execute(transaction):
        return function(transaction)
    return execute


@pytest.fixture
def firestore_store(monkeypatch):
    module = SimpleNamespace(SERVER_TIMESTAMP=SERVER_TIMESTAMP, transactional=transactional)
    monkeypatch.setattr(google.cloud, "firestore", module, raising=False)
    client = FakeFirestoreClient()
    store = FirestoreRunStore.__new__(FirestoreRunStore)
    store._client = client
    return store, client


@pytest.fixture
def event():
    return ChangeEvent(
        event_id="evt-firestore",
        change_id="chg-firestore",
        shop_id="shop",
        target_type="product",
        target_id="1",
        mutation_class="product.title",
        current_value="old",
        proposed_value="new",
    )


def test_firestore_server_time_controls_processing_recovery(firestore_store, event, monkeypatch):
    store, client = firestore_store
    monkeypatch.setattr("app.services.firestore_store._now", lambda: "2999-01-01T00:00:00+00:00")
    assert store.claim_event(event, "owner-a", lease_seconds=60)[0] == ClaimResult.CLAIM_ACQUIRED

    client.server_time += timedelta(seconds=59)
    monkeypatch.setattr("app.services.firestore_store._now", lambda: "1800-01-01T00:00:00+00:00")
    assert store.claim_event(event, "owner-b", lease_seconds=60)[0] == ClaimResult.IN_PROGRESS

    client.server_time += timedelta(seconds=2)
    result, recovered = store.claim_event(event, "owner-b", lease_seconds=60)
    assert result == ClaimResult.STALE_CLAIM_RECOVERED
    assert recovered["attempt"] == 2


def test_firestore_assessing_is_never_recovered(firestore_store, event):
    store, client = firestore_store
    _, run = store.claim_event(event, "owner-a", lease_seconds=1)
    store.begin_assessment(event.event_id, "owner-a", run["attempt"])
    client.server_time += timedelta(days=365)

    result, existing = store.claim_event(event, "owner-b", lease_seconds=1)

    assert result == ClaimResult.IN_PROGRESS
    assert existing["status"] == WorkflowStatus.ASSESSING.value
    assert existing["claim_owner_id"] == "owner-a"


def test_firestore_terminal_is_immutable_and_fenced(firestore_store, event):
    store, _ = firestore_store
    _, run = store.claim_event(event, "owner-a")
    store.begin_assessment(event.event_id, "owner-a", run["attempt"])
    store.settle(
        event.event_id,
        "owner-a",
        run["attempt"],
        status=WorkflowStatus.WAITING_FOR_HUMAN_AUTHORITY.value,
        reason="original",
    )
    original = store.get(event.event_id)

    preserved = store.settle(
        event.event_id,
        "owner-a",
        run["attempt"],
        status=WorkflowStatus.FAILED.value,
        reason="replacement",
    )
    with pytest.raises(RuntimeError, match="Stale owner"):
        store.settle(
            event.event_id,
            "owner-a",
            run["attempt"] + 1,
            status=WorkflowStatus.FAILED.value,
        )

    assert preserved == original
    assert store.get(event.event_id)["reason"] == "original"


def test_firestore_operations_use_transactional_read_modify_write():
    import inspect
    import app.services.firestore_store as module

    source = inspect.getsource(module.FirestoreRunStore)
    assert source.count("@firestore.transactional") == 4
    assert "snapshot.read_time - claimed_at" in source
    assert "firestore.SERVER_TIMESTAMP" in source
    assert "datetime.now" not in source

def test_local_clock_skew_cannot_create_dual_ownership(firestore_store, event, monkeypatch):
    """
    Demonstrates that worker-local clock skew cannot cause two workers to 
    independently own the same lease. The server time is the only authority.
    """
    store, client = firestore_store
    
    # Worker A claims with a clock far in the future
    monkeypatch.setattr("app.services.firestore_store._now", lambda: "2999-01-01T00:00:00+00:00")
    result_a, _ = store.claim_event(event, "owner-a", lease_seconds=60)
    assert result_a == ClaimResult.CLAIM_ACQUIRED

    # Worker B tries to claim exactly 1 second later (server time)
    client.server_time += timedelta(seconds=1)
    
    # Worker B has a clock far in the past (clock skew!)
    monkeypatch.setattr("app.services.firestore_store._now", lambda: "1800-01-01T00:00:00+00:00")
    
    # If B's clock was used, B might think A's claim is old. But server time is used.
    result_b, _ = store.claim_event(event, "owner-b", lease_seconds=60)
    
    # Worker B must not acquire the claim
    assert result_b == ClaimResult.IN_PROGRESS
