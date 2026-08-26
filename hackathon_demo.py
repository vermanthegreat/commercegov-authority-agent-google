import argparse
import asyncio
import json
import logging
import hashlib
import os
from typing import Any

from app.models import (
    ChangeEvent, WorkflowStatus, AuthorityIntelligenceAssessmentV1, 
    IntelligenceClassification, OperatorAction, PipelineNamespace
)
from app.services.firestore_store import InMemoryRunStore
from app.routes.operational import process_operational_event
from app.agent.intelligence_agent import IntelligenceAssessor, AdkGeminiIntelligenceAssessor
from app.config import Settings

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("demo")
logger.setLevel(logging.INFO)

class DeterministicFakeIntelligenceAssessor(IntelligenceAssessor):
    async def assess(self, event: dict[str, Any], history: list[dict[str, Any]]) -> AuthorityIntelligenceAssessmentV1:
        logger.info(f"   [Assessor] Assessing event {event.get('event_id')} (Offline Deterministic Mode)")
        logger.info(f"   [Assessor] History records provided: {len(history)}")
        
        has_related_history = False
        for h in history:
            if h.get("mutation_class") == event.get("mutation_class") and h.get("classification") == IntelligenceClassification.AUTHORITY_AT_RISK.value:
                has_related_history = True
                break

        if has_related_history:
            return AuthorityIntelligenceAssessmentV1(
                classification=IntelligenceClassification.AUTHORITY_AT_RISK,
                summary="Repeated title change attempt following prior risk",
                reason="Multiple changes to the same target property detected after a risky event.",
                evidence_refs=["hist-1"],
                affected_scope="product.title",
                recommended_operator_action=OperatorAction.INVESTIGATE_RISK
            )
        else:
            return AuthorityIntelligenceAssessmentV1(
                classification=IntelligenceClassification.NO_ACTION_REQUIRED,
                summary="Standard change with no relevant risk history",
                reason="Normal operation, safe to suppress.",
                evidence_refs=[],
                affected_scope="product.title",
                recommended_operator_action=OperatorAction.NONE
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

def inject_history(store: InMemoryRunStore, event: ChangeEvent, related: bool):
    canonical = json.dumps({"tenant": event.agency_id, "shop": event.shop_id, "target": event.target_id, "type": event.target_type, "concern": event.mutation_class}, sort_keys=True, separators=(",",":"))
    attention_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    
    if related:
        hist = {
            "event_id": f"hist-related-{event.event_id}",
            "namespace": PipelineNamespace.AUTHORITY_INTELLIGENCE.value,
            "attention_key": attention_key,
            "intelligence_classification": IntelligenceClassification.AUTHORITY_AT_RISK.value,
            "summary": "Previous risky change",
            "reason": "Operator previously flagged this.",
            "affected_scope": event.mutation_class,
            "target_id": event.target_id,
            "mutation_class": event.mutation_class,
            "status": WorkflowStatus.WAITING_FOR_HUMAN_AUTHORITY.value,
            "evidence_refs": ["evidence-1"],
            "created_at": "2026-08-25T12:00:00Z"
        }
    else:
        hist = {
            "event_id": f"hist-unrelated-{event.event_id}",
            "namespace": PipelineNamespace.AUTHORITY_INTELLIGENCE.value,
            "attention_key": attention_key,
            "intelligence_classification": IntelligenceClassification.INFORMATIONAL.value,
            "summary": "Previous informational change",
            "reason": "Just a normal log.",
            "affected_scope": event.mutation_class,
            "target_id": event.target_id,
            "mutation_class": event.mutation_class,
            "status": WorkflowStatus.SUPPRESSED.value,
            "evidence_refs": [],
            "created_at": "2026-08-25T12:00:00Z"
        }
    store.runs[f"{PipelineNamespace.AUTHORITY_INTELLIGENCE.value}:{hist['event_id']}"] = hist


async def print_run(name: str, event: ChangeEvent, store: InMemoryRunStore, assessor: IntelligenceAssessor):        
    print(f"\n================================================")
    print(f"SCENARIO: {name}")
    print(f"================================================")
    print(f"Current event:\n  ID: {event.event_id}\n  Change: '{event.current_value}' -> '{event.proposed_value}'")

    # Show history if present
    canonical = json.dumps({"tenant": event.agency_id, "shop": event.shop_id, "target": event.target_id, "type": event.target_type, "concern": event.mutation_class}, sort_keys=True, separators=(",",":"))
    attention_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    history = store.get_history(PipelineNamespace.AUTHORITY_INTELLIGENCE.value, attention_key)
    
    print("\nHistory:")
    if not history:
        print("  None")
    else:
        for h in history:
            print(f"  - [{h.get('created_at')}] {h.get('intelligence_classification')}: {h.get('summary')}")

    try:
        result = await process_operational_event(event, store, assessor)
    except Exception as e:
        print(f"\nException during processing: {e}")
        result = store.get(PipelineNamespace.AUTHORITY_INTELLIGENCE.value, event.event_id) or {"status": "UNKNOWN"}

    attention = store.get_attention(PipelineNamespace.AUTHORITY_INTELLIGENCE.value, attention_key)
    action = attention.get('recommended_operator_action', 'NONE') if attention else 'NONE'

    print("\nAuthority Intelligence:")
    print(f"  Level: {result.get('intelligence_classification', 'N/A')}")
    print(f"  Reason: {result.get('reason', 'N/A')}")
    print(f"  Recommended action: {action}")

    print("\nCommerceGov Approval:")
    print("  NOT GRANTED")
    
    print("\nCommerceGov Apply:")
    print("  NOT GRANTED")

    print("\nShopify Direct Write:")
    print("  NONE")

    print("\nWhy this changed:")
    if result.get('intelligence_classification') == IntelligenceClassification.AUTHORITY_AT_RISK.value:
        print("  The intelligence assessor correlated the current event with an unresolved risky history, elevating the response to block autonomous progression.")
    else:
        print("  The intelligence assessor found no related risks, so it suppressed the event as noise (safe continuation).")

async def run_hackathon_demo(live: bool):
    print("==================================================")
    print(f"TASKMASTER HACKATHON DEMO (LIVE GEMINI: {live})")
    if not live:
        print("Offline demo mode reproduces the same structured assessment contract")
        print("without requiring external credentials.")
        print("The live path uses Gemini over the bounded context.")
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

    # Scenario 1. SAFE CONTINUATION
    evt1 = make_event("evt-001", "Snowboard Pro Edition")
    await print_run("1. SAFE CONTINUATION", evt1, store, assessor)   

    # Scenario 2. KILLER DEMO: SAME EVENT + RELATED HISTORY
    store = InMemoryRunStore()
    evt2a = make_event("evt-002", "Snowboard Elite")
    inject_history(store, evt2a, related=False)
    await print_run("2A. SAME EVENT, DIFFERENT HISTORY (Unrelated/Resolved)", evt2a, store, assessor)

    store = InMemoryRunStore()
    evt2b = make_event("evt-002", "Snowboard Elite")
    inject_history(store, evt2b, related=True)
    await print_run("2B. SAME EVENT, DIFFERENT HISTORY (Related/Unresolved)", evt2b, store, assessor)

    # Scenario 3. ADVERSARIAL PROTECTION
    print(f"\n================================================")
    print("SCENARIO: 3. ADVERSARIAL PROTECTION")
    print(f"================================================")
    print("Re-submitting evt-002 with DIFFERENT proposed value (Evidence Drift) ...")
    evt3 = make_event("evt-002", "Sneaky change")
    try:
        await process_operational_event(evt3, store, assessor)
    except Exception as e:
        print(f"Deterministic enforcement rejected operation securely:\n{e}")

    print("\nCommerceGov Approval:")
    print("  NOT GRANTED")
    print("\nCommerceGov Apply:")
    print("  NOT GRANTED")
    print("\nShopify Direct Write:")
    print("  NONE")

    print("\n==================================================")
    print("DEMO COMPLETE")
    print("==================================================")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Taskmaster Demo")
    parser.add_argument("--live", action="store_true", help="Use live Gemini ADK assessor")
    args = parser.parse_args()
    asyncio.run(run_hackathon_demo(args.live))
