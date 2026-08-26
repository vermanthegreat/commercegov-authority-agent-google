from app.models import PipelineNamespace
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from threading import Barrier, Lock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.models import ClaimResult, ChangeEvent, WorkflowStatus
from app.routes.events import process_event
from app.services.firestore_store import InMemoryRunStore

def run_async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return wrapper


class ManualClock:
    def __init__(self, value: float = 0.0) -> None:
        self._value = value
        self._lock = Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._value

    def set(self, value: float) -> None:
        with self._lock:
            self._value = value


@pytest.fixture
def base_payload():
    return {
        "event_id": "evt_idem",
        "change_id": "chg_idem",
        "shop_id": "demo",
        "target_type": "product",
        "target_id": "123",
        "mutation_class": "product.title",
        "current_value": "old",
        "proposed_value": "new",
        "policy_context": {},
        "authority_context": {"requires_human_approval": False},
    }


def concurrent_claims(store, event, owners, lease_seconds=60):
    barrier = Barrier(len(owners))

    def claim(owner):
        barrier.wait()
        return owner, store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, owner, lease_seconds=lease_seconds)[0]

    with ThreadPoolExecutor(max_workers=len(owners)) as executor:
        return list(executor.map(claim, owners))


def test_threaded_concurrent_first_claim_has_one_owner(base_payload):
    store = InMemoryRunStore()
    event = ChangeEvent(**base_payload)

    results = concurrent_claims(store, event, ["owner-a", "owner-b"])

    assert [result for _, result in results].count(ClaimResult.CLAIM_ACQUIRED) == 1
    assert [result for _, result in results].count(ClaimResult.IN_PROGRESS) == 1


def test_threaded_concurrent_stale_recovery_has_one_owner(base_payload):
    clock = ManualClock()
    store = InMemoryRunStore(monotonic_clock=clock)
    event = ChangeEvent(**base_payload)
    assert store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, "owner-original", lease_seconds=10)[0] == ClaimResult.CLAIM_ACQUIRED
    clock.set(11)

    results = concurrent_claims(store, event, ["owner-a", "owner-b"], lease_seconds=10)

    assert [result for _, result in results].count(ClaimResult.STALE_CLAIM_RECOVERED) == 1
    assert [result for _, result in results].count(ClaimResult.IN_PROGRESS) == 1


@run_async_test
async def test_concurrent_delivery_calls_assessor_once(app_with_fake, base_payload):
    app, _, assessor, cg_client = app_with_fake
    original_assess = assessor.assess

    async def slow_assess(event):
        await asyncio.sleep(0.05)
        return await original_assess(event)

    assessor.assess = slow_assess
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.gather(
            client.post("/events/change", json=base_payload),
            client.post("/events/change", json=base_payload),
        )

    assert [response.status_code for response in responses] == [200, 200]
    assert assessor.calls == 1


def test_in_progress_duplicate_does_not_assess(app_with_fake, base_payload):
    app, store, assessor, cg_client = app_with_fake
    event = ChangeEvent(**base_payload)
    assert store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, "owner-1")[0] == ClaimResult.CLAIM_ACQUIRED

    response = TestClient(app).post("/events/change", json=base_payload)

    assert response.status_code == 200
    assert response.json()["status"] == WorkflowStatus.PROCESSING.value
    assert assessor.calls == 0


@run_async_test
async def test_long_assessment_beyond_processing_lease_remains_single_flight(base_payload, human_assessment):
    clock = ManualClock()
    store = InMemoryRunStore(monotonic_clock=clock)
    event = ChangeEvent(**base_payload)

    class BlockingAssessor:
        def __init__(self):
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def assess(self, assessed_event):
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return human_assessment.model_copy(update={"change_id": assessed_event.change_id})

    assessor = BlockingAssessor()
    from tests.conftest import FakeCommerceGovClient
    cg_client = FakeCommerceGovClient()
    first = asyncio.create_task(process_event(event, store, assessor, cg_client))
    await assessor.started.wait()
    clock.set(10_000)

    duplicate = await process_event(event, store, assessor, cg_client)
    assert duplicate["status"] == WorkflowStatus.ASSESSING.value
    assert assessor.calls == 1

    assessor.release.set()
    await first
    assert assessor.calls == 1


def test_worker_wall_clock_skew_cannot_expire_processing_claim(base_payload, monkeypatch):
    clock = ManualClock(100)
    store = InMemoryRunStore(monotonic_clock=clock)
    event = ChangeEvent(**base_payload)
    monkeypatch.setattr("app.services.firestore_store._now", lambda: "1900-01-01T00:00:00+00:00")
    assert store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, "owner-behind", lease_seconds=60)[0] == ClaimResult.CLAIM_ACQUIRED

    monkeypatch.setattr("app.services.firestore_store._now", lambda: "2999-01-01T00:00:00+00:00")
    assert store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, "owner-ahead", lease_seconds=60)[0] == ClaimResult.IN_PROGRESS

    monkeypatch.setattr("app.services.firestore_store._now", lambda: "1800-01-01T00:00:00+00:00")
    assert store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, "owner-behind-again", lease_seconds=60)[0] == ClaimResult.IN_PROGRESS


def test_stale_owner_and_owner_id_reuse_are_fenced(base_payload):
    clock = ManualClock()
    store = InMemoryRunStore(monotonic_clock=clock)
    event = ChangeEvent(**base_payload)
    _, run1 = store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, "owner-a", lease_seconds=1)
    clock.set(2)
    _, run2 = store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, "owner-b", lease_seconds=1)
    clock.set(4)
    result3, run3 = store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, "owner-a", lease_seconds=1)
    assert result3 == ClaimResult.STALE_CLAIM_RECOVERED
    assert run3["attempt"] > run2["attempt"] > run1["attempt"]
    store.begin_assessment(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event.event_id, "owner-a", run3["attempt"])

    with pytest.raises(RuntimeError, match="Stale owner"):
        store.settle(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event.event_id, "owner-a", run1["attempt"], status=WorkflowStatus.FAILED.value)


def test_terminal_settlement_is_monotonic_and_idempotent(base_payload):
    store = InMemoryRunStore()
    event = ChangeEvent(**base_payload)
    _, run = store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, "owner")
    store.begin_assessment(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event.event_id, "owner", run["attempt"])
    original = store.settle(
            PipelineNamespace.AUTHORITY_ASSESSMENT.value,
            event.event_id,
        "owner",
        run["attempt"],
        status=WorkflowStatus.WAITING_FOR_HUMAN_AUTHORITY.value,
        reason="original",
    )

    rejected_overwrite = store.settle(
            PipelineNamespace.AUTHORITY_ASSESSMENT.value,
            event.event_id,
        "owner",
        run["attempt"],
        status=WorkflowStatus.FAILED.value,
        reason="replacement",
    )
    idempotent = store.settle(
            PipelineNamespace.AUTHORITY_ASSESSMENT.value,
            event.event_id,
        "owner",
        run["attempt"],
        status=WorkflowStatus.WAITING_FOR_HUMAN_AUTHORITY.value,
        reason="different",
    )

    assert rejected_overwrite == original
    assert idempotent == original
    assert store.get(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event.event_id) == original


@run_async_test
async def test_successful_commit_ack_failure_cannot_downgrade(base_payload, human_assessment):
    class AckFailingStore(InMemoryRunStore):
        def __init__(self):
            super().__init__()
            self.failed_ack = False

        def settle(self, namespace, event_id, owner_id, attempt, **fields):
            result = super().settle(namespace, event_id, owner_id, attempt, **fields)
            if not self.failed_ack and fields.get("status") == WorkflowStatus.WAITING_FOR_HUMAN_AUTHORITY.value:
                self.failed_ack = True
                raise TimeoutError("commit acknowledged by store but not caller")
            return result

    class Assessor:
        async def assess(self, event):
            return human_assessment.model_copy(update={"change_id": event.change_id})

    store = AckFailingStore()
    from tests.conftest import FakeCommerceGovClient
    with pytest.raises(TimeoutError):
        await process_event(ChangeEvent(**base_payload), store, Assessor(), FakeCommerceGovClient())

    persisted = store.get(PipelineNamespace.AUTHORITY_ASSESSMENT.value, base_payload["event_id"])
    assert persisted["status"] == WorkflowStatus.WAITING_FOR_HUMAN_AUTHORITY.value
    assert persisted["reason"] != "Authority assessment failed"


@run_async_test
async def test_post_settlement_read_failure_cannot_downgrade(base_payload, human_assessment):
    class ReadFailingStore(InMemoryRunStore):
        def get(self, namespace, event_id):
            run = super().get(namespace, event_id)
            if run and run["status"] == WorkflowStatus.WAITING_FOR_HUMAN_AUTHORITY.value:
                raise OSError("response read failed")
            return run

    class Assessor:
        async def assess(self, event):
            return human_assessment.model_copy(update={"change_id": event.change_id})

    store = ReadFailingStore()
    from tests.conftest import FakeCommerceGovClient
    with pytest.raises(OSError):
        await process_event(ChangeEvent(**base_payload), store, Assessor(), FakeCommerceGovClient())

    persisted = InMemoryRunStore.get(store, PipelineNamespace.AUTHORITY_ASSESSMENT.value, base_payload["event_id"])
    assert persisted["status"] == WorkflowStatus.WAITING_FOR_HUMAN_AUTHORITY.value


@run_async_test
async def test_ambiguous_assessor_failure_fails_closed_without_retry(base_payload):
    class FailingAssessor:
        def __init__(self):
            self.calls = 0

        async def assess(self, event):
            self.calls += 1
            raise TimeoutError("request outcome unknown")

    store = InMemoryRunStore()
    assessor = FailingAssessor()
    event = ChangeEvent(**base_payload)
    from tests.conftest import FakeCommerceGovClient
    with pytest.raises(RuntimeError, match="outcome is unknown"):
        await process_event(event, store, assessor, FakeCommerceGovClient())

    assert store.get(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event.event_id)["status"] == WorkflowStatus.ASSESSMENT_OUTCOME_UNKNOWN.value
    replay = await process_event(event, store, assessor, FakeCommerceGovClient())
    assert replay["status"] == WorkflowStatus.ASSESSMENT_OUTCOME_UNKNOWN.value
    assert assessor.calls == 1


def test_terminal_replay_and_event_conflict_preserve_record(app_with_fake, base_payload):
    app, store, assessor, cg_client = app_with_fake
    client = TestClient(app)
    first = client.post("/events/change", json=base_payload)
    original = store.get(PipelineNamespace.AUTHORITY_ASSESSMENT.value, base_payload["event_id"])

    replay = client.post("/events/change", json=base_payload)
    conflict_payload = base_payload | {"proposed_value": "different"}
    conflict = client.post("/events/change", json=conflict_payload)

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert conflict.status_code == 409
    assert store.get(PipelineNamespace.AUTHORITY_ASSESSMENT.value, base_payload["event_id"]) == original
    assert assessor.calls == 1
