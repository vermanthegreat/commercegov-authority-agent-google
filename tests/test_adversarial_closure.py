from app.models import PipelineNamespace
import asyncio
import pytest
from app.models import WorkflowStatus, ChangeEvent, ClaimResult
from app.services.firestore_store import InMemoryRunStore
from app.routes.events import process_event
from app.agent.authority_agent import AuthorityAssessor, TransientPreAssessmentError
from app.models import AuthorityAssessment, Classification, RiskLevel, RecommendedNextAction
from fastapi import HTTPException
import pydantic

class FakeAssessor:
    def __init__(self, result=None, error=None, delay=0):
        self.result = result
        self.error = error
        self.delay = delay
        self.calls = 0
        
    async def assess(self, event):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result.model_copy(update={"change_id": event.change_id})

@pytest.fixture
def event():
    return ChangeEvent(
        event_id="evt-adv",
        change_id="chg-adv",
        shop_id="shop",
        target_type="product",
        target_id="1",
        mutation_class="product.title",
        current_value="old",
        proposed_value="new",
    )

@pytest.fixture
def assessment():
    return AuthorityAssessment(
        change_id="chg-adv",
        classification=Classification.AUTONOMOUSLY_CONTINUE,
        risk_level=RiskLevel.low,
        reason="Looks good",
        recommended_next_action=RecommendedNextAction.CONTINUE
    )

@pytest.mark.asyncio
async def test_transient_failure_recovery(event):
    store = InMemoryRunStore()
    assessor = FakeAssessor(error=TransientPreAssessmentError("transport error"))
    
    from tests.conftest import FakeCommerceGovClient
    cg_client = FakeCommerceGovClient()
    with pytest.raises(HTTPException) as exc:
        await process_event(event, store, assessor, cg_client)
    assert exc.value.status_code == 503
    
    # State should be reverted to PROCESSING and claim clock reset
    run = store.get(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event.event_id)
    assert run["status"] == WorkflowStatus.PROCESSING.value
    
    # Another worker can immediately claim it
    claim, new_run = store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, "owner-b")
    assert claim == ClaimResult.STALE_CLAIM_RECOVERED
    assert new_run["attempt"] == 2

@pytest.mark.asyncio
async def test_ambiguous_failure_is_terminal(event):
    store = InMemoryRunStore()
    assessor = FakeAssessor(error=RuntimeError("unknown adk error"))
    
    from tests.conftest import FakeCommerceGovClient
    cg_client = FakeCommerceGovClient()
    with pytest.raises(RuntimeError, match="unknown"):
        await process_event(event, store, assessor, cg_client)
        
    run = store.get(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event.event_id)
    assert run["status"] == WorkflowStatus.ASSESSMENT_OUTCOME_UNKNOWN.value
    
    # Another worker CANNOT claim it
    claim, _ = store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, "owner-b")
    assert claim == ClaimResult.TERMINAL_REPLAY

@pytest.mark.asyncio
async def test_deterministic_failure_is_terminal(event):
    store = InMemoryRunStore()
    
    assessor = FakeAssessor(error=pydantic.ValidationError.from_exception_data("title", line_errors=[]))
    
    from tests.conftest import FakeCommerceGovClient
    cg_client = FakeCommerceGovClient()
    with pytest.raises(RuntimeError, match="Deterministic"):
        await process_event(event, store, assessor, cg_client)
        
    run = store.get(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event.event_id)
    assert run["status"] == WorkflowStatus.FAILED.value
    
    # Another worker CANNOT claim it
    claim, _ = store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, "owner-b")
    assert claim == ClaimResult.TERMINAL_REPLAY

@pytest.mark.asyncio
async def test_terminal_immutability_against_stale_worker(event, assessment, monkeypatch):
    store = InMemoryRunStore()
    
    # Worker B completes the event (e.g. after worker A timed out)
    claim_b, run_b = store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, "owner-b")
    store.begin_assessment(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event.event_id, "owner-b", run_b["attempt"])
    store.settle(
            PipelineNamespace.AUTHORITY_ASSESSMENT.value,
            event.event_id, "owner-b", run_b["attempt"], 
        status=WorkflowStatus.AUTONOMOUSLY_CONTINUABLE.value,
        reason="completed by B"
    )
    
    # Worker A (stale) now wakes up and throws an exception
    assessor_a = FakeAssessor(error=RuntimeError("A failed"))
    
    # In order to simulate A's exception handler firing after B finished, 
    # we can just call process_event for A but monkeypatch the claim_event to return 
    # a fake old claim. Wait, process_event claims it itself. 
    # Let's just invoke the exception handler directly via store.mark_assessment_unknown
    with pytest.raises(RuntimeError, match="Stale owner"):
        store.mark_assessment_unknown(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event.event_id, "owner-a", 1, "A failed")
        
    run = store.get(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event.event_id)
    assert run["status"] == WorkflowStatus.AUTONOMOUSLY_CONTINUABLE.value       
    assert run["reason"] == "completed by B"


@pytest.mark.asyncio
async def test_commercegov_deterministic_proposal_failure_is_terminal(event, assessment):
    store = InMemoryRunStore()
    assessor = FakeAssessor(result=assessment)

    from tests.conftest import FakeCommerceGovClient
    from app.services.commercegov_client import CommerceGovDeterministicError

    class FailingCGClient(FakeCommerceGovClient):
        async def submit_proposal(self, proposal):
            raise CommerceGovDeterministicError("Rejected by CommerceGov schema")

    cg_client = FailingCGClient()

    with pytest.raises(RuntimeError, match="CommerceGov rejected proposal"):
        await process_event(event, store, assessor, cg_client)

    run = store.get(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event.event_id)
    assert run["status"] == WorkflowStatus.FAILED.value
    assert "Rejected by CommerceGov" in run["reason"]
    assert "proposal_id" not in run
    assert run["agency_id"] == event.agency_id

    # Terminal replay prevents duplicate assessment
    claim, _ = store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, "owner-b")
    assert claim == ClaimResult.TERMINAL_REPLAY

@pytest.mark.asyncio
async def test_commercegov_transient_proposal_failure_is_terminal(event, assessment):
    store = InMemoryRunStore()
    assessor = FakeAssessor(result=assessment)

    from tests.conftest import FakeCommerceGovClient
    from app.services.commercegov_client import CommerceGovTransientError

    class TransientFailingCGClient(FakeCommerceGovClient):
        async def submit_proposal(self, proposal):
            raise CommerceGovTransientError("Network timeout")

    cg_client = TransientFailingCGClient()

    with pytest.raises(RuntimeError, match="Failed to submit governed proposal"):
        await process_event(event, store, assessor, cg_client)

    run = store.get(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event.event_id)
    assert run["status"] == WorkflowStatus.ASSESSMENT_OUTCOME_UNKNOWN.value
    assert "Network timeout" in run["reason"]
    assert "proposal_id" not in run
    assert run["agency_id"] == event.agency_id

    # Terminal replay prevents duplicate assessment
    claim, _ = store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, "owner-b")
    assert claim == ClaimResult.TERMINAL_REPLAY

