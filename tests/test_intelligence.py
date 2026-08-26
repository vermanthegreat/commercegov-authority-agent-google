import pytest

from app.models import (
    ChangeEvent, IntelligenceClassification, WorkflowStatus, AuthorityIntelligenceAssessmentV1
)
from app.services.firestore_store import InMemoryRunStore
from app.routes.operational import process_operational_event

class FakeIntelligenceAssessor:
    def __init__(self, result: AuthorityIntelligenceAssessmentV1) -> None:
        self.result = result
        self.calls = 0

    async def assess(self, event, history):
        self.calls += 1
        return self.result

@pytest.fixture
def intelligence_event_payload():
    return ChangeEvent(
        event_id="evt_intel_001", change_id="chg_intel_001", shop_id="demo-shop",
        target_type="product", target_id="12345", mutation_class="product.title",
        current_value="Original title", proposed_value="Proposed title",
        policy_context={}, authority_context={}
    )

@pytest.fixture
def store():
    return InMemoryRunStore()

@pytest.mark.asyncio
async def test_no_action_required_creates_no_task(intelligence_event_payload, store):
    assessment = AuthorityIntelligenceAssessmentV1(
        classification=IntelligenceClassification.NO_ACTION_REQUIRED,
        summary="Noise", reason="Background sync", evidence_refs=[],
        affected_scope="None", recommended_operator_action="None"
    )
    assessor = FakeIntelligenceAssessor(assessment)
    
    run = await process_operational_event(intelligence_event_payload, store, assessor)
    
    assert run["status"] == WorkflowStatus.AUTONOMOUSLY_CONTINUABLE.value
    assert run["intelligence_classification"] == "NO_ACTION_REQUIRED"
    assert "attention_key" not in run

@pytest.mark.asyncio
async def test_informational_creates_attention_but_not_waiting_status(intelligence_event_payload, store):
    assessment = AuthorityIntelligenceAssessmentV1(
        classification=IntelligenceClassification.INFORMATIONAL,
        summary="Info", reason="Something happened", evidence_refs=[],
        affected_scope="Product", recommended_operator_action="None"
    )
    assessor = FakeIntelligenceAssessor(assessment)
    
    run = await process_operational_event(intelligence_event_payload, store, assessor)
    
    assert run["status"] == WorkflowStatus.AUTONOMOUSLY_CONTINUABLE.value
    assert run["intelligence_classification"] == "INFORMATIONAL"
    
    attention_key = run["attention_key"]
    attention = store.get_attention(attention_key)
    assert attention is not None
    assert attention["classification"] == "INFORMATIONAL"

@pytest.mark.asyncio
async def test_action_required_creates_attention_and_blocks(intelligence_event_payload, store):
    assessment = AuthorityIntelligenceAssessmentV1(
        classification=IntelligenceClassification.ACTION_REQUIRED,
        summary="Urgent", reason="Authority conflict", evidence_refs=[],
        affected_scope="Product", recommended_operator_action="Fix it"
    )
    assessor = FakeIntelligenceAssessor(assessment)
    
    run = await process_operational_event(intelligence_event_payload, store, assessor)
    
    assert run["status"] == WorkflowStatus.WAITING_FOR_HUMAN_AUTHORITY.value
    assert run["intelligence_classification"] == "ACTION_REQUIRED"
    
    attention_key = run["attention_key"]
    attention = store.get_attention(attention_key)
    assert attention is not None
    assert attention["classification"] == "ACTION_REQUIRED"
    assert attention["summary"] == "Urgent"

@pytest.mark.asyncio
async def test_correlated_event_updates_existing_attention(intelligence_event_payload, store):
    # First event
    assessment1 = AuthorityIntelligenceAssessmentV1(
        classification=IntelligenceClassification.INFORMATIONAL,
        summary="Info 1", reason="Reason 1", evidence_refs=[],
        affected_scope="Product", recommended_operator_action="None"
    )
    assessor1 = FakeIntelligenceAssessor(assessment1)
    run1 = await process_operational_event(intelligence_event_payload, store, assessor1)
    
    attention_key = run1["attention_key"]
    attention1 = store.get_attention(attention_key)
    
    # Correlated second event (same shop and target)
    evt2 = intelligence_event_payload.model_copy(update={"event_id": "evt_intel_002", "proposed_value": "New title 2"})
    
    assessment2 = AuthorityIntelligenceAssessmentV1(
        classification=IntelligenceClassification.AUTHORITY_AT_RISK,
        summary="Escalation", reason="Reason 2", evidence_refs=["evt_intel_001"],
        affected_scope="Product", recommended_operator_action="Review"
    )
    assessor2 = FakeIntelligenceAssessor(assessment2)
    run2 = await process_operational_event(evt2, store, assessor2)
    
    assert run2["status"] == WorkflowStatus.WAITING_FOR_HUMAN_AUTHORITY.value
    assert run2["attention_key"] == attention_key
    
    attention2 = store.get_attention(attention_key)
    assert attention2["classification"] == "AUTHORITY_AT_RISK"
    assert attention2["summary"] == "Escalation"
    # Verify it updated the same object essentially (it overwrites)
    assert attention2["updated_at"] > attention1["updated_at"] or attention2["updated_at"] == attention1["updated_at"]
    assert len(store.attentions) == 1

@pytest.mark.asyncio
async def test_duplicate_replay_does_not_duplicate_attention(intelligence_event_payload, store):
    assessment = AuthorityIntelligenceAssessmentV1(
        classification=IntelligenceClassification.ACTION_REQUIRED,
        summary="Urgent", reason="Authority conflict", evidence_refs=[],
        affected_scope="Product", recommended_operator_action="Fix it"
    )
    assessor = FakeIntelligenceAssessor(assessment)
    
    run1 = await process_operational_event(intelligence_event_payload, store, assessor)
    assert assessor.calls == 1
    
    # Replay same event
    run2 = await process_operational_event(intelligence_event_payload, store, assessor)
    assert assessor.calls == 1 # Assessor should not be called again
    
    assert run2["status"] == run1["status"]
    assert len(store.attentions) == 1

@pytest.mark.asyncio
async def test_malformed_gemini_assessment_fails_closed(intelligence_event_payload, store):
    class BadAssessor:
        async def assess(self, event, history):
            raise ValueError("Malformed output")
            
    assessor = BadAssessor()
    
    with pytest.raises(RuntimeError, match="outcome is unknown"):
        await process_operational_event(intelligence_event_payload, store, assessor)
    
    run = store.get(intelligence_event_payload.event_id)
    assert run["status"] == WorkflowStatus.ASSESSMENT_OUTCOME_UNKNOWN.value

@pytest.mark.asyncio
async def test_cross_tenant_isolation(store):
    evt1 = ChangeEvent(
        event_id="evt_1", change_id="chg_1", shop_id="tenant-A",
        target_type="product", target_id="123", mutation_class="title",
        current_value="A", proposed_value="B", policy_context={}, authority_context={}
    )
    evt2 = ChangeEvent(
        event_id="evt_2", change_id="chg_2", shop_id="tenant-B",
        target_type="product", target_id="123", mutation_class="title",
        current_value="A", proposed_value="B", policy_context={}, authority_context={}
    )
    
    assessment = AuthorityIntelligenceAssessmentV1(
        classification=IntelligenceClassification.ACTION_REQUIRED,
        summary="Urgent", reason="Conflict", evidence_refs=[],
        affected_scope="Product", recommended_operator_action="Fix"
    )
    assessor = FakeIntelligenceAssessor(assessment)
    
    run1 = await process_operational_event(evt1, store, assessor)
    run2 = await process_operational_event(evt2, store, assessor)
    
    # Each tenant should get their own attention key
    assert run1["attention_key"] != run2["attention_key"]
    assert len(store.attentions) == 2
