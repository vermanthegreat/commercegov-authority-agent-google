import pytest

from app.main import create_app
from app.models import (
    AuthorityAssessment, Classification, RecommendedNextAction, RiskLevel,
)
from app.services.firestore_store import InMemoryRunStore


class FakeAssessor:
    def __init__(self, result: AuthorityAssessment) -> None:
        self.result = result
        self.calls = 0

    async def assess(self, event):
        self.calls += 1
        return self.result.model_copy(update={"change_id": event.change_id})


@pytest.fixture
def event_payload():
    return {
        "event_id": "evt_001", "change_id": "chg_001", "shop_id": "demo-shop",
        "target_type": "product", "target_id": "12345", "mutation_class": "product.title",
        "current_value": "Original title", "proposed_value": "Proposed title",
        "policy_context": {"max_length": 70, "brand_tone": "professional"},
        "authority_context": {"actor_role": "operator", "requires_human_approval": True},
    }


@pytest.fixture
def human_assessment():
    return AuthorityAssessment(
        change_id="chg_001", classification=Classification.HUMAN_AUTHORITY_REQUIRED,
        risk_level=RiskLevel.medium,
        reason="Human approval is required before this governed mutation can proceed.",
        policy_observations=[],
        recommended_next_action=RecommendedNextAction.REQUEST_HUMAN_AUTHORITY,
    )


class FakeCommerceGovClient:
    def __init__(self):
        self.calls = 0
        self.last_proposal_id = None
        self.error = None

    async def submit_proposal(self, shop_id, product_id, changes, idempotency_key):
        self.calls += 1
        if self.error:
            raise self.error
        self.last_proposal_id = f"prop-{idempotency_key}"
        return self.last_proposal_id

@pytest.fixture
def app_with_fake(human_assessment):
    store = InMemoryRunStore()
    assessor = FakeAssessor(human_assessment)
    cg_client = FakeCommerceGovClient()
    return create_app(store=store, assessor=assessor, commercegov_client=cg_client), store, assessor, cg_client
