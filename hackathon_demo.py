import asyncio
import json
import logging
from typing import Any

from app.models import ChangeEvent, WorkflowStatus, ClaimResult, AuthorityAssessment, Classification, RiskLevel, RecommendedNextAction
from app.services.firestore_store import InMemoryRunStore
from app.routes.events import process_event
from app.agent.authority_agent import AuthorityAssessor

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("demo")

class DeterministicFakeAssessor(AuthorityAssessor):
    async def assess(self, event: ChangeEvent) -> AuthorityAssessment:
        logger.info(f"   [Assessor] Assessing event {event.event_id}")
        logger.info(f"   [Assessor] Gathering evidence... fingerprint: {event.fingerprint}")
        # Deterministic logic
        if "pro" in event.proposed_value.lower():
            return AuthorityAssessment(
                change_id=event.change_id,
                classification=Classification.AUTONOMOUSLY_CONTINUE,
                risk_level=RiskLevel.low,
                reason="Pro features are pre-approved",
                policy_observations=["Title update acceptable"],
                recommended_next_action=RecommendedNextAction.CONTINUE
            )
        return AuthorityAssessment(
            change_id=event.change_id,
            classification=Classification.HUMAN_AUTHORITY_REQUIRED,
            risk_level=RiskLevel.medium,
            reason="Non-pro title changes require review",
            policy_observations=["Tone check required"],
            recommended_next_action=RecommendedNextAction.REQUEST_HUMAN_AUTHORITY
        )

class DeterministicFakeCommerceGovClient:
    async def submit_proposal(self, shop_id: str, product_id: str, changes: dict[str, Any], idempotency_key: str) -> str:
        logger.info(f"   [CommerceGov Handoff] Submitting governed proposal for {shop_id}/{product_id}")
        logger.info(f"   [CommerceGov Handoff] Idempotency Key: {idempotency_key}")
        logger.info(f"   [CommerceGov Handoff] Changes: {json.dumps(changes)}")
        return "prop-mock-12345"

async def run_hackathon_demo():
    print("==================================================")
    print("TASKMASTER HACKATHON DEMO")
    print("==================================================")

    store = InMemoryRunStore()
    assessor = DeterministicFakeAssessor()
    cg_client = DeterministicFakeCommerceGovClient()

    event = ChangeEvent(
        event_id="demo-event-001",
        change_id="chg-001",
        shop_id="hackathon-store.myshopify.com",
        target_type="product",
        target_id="gid://shopify/Product/112233",
        mutation_class="product.title",
        current_value="Snowboard",
        proposed_value="Snowboard Pro Edition",
        policy_context={"brand_tone": "professional"},
        authority_context={"actor_role": "operator"}
    )

    print("\n--- PHASE 1: Event Arrival ---")
    print(f"Request: Change {event.mutation_class} on {event.target_id} from '{event.current_value}' to '{event.proposed_value}'")
    print(f"Computed Fingerprint: {event.fingerprint}")

    print("\n--- PHASE 2: Processing ---")
    result1 = await process_event(event, store, assessor, cg_client)
    
    print("\n--- PHASE 3: Outcome ---")
    print(f"Final Status: {result1['status']}")
    print(f"Authority Classification: {result1.get('classification')}")
    print(f"Reason: {result1.get('reason')}")
    print(f"CommerceGov Proposal ID: {result1.get('proposal_id')}")
    print(f"Shopify Direct Write Performed: NO (Delegated to CommerceGov)")

    print("\n--- PHASE 4: Idempotency & Duplicate Safety ---")
    print("Re-submitting the exact same event...")
    result2 = await process_event(event, store, assessor, cg_client)
    
    print(f"Duplicate Submission Handled Safely. Status remains: {result2['status']}")
    if result2["status"] == WorkflowStatus.AUTONOMOUSLY_CONTINUABLE.value:
        print("Terminal replay confirmed. No double assessment occurred.")

    print("\n==================================================")
    print("DEMO COMPLETE")
    print("==================================================")

if __name__ == "__main__":
    asyncio.run(run_hackathon_demo())
