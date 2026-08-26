import argparse
import asyncio
import logging
import os

from app.models import ChangeEvent, AuthorityIntelligenceAssessmentV1, IntelligenceClassification
from app.services.firestore_store import InMemoryRunStore
from app.routes.operational import process_operational_event
from app.agent.intelligence_agent import IntelligenceAssessor, AdkGeminiIntelligenceAssessor
from app.config import Settings

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("demo")
logger.setLevel(logging.INFO)

class DeterministicFakeIntelligenceAssessor(IntelligenceAssessor):
    async def assess(self, event: dict, history: list) -> AuthorityIntelligenceAssessmentV1:
        logger.info(f"   [Intelligence] Assessing operational event {event['event_id']}")
        logger.info(f"   [Intelligence] Historical context size: {len(history)}")
        
        val = event["proposed_value"].lower()
        if "harmless" in val:
            return AuthorityIntelligenceAssessmentV1(
                classification=IntelligenceClassification.NO_ACTION_REQUIRED,
                summary="Routine update",
                reason="This is a known background process that requires no attention.",
                affected_scope="Product Title",
                recommended_operator_action="None"
            )
        elif "informational" in val:
            return AuthorityIntelligenceAssessmentV1(
                classification=IntelligenceClassification.INFORMATIONAL,
                summary="Relevant update",
                reason="This is an informational update about a governed proposal.",
                affected_scope="Product Title",
                recommended_operator_action="None"
            )
        elif "drift" in val:
            return AuthorityIntelligenceAssessmentV1(
                classification=IntelligenceClassification.AUTHORITY_AT_RISK,
                summary="Evidence drift detected",
                reason="Correlated event indicates the underlying production state has drifted.",
                evidence_refs=[h["event_id"] for h in history] if history else [],
                affected_scope="Product Title",
                recommended_operator_action="Review proposal validity"
            )
        elif "actionable" in val:
            return AuthorityIntelligenceAssessmentV1(
                classification=IntelligenceClassification.ACTION_REQUIRED,
                summary="Immediate action needed",
                reason="System detected a critical mismatch requiring human intervention.",
                affected_scope="Product Settings",
                recommended_operator_action="Escalate and lock"
            )
        else:
            return AuthorityIntelligenceAssessmentV1(
                classification=IntelligenceClassification.NO_ACTION_REQUIRED,
                summary="Unknown",
                reason="Default suppression",
                affected_scope="None",
                recommended_operator_action="None"
            )

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

async def print_run(name: str, event: ChangeEvent, store: InMemoryRunStore, assessor: IntelligenceAssessor):        
    print(f"\n--- SCENARIO: {name} ---")
    print(f"EVENT: {event.event_id}")
    print(f"REQUESTED CHANGE: '{event.current_value}' -> '{event.proposed_value}'")

    try:
        result = await process_operational_event(event, store, assessor)
    except Exception as e:
        print(f"Exception during processing: {e}")
        result = store.get(event.event_id) or {"status": "UNKNOWN"}

    print("\n[INTELLIGENCE BOUNDARY]")
    print(f"Status: {result.get('status')}")
    print(f"Classification: {result.get('intelligence_classification')}")      
    print(f"Reason: {result.get('reason')}")

    attention_key = result.get('attention_key')
    if attention_key:
        attention = store.get_attention(attention_key)
        print(f"\n[OPERATOR ATTENTION]")
        print(f"Key: {attention_key}")
        print(f"Level: {attention.get('classification')}")
        print(f"Summary: {attention.get('summary')}")
        print(f"Recommended Action: {attention.get('recommended_operator_action')}")
        print(f"Evidence Refs: {attention.get('evidence_refs')}")
    else:
        print(f"\n[OPERATOR ATTENTION]")
        print("NOISE SUPPRESSED - No task created")


async def run_hackathon_demo(live: bool):
    print("==================================================")
    print(f"AUTHORITY INTELLIGENCE PHASE 3 DEMO (LIVE GEMINI: {live})")
    print("==================================================")

    store = InMemoryRunStore()

    if live:
        settings = Settings(
            google_cloud_project=os.getenv("GOOGLE_CLOUD_PROJECT"),
            google_cloud_location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
            firestore_database=os.getenv("FIRESTORE_DATABASE", "(default)"),
            use_in_memory_store=True,
            commercegov_api_url=os.getenv("COMMERCEGOV_API_URL", "https://mock.commercegov.local"),
            commercegov_api_token=os.getenv("COMMERCEGOV_API_TOKEN", "mock_token"),
            taskmaster_api_token=os.getenv("TASKMASTER_API_TOKEN", "mock_token")
        )
        assessor = AdkGeminiIntelligenceAssessor(settings)
    else:
        assessor = DeterministicFakeIntelligenceAssessor()


    # 1. HARMLESS OPERATIONAL CHANGE
    evt1 = make_event("evt-101", "harmless routine update")
    await print_run("1. HARMLESS OPERATIONAL CHANGE", evt1, store, assessor)

    # 2. RELEVANT GOVERNED PROPOSAL
    evt2 = make_event("evt-102", "informational proposal")
    await print_run("2. RELEVANT GOVERNED PROPOSAL", evt2, store, assessor)

    # 3. CORRELATED AUTHORITY/EVIDENCE DRIFT
    evt3 = make_event("evt-103", "evidence drift occurred")
    await print_run("3. CORRELATED EVIDENCE DRIFT", evt3, store, assessor)

    # 4. DUPLICATE REPLAY
    print(f"\n--- SCENARIO: 4. DUPLICATE REPLAY ---")
    print("Re-submitting evt-103 with exact same fingerprint...")
    result4 = await process_operational_event(evt3, store, assessor)
    print(f"Replay Status: {result4.get('status')}")

    # 5. GENUINELY ACTIONABLE CONDITION
    evt5 = make_event("evt-105", "actionable condition")
    await print_run("5. GENUINELY ACTIONABLE CONDITION", evt5, store, assessor)

    print("\n==================================================")
    print("DEMO COMPLETE")
    print("==================================================")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 3 Demo")
    parser.add_argument("--live", action="store_true", help="Use live Gemini ADK intelligence assessor")
    args = parser.parse_args()
    asyncio.run(run_hackathon_demo(args.live))
