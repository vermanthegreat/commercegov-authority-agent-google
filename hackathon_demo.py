import argparse
import asyncio
import json
import logging
from typing import Any

from app.models import ChangeEvent, WorkflowStatus, AuthorityAssessment, Classification, RiskLevel, RecommendedNextAction
from app.services.firestore_store import InMemoryRunStore
from app.routes.events import process_event
from app.agent.authority_agent import AuthorityAssessor, AdkGeminiAuthorityAssessor, TransientPreAssessmentError
from app.config import Settings

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("demo")
logger.setLevel(logging.INFO)

class DeterministicFakeAssessor(AuthorityAssessor):
    async def assess(self, event: ChangeEvent) -> AuthorityAssessment:
        logger.info(f"   [Assessor] Assessing event {event.event_id} (Offline Deterministic Mode)")
        logger.info(f"   [Assessor] Gathering evidence... fingerprint: {event.fingerprint}")
        
        val = event.proposed_value.lower()
        if "ambiguous" in val:
            # Simulate a failure after assessment dispatch
            raise RuntimeError("Simulated unknown ADK exception after dispatch")
        elif "blocked" in val:
            return AuthorityAssessment(
                change_id=event.change_id,
                classification=Classification.BLOCKED,
                risk_level=RiskLevel.high,
                reason="Policy violation: Restricted keyword",
                policy_observations=["Contains blocked word"],
                recommended_next_action=RecommendedNextAction.BLOCK
            )
        elif "review" in val:
            return AuthorityAssessment(
                change_id=event.change_id,
                classification=Classification.HUMAN_AUTHORITY_REQUIRED,
                risk_level=RiskLevel.medium,
                reason="Review required for material change",
                policy_observations=["Tone check required"],
                recommended_next_action=RecommendedNextAction.REQUEST_HUMAN_AUTHORITY
            )
        else:
            return AuthorityAssessment(
                change_id=event.change_id,
                classification=Classification.AUTONOMOUSLY_CONTINUE,
                risk_level=RiskLevel.low,
                reason="Change is acceptable",
                policy_observations=["Title update acceptable"],
                recommended_next_action=RecommendedNextAction.CONTINUE
            )

class DeterministicFakeCommerceGovClient:
    async def submit_proposal(self, proposal) -> str:
        logger.info(f"   [CommerceGov Handoff] Submitting governed proposal for {proposal.shop_id}/{proposal.target_id}")
        logger.info(f"   [CommerceGov Handoff] Idempotency Key: {proposal.idempotency_key}")
        logger.info(f"   [CommerceGov Handoff] Changes: {json.dumps(proposal.requested_changes)}")
        return f"prop-mock-{proposal.event_id}"

def make_event(event_id: str, proposed_value: str) -> ChangeEvent:
    return ChangeEvent(
        event_id=event_id,
        change_id=f"chg-{event_id}",
        shop_id="hackathon-store.myshopify.com",
        target_type="product",
        target_id="gid://shopify/Product/112233",
        mutation_class="product.title",
        current_value="Snowboard",
        proposed_value=proposed_value,
        policy_context={"brand_tone": "professional"},
        authority_context={"actor_role": "operator"}
    )

async def print_run(name: str, event: ChangeEvent, store: InMemoryRunStore, assessor: AuthorityAssessor, cg_client: DeterministicFakeCommerceGovClient):
    print(f"\n--- SCENARIO: {name} ---")
    print(f"EVENT: {event.event_id}")
    print(f"REQUESTED CHANGE: '{event.current_value}' -> '{event.proposed_value}'")
    print(f"FINGERPRINT: {event.fingerprint}")
    
    try:
        result = await process_event(event, store, assessor, cg_client)
    except Exception as e:
        print(f"Exception during processing: {e}")
        result = store.get(event.event_id) or {"status": "UNKNOWN"}

    print("\n[DECISION BOUNDARY]")
    print(f"Status: {result.get('status')}")
    print(f"Attempt: {result.get('attempt')}")
    if "classification" in result:
        print(f"Authority Classification: {result.get('classification')}")
        print(f"Reason: {result.get('reason')}")
    
    if "proposal_id" in result:
        print(f"\n[COMMERCEGOV HANDOFF]")
        print(f"Proposal Version: v1")
        print(f"Proposal ID: {result.get('proposal_id')}")
    
    print(f"\n[PRODUCTION EFFECT]")
    print("SHOPIFY DIRECT WRITE: NONE")

async def run_hackathon_demo(live: bool):
    print("==================================================")
    print(f"TASKMASTER HACKATHON DEMO (LIVE GEMINI: {live})")
    print("==================================================")

    store = InMemoryRunStore()
    
    if live:
        settings = Settings(
            google_cloud_project="commercegov-vertex-2026",
            google_cloud_location="us-central1",
            gemini_model="gemini-3.1-pro-preview",
            firestore_database="(default)",
            use_in_memory_store=True,
            commercegov_api_url="https://mock.commercegov.local",
            commercegov_api_token="mock_token",
            taskmaster_api_token="mock_token"
        )
        assessor = AdkGeminiAuthorityAssessor(settings)
    else:
        assessor = DeterministicFakeAssessor()
        
    cg_client = DeterministicFakeCommerceGovClient()

    # 1. SAFE CONTINUATION
    evt1 = make_event("evt-001", "Snowboard Pro Edition")
    await print_run("1. SAFE CONTINUATION", evt1, store, assessor, cg_client)

    # 2. HUMAN AUTHORITY REQUIRED
    evt2 = make_event("evt-002", "review this new title")
    await print_run("2. HUMAN AUTHORITY REQUIRED", evt2, store, assessor, cg_client)

    # 3. POLICY BLOCK
    evt3 = make_event("evt-003", "blocked title change")
    await print_run("3. POLICY BLOCK", evt3, store, assessor, cg_client)

    # 4. DUPLICATE REPLAY
    print(f"\n--- SCENARIO: 4. DUPLICATE REPLAY ---")
    print("Re-submitting evt-001 with exact same fingerprint...")
    result4 = await process_event(evt1, store, assessor, cg_client)
    print(f"Replay Status: {result4.get('status')}")

    # 5. EVIDENCE DRIFT / ID CONFLICT
    print(f"\n--- SCENARIO: 5. EVIDENCE DRIFT / ID CONFLICT ---")
    print("Re-submitting evt-001 with DIFFERENT proposed value...")
    evt5 = make_event("evt-001", "Sneaky change")
    try:
        await process_event(evt5, store, assessor, cg_client)
    except Exception as e:
        print(f"Rejected securely: {e}")

    # 6. AMBIGUOUS ASSESSOR OUTCOME
    evt6 = make_event("evt-006", "ambiguous")
    if live:
        print("\n--- SCENARIO: 6. AMBIGUOUS OUTCOME (Skipped in live mode to avoid random network drops) ---")
    else:
        await print_run("6. AMBIGUOUS ASSESSOR OUTCOME", evt6, store, assessor, cg_client)

    print("\n==================================================")
    print("DEMO COMPLETE")
    print("==================================================")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Taskmaster Demo")
    parser.add_argument("--live", action="store_true", help="Use live Gemini ADK assessor")
    args = parser.parse_args()
    asyncio.run(run_hackathon_demo(args.live))
