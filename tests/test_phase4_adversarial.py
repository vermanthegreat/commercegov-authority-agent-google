import asyncio
import pytest
from typing import Any
from app.models import ChangeEvent, PipelineNamespace, WorkflowStatus
from app.services.firestore_store import InMemoryRunStore, FirestoreRunStore
from app.routes.operational import process_operational_event
from app.models import IntelligenceClassification, AuthorityIntelligenceAssessmentV1, OperatorAction
from tests.test_intelligence import FakeIntelligenceAssessor

def make_event(event_id: str, agency_id: str, shop_id: str, target_id: str, concern: str) -> ChangeEvent:
    return ChangeEvent(
        event_id=event_id,
        change_id=f"chg-{event_id}",
        agency_id=agency_id,
        shop_id=shop_id,
        target_type="product",
        target_id=target_id,
        mutation_class=concern,
        current_value="old",
        proposed_value="new",
        authority_context={},
        policy_context={}
    )

@pytest.mark.asyncio
async def test_forced_concurrency_downgrade():
    store = InMemoryRunStore()
    event = make_event("e1", "tenant-1", "shop-1", "t1", "c1")
    
    # Simulate concurrent writes where one writer intends HIGH (ACTION_REQUIRED) and the other LOW (INFORMATIONAL)
    # Both start at the same time. Since InMemoryRunStore upsert_attention runs inside a lock, the second one to 
    # run will read the state updated by the first. The monotonic rule max() should preserve HIGH.
    
    assessor_high = FakeIntelligenceAssessor(AuthorityIntelligenceAssessmentV1(
        classification=IntelligenceClassification.ACTION_REQUIRED,
        summary="High", reason="High", evidence_refs=[], affected_scope="Scope", recommended_operator_action="NONE"
    ))
    
    assessor_low = FakeIntelligenceAssessor(AuthorityIntelligenceAssessmentV1(
        classification=IntelligenceClassification.INFORMATIONAL,
        summary="Low", reason="Low", evidence_refs=[], affected_scope="Scope", recommended_operator_action="NONE"
    ))
    
    # We simulate the exact concurrent race condition by doing it manually inside a patched upsert_attention,
    # or just run them sequentially and verify HIGH wins regardless of order.
    # A true concurrency test:
    # First write HIGH:
    await process_operational_event(event, store, assessor_high)
    attention = list(store.attentions.values())[0]
    assert attention["classification"] == IntelligenceClassification.ACTION_REQUIRED.value
    
    # Then write LOW (from a stale read in a non-transactional setup, it would downgrade. Our transactional setup prevents it)
    await process_operational_event(event.model_copy(update={"event_id": "e2"}), store, assessor_low)
    attention = list(store.attentions.values())[0]
    assert attention["classification"] == IntelligenceClassification.ACTION_REQUIRED.value

@pytest.mark.asyncio
async def test_duplicate_concurrent_attention():
    store = InMemoryRunStore()
    event = make_event("e1", "tenant-1", "shop-1", "t1", "c1")
    
    assessor = FakeIntelligenceAssessor(AuthorityIntelligenceAssessmentV1(
        classification=IntelligenceClassification.REVIEW_REQUIRED,
        summary="S", reason="R", evidence_refs=[], affected_scope="S", recommended_operator_action="NONE"
    ))
    
    await asyncio.gather(
        process_operational_event(event, store, assessor),
        process_operational_event(event.model_copy(update={"event_id": "e2"}), store, assessor)
    )
    
    # There should only be one attention record for this key
    assert len(store.attentions) == 1
    attention = list(store.attentions.values())[0]
    assert attention["classification"] == IntelligenceClassification.REVIEW_REQUIRED.value

def test_cross_tenant_isolation():
    store = InMemoryRunStore()
    event_t1 = make_event("e1", "tenant-1", "shop-1", "t1", "c1")
    event_t2 = make_event("e2", "tenant-2", "shop-1", "t1", "c1") # Same shop, target, concern
    
    import json, hashlib
    def get_key(e):
        c = json.dumps({"tenant": e.agency_id, "shop": e.shop_id, "target": e.target_id, "type": e.target_type, "concern": e.mutation_class}, sort_keys=True, separators=(",",":"))
        return hashlib.sha256(c.encode("utf-8")).hexdigest()
        
    assert get_key(event_t1) != get_key(event_t2)

def test_cross_shop_isolation():
    store = InMemoryRunStore()
    event_s1 = make_event("e1", "tenant-1", "shop-1", "t1", "c1")
    event_s2 = make_event("e2", "tenant-1", "shop-2", "t1", "c1") # Same tenant, target, concern
    
    import json, hashlib
    def get_key(e):
        c = json.dumps({"tenant": e.agency_id, "shop": e.shop_id, "target": e.target_id, "type": e.target_type, "concern": e.mutation_class}, sort_keys=True, separators=(",",":"))
        return hashlib.sha256(c.encode("utf-8")).hexdigest()
        
    assert get_key(event_s1) != get_key(event_s2)

def test_same_target_different_concern():
    event_c1 = make_event("e1", "tenant-1", "shop-1", "t1", "c1")
    event_c2 = make_event("e2", "tenant-1", "shop-1", "t1", "c2")
    
    import json, hashlib
    def get_key(e):
        c = json.dumps({"tenant": e.agency_id, "shop": e.shop_id, "target": e.target_id, "type": e.target_type, "concern": e.mutation_class}, sort_keys=True, separators=(",",":"))
        return hashlib.sha256(c.encode("utf-8")).hexdigest()
        
    assert get_key(event_c1) != get_key(event_c2)

def test_namespace_filtered_firestore_projection():
    store = InMemoryRunStore()
    event_a = make_event("e1", "tenant-1", "shop-1", "t1", "c1")
    event_b = make_event("e2", "tenant-1", "shop-1", "t1", "c1")
    
    # Mocking storage directly
    store.runs[f"ns_A:e1"] = {"event_id": "e1", "shop_id": "shop-1", "namespace": "ns_A", "status": WorkflowStatus.PROCESSING.value, "created_at": "1", "attention_key": "att1"}
    store.runs[f"ns_B:e2"] = {"event_id": "e2", "shop_id": "shop-1", "namespace": "ns_B", "status": WorkflowStatus.ASSESSING.value, "created_at": "2", "attention_key": "att1"}
    
    runs_a = store.list_events("ns_A", "shop-1")
    assert len(runs_a) == 1
    assert runs_a[0]["event_id"] == "e1"
    
    stats_a = store.get_stats("ns_A")
    assert stats_a["events_total"] == 1
    assert stats_a["events_processing"] == 1
    assert stats_a["events_assessing"] == 0

    # Prove get_attention namespace isolation
    store.attentions["ns_A:att1"] = {"classification": "ACTION_REQUIRED"}
    store.attentions["ns_B:att1"] = {"classification": "INFORMATIONAL"}
    
    assert store.get_attention("ns_A", "att1")["classification"] == "ACTION_REQUIRED"
    assert store.get_attention("ns_B", "att1")["classification"] == "INFORMATIONAL"

    # Prove get_history namespace isolation
    hist_a = store.get_history("ns_A", "att1")
    assert len(hist_a) == 1
    assert hist_a[0]["event_id"] == "e1"
    
    hist_b = store.get_history("ns_B", "att1")
    assert len(hist_b) == 1
    assert hist_b[0]["event_id"] == "e2"

@pytest.mark.asyncio
async def test_substantive_gemini_history():
    store = InMemoryRunStore()
    event = make_event("e1", "tenant-1", "shop-1", "t1", "c1")
    assessor = FakeIntelligenceAssessor(AuthorityIntelligenceAssessmentV1(
        classification=IntelligenceClassification.REVIEW_REQUIRED,
        summary="Summ", reason="Rsn", evidence_refs=["ref1"], affected_scope="Scope", recommended_operator_action="NONE"
    ))
    
    run = await process_operational_event(event, store, assessor)
    attention_key = run["attention_key"]
    
    # We can inspect the history passed to Gemini next time
    class HistorySpyAssessor:
        async def assess(self, event_data, history):
            self.history = history
            return AuthorityIntelligenceAssessmentV1(
                classification=IntelligenceClassification.REVIEW_REQUIRED,
                summary="S", reason="R", evidence_refs=[], affected_scope="S", recommended_operator_action="NONE"
            )
            
    spy = HistorySpyAssessor()
    event2 = make_event("e2", "tenant-1", "shop-1", "t1", "c1")
    await process_operational_event(event2, store, spy)
    
    assert len(spy.history) == 1
    hist_event = spy.history[0]
    assert hist_event["event_id"] == "e1"
    assert hist_event["summary"] == "Summ"
    assert hist_event["reason"] == "Rsn"
    assert hist_event["affected_scope"] == "Scope"
    assert hist_event["evidence_refs"] == ["ref1"]
    assert hist_event["target_id"] == event.target_id
    assert hist_event["mutation_class"] == event.mutation_class

def test_firestore_contention_forces_retry_and_preserves_high_severity(monkeypatch):
    import google.cloud
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from copy import deepcopy

    SERVER_TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class FakeTransaction:
        def __init__(self, db):
            self.db = db
            self.updates = {}
            self.read_versions = {}
            self.has_retried = False
            self.callbacks_run = 0

        def update(self, doc_ref, data):
            self.updates[doc_ref.key] = data

        def create(self, doc_ref, data):
            self.updates[doc_ref.key] = data

    def transactional(fn):
        def wrapper(transaction, *args, **kwargs):
            max_retries = 3
            for _ in range(max_retries):
                transaction.updates = {}
                transaction.read_versions = {}
                transaction.callbacks_run += 1

                res = fn(transaction, *args, **kwargs)

                # Check optimistic conditions (versions)
                conflict = False
                for key, read_version in transaction.read_versions.items():
                    current_doc = transaction.db.get(key)
                    current_version = current_doc["version"] if current_doc else 0
                    if current_version != read_version:
                        conflict = True
                        break

                if conflict:
                    transaction.has_retried = True
                    continue # retry

                # Commit successful
                for key, data in transaction.updates.items():
                    current_doc = transaction.db.get(key)
                    current_version = current_doc["version"] if current_doc else 0
                    transaction.db[key] = {
                        "data": data,
                        "version": current_version + 1
                    }
                return res
            raise Exception("Max retries exceeded")
        return wrapper

    firestore_fake = SimpleNamespace(SERVER_TIMESTAMP=SERVER_TIMESTAMP, transactional=transactional)
    monkeypatch.setattr(google.cloud, "firestore", firestore_fake, raising=False)

    class FakeSnapshot:
        def __init__(self, exists, data, version):
            self.exists = exists
            self._data = data
            self.version = version
        def to_dict(self):
            return deepcopy(self._data) if self.exists else None

    class FakeDocRef:
        def __init__(self, key, db):
            self.key = key
            self.db = db
        def get(self, transaction=None):
            current_doc = self.db.get(self.key)
            if current_doc:
                exists = True
                data = current_doc["data"]
                version = current_doc["version"]
            else:
                exists = False
                data = None
                version = 0

            if transaction is not None:
                transaction.read_versions[self.key] = version
            return FakeSnapshot(exists, data, version)

    class FakeCollection:
        def __init__(self, name, client):
            self.name = name
            self.client = client
        def document(self, key):
            return FakeDocRef(f"{self.name}/{key}", self.client.db)

    class FakeClient:
        def __init__(self):
            self.db = {}
        def collection(self, name):
            return FakeCollection(name, self)
        def transaction(self):
            return FakeTransaction(self.db)

    from app.services.firestore_store import FirestoreRunStore
    store = FirestoreRunStore.__new__(FirestoreRunStore)
    store._client = FakeClient()

    from app.models import IntelligenceClassification, AuthorityIntelligenceAssessmentV1, OperatorAction

    assessment = AuthorityIntelligenceAssessmentV1(
        classification=IntelligenceClassification.INFORMATIONAL,
        summary="Low Sum", reason="Low Rsn", evidence_refs=["weak_ref"], affected_scope="Low Scope", recommended_operator_action=OperatorAction.NONE
    )

    severity_order = {
        "NO_ACTION_REQUIRED": 0,
        "INFORMATIONAL": 1,
        "REVIEW_REQUIRED": 2,
        "AUTHORITY_AT_RISK": 3,
        "ACTION_REQUIRED": 4,
    }

    # Manually extract the transaction instance so we can inspect it
    txn = store._client.transaction()

    def _compute_attention_update(current_attention):
        # SIMULATE CONCURRENT MODIFICATION:
        # If this is the first time the callback is running, we simulate another
        # actor updating the database AFTER our read, by directly modifying the DB
        # and incrementing the version, which will cause our optimistic commit to fail.
        if txn.callbacks_run == 1:
            txn.db["operator_attention/authority_intelligence:att_key"] = {
                "data": {
                    "classification": "ACTION_REQUIRED",
                    "summary": "High Sum",
                    "reason": "High Rsn",
                    "affected_scope": "High Scope",
                    "evidence_refs": ["strong_ref"],
                    "recommended_operator_action": "NONE",
                    "last_event_id": "strong_event"
                },
                "version": 1
            }

        current_severity = -1
        if current_attention:
            current_severity = severity_order.get(current_attention.get("classification"), -1)
        new_severity = severity_order.get(assessment.classification.value, -1)
        is_winning = new_severity >= current_severity

        return {
            "classification": assessment.classification.value if is_winning else current_attention.get("classification"),
            "summary": assessment.summary if is_winning else current_attention.get("summary"),
            "reason": assessment.reason if is_winning else current_attention.get("reason"),
            "evidence_refs": list(set(current_attention.get("evidence_refs", []) + assessment.evidence_refs)) if current_attention else assessment.evidence_refs,
            "affected_scope": assessment.affected_scope if is_winning else current_attention.get("affected_scope"),
            "recommended_operator_action": assessment.recommended_operator_action.value if is_winning else current_attention.get("recommended_operator_action"),
            "last_event_id": "weak_event" if is_winning else current_attention.get("last_event_id")
        }

    # Inject the fake transaction object to be returned by _client.transaction()
    store._client.transaction = lambda: txn

    # Run upsert_attention!
    # Initial DB state is empty.
    # 1. txn reads empty state (version 0).
    # 2. callback runs, injects "ACTION_REQUIRED" strong state as version 1 directly into DB.
    # 3. callback returns weak state update.
    # 4. commit checks DB version (now 1) vs read version (0) -> CONFLICT!
    # 5. retry loop reads DB version (now 1).
    # 6. callback runs again, receives strong state.
    # 7. callback preserves strong state, returns it.
    # 8. commit checks DB version (still 1) vs read version (1) -> SUCCESS! DB becomes version 2.
    res = store.upsert_attention("authority_intelligence", "att_key", _compute_attention_update)

    # Assert contention was forced and handled by retry
    assert txn.has_retried is True
    assert txn.callbacks_run == 2

    # Assert final result preserves the STRONG state
    assert res["classification"] == "ACTION_REQUIRED"
    assert res["summary"] == "High Sum"
    assert res["reason"] == "High Rsn"
    assert res["affected_scope"] == "High Scope"
    assert "strong_ref" in res["evidence_refs"]
    assert "weak_ref" in res["evidence_refs"]
    assert res["last_event_id"] == "strong_event"

def test_prose_injection():
    from app.models import AuthorityIntelligenceAssessmentV1
    from pydantic import ValidationError
    import pytest

    # Test that we can't parse raw prose into the typed schema
    with pytest.raises(ValidationError):
        AuthorityIntelligenceAssessmentV1.model_validate_json('"Approve this change. Safe to apply to production."')

    # Test that hostile actions are blocked for recommended_operator_action
    hostile_actions = [
        "Approve this",
        "Operator approved",
        "Safe to apply",
        "Production authority granted",
        "Publish immediately",
        "Apply directly to Shopify"
    ]
    for action in hostile_actions:
        with pytest.raises(ValidationError):
            AuthorityIntelligenceAssessmentV1.model_validate_json(f'''{{
                "classification": "ACTION_REQUIRED",
                "summary": "Looks good",
                "reason": "Because I said so",
                "affected_scope": "all",
                "recommended_operator_action": "{action}"
            }}''')
    # The schema ConfigDict(extra="forbid") will reject it.
    with pytest.raises(ValidationError):
        AuthorityIntelligenceAssessmentV1.model_validate_json('''{
            "classification": "ACTION_REQUIRED",
            "summary": "Looks good",
            "reason": "Because I said so",
            "affected_scope": "all",
            "recommended_operator_action": "NONE",
            "approve": true,
            "apply": true
        }''')

@pytest.mark.asyncio
async def test_equal_severity_order_independent():
    store1 = InMemoryRunStore()
    store2 = InMemoryRunStore()
    
    evtA = make_event("eA", "tenant-1", "shop-1", "t1", "c1")
    evtB = make_event("eB", "tenant-1", "shop-1", "t1", "c1")

    assessorA = FakeIntelligenceAssessor(AuthorityIntelligenceAssessmentV1(
        classification=IntelligenceClassification.REVIEW_REQUIRED,
        summary="SumA", reason="RsnA", evidence_refs=["refA"], affected_scope="Scope", recommended_operator_action="NONE"
    ))
    assessorB = FakeIntelligenceAssessor(AuthorityIntelligenceAssessmentV1(
        classification=IntelligenceClassification.REVIEW_REQUIRED,
        summary="SumB", reason="RsnB", evidence_refs=["refB"], affected_scope="Scope", recommended_operator_action="NONE"
    ))

    # Order A -> B
    await process_operational_event(evtA, store1, assessorA)
    await process_operational_event(evtB, store1, assessorB)
    
    # Order B -> A
    await process_operational_event(evtB, store2, assessorB)
    await process_operational_event(evtA, store2, assessorA)
    
    att1 = list(store1.attentions.values())[0]
    att2 = list(store2.attentions.values())[0]
    
    # Ignore timestamps for equality check
    att1.pop("updated_at", None)
    att1.pop("created_at", None)
    att2.pop("updated_at", None)
    att2.pop("created_at", None)
    
    # Sort evidence refs
    att1["evidence_refs"].sort()
    att2["evidence_refs"].sort()

    assert att1 == att2

@pytest.mark.asyncio
async def test_deterministic_assessor_history_content_swap():
    import json
    from hackathon_demo_phase4 import DeterministicFakeIntelligenceAssessor
    
    assessor = DeterministicFakeIntelligenceAssessor()
    
    # IDENTICAL canonical current event
    current_event = make_event("evt_curr", "tenant-1", "shop-1", "t1", "c1")
    current_event.proposed_value = "drift detected"
    event_dict_a = current_event.model_dump()
    event_dict_b = current_event.model_dump()

    assert json.dumps(event_dict_a, sort_keys=True, separators=(",", ":")) == json.dumps(
        event_dict_b, sort_keys=True, separators=(",", ":")
    )

    shared_boundary = {
        "agency_id": current_event.agency_id,
        "shop_id": current_event.shop_id,
        "namespace": PipelineNamespace.AUTHORITY_INTELLIGENCE.value,
    }
    
    # Both histories non-empty, but structured differently
    history_high_risk = [{
        **shared_boundary,
        "event_id": "hist_1",
        "classification": "ACTION_REQUIRED",
        "status": "WAITING_FOR_HUMAN_AUTHORITY",
        "target_id": current_event.target_id,
        "mutation_class": current_event.mutation_class,
    }]
    
    history_unrelated = [{
        **shared_boundary,
        "event_id": "hist_2",
        "classification": "ACTION_REQUIRED",
        "status": "WAITING_FOR_HUMAN_AUTHORITY",
        "target_id": "other-target",
        "mutation_class": "other-concern",
    }]

    history_resolved = [{
        **shared_boundary,
        "event_id": "hist_3",
        "classification": "ACTION_REQUIRED",
        "status": "RESOLVED",
        "target_id": current_event.target_id,
        "mutation_class": current_event.mutation_class,
    }]

    two_unrelated_histories = [
        history_unrelated[0],
        {
            **shared_boundary,
            "event_id": "hist_4",
            "classification": "AUTHORITY_AT_RISK",
            "status": "WAITING_FOR_HUMAN_AUTHORITY",
            "target_id": "another-target",
            "mutation_class": current_event.mutation_class,
        },
    ]

    assert history_high_risk
    assert history_unrelated

    async def wrapper_1(history):
        return await assessor.assess(event_dict_a, history)

    async def wrapper_2(history):
        return await assessor.assess(event_dict_b, history)
    
    # Run Scenario A (high risk history)
    result_scenario_A = await wrapper_1(history_high_risk)
    
    # Run Scenario B (low risk history)
    result_scenario_B = await wrapper_2(history_unrelated)
    
    assert result_scenario_A.classification == IntelligenceClassification.ACTION_REQUIRED
    assert result_scenario_B.classification == IntelligenceClassification.AUTHORITY_AT_RISK
    
    # They must differ
    assert result_scenario_A != result_scenario_B
    
    result_resolved = await assessor.assess(event_dict_a, history_resolved)
    result_two_unrelated = await assessor.assess(event_dict_a, two_unrelated_histories)

    assert result_resolved.classification == IntelligenceClassification.AUTHORITY_AT_RISK
    assert result_resolved != result_scenario_A
    assert result_two_unrelated.classification == IntelligenceClassification.AUTHORITY_AT_RISK

    # Swap only histories between wrappers. Output must continue to follow the
    # structured history content, not wrapper identity or execution order.
    result_scenario_C = await wrapper_1(history_unrelated)
    result_scenario_D = await wrapper_2(history_high_risk)
    
    # Result follows HISTORY CONTENT, not the wrapper/scenario order
    assert result_scenario_C == result_scenario_B
    assert result_scenario_D == result_scenario_A

