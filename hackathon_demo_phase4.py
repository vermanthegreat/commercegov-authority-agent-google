import argparse
import asyncio
import json
import logging

from app.models import ChangeEvent, AuthorityIntelligenceAssessmentV1, IntelligenceClassification, PipelineNamespace
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
        
        # Tenant, shop, and namespace are bounded by the pipeline before history
        # reaches the assessor. Bind the remaining semantic relationship to the
        # current target and concern before considering risk or resolution state.
        has_unresolved_high_risk = False
        has_related_resolved_or_lower_risk = False
        related_history = []
        
        for h in history:
            if (
                h.get("target_id") != event.get("target_id")
                or h.get("mutation_class") != event.get("mutation_class")
            ):
                continue

            related_history.append(h)
            c = h.get("classification")
            if c in ["ACTION_REQUIRED", "AUTHORITY_AT_RISK"] and h.get("status") != "RESOLVED":
                has_unresolved_high_risk = True
            else:
                has_related_resolved_or_lower_risk = True

        if has_unresolved_high_risk:
            return AuthorityIntelligenceAssessmentV1(
                classification=IntelligenceClassification.ACTION_REQUIRED,
                summary="Escalating due to unresolved prior high risk",
                reason="Event is actionable because history shows unresolved prior authority conflict.",
                evidence_refs=[h["event_id"] for h in related_history],
                affected_scope="Product Title",
                recommended_operator_action="REVIEW_AND_APPROVE"
            )

        if has_related_resolved_or_lower_risk and not has_unresolved_high_risk:
            if "drift" in val:
                return AuthorityIntelligenceAssessmentV1(
                    classification=IntelligenceClassification.AUTHORITY_AT_RISK,
                    summary="Evidence drift detected (prior history resolved)",
                    reason="Correlated event indicates drift, but previous concerns were resolved.",
                    evidence_refs=[h["event_id"] for h in related_history],
                    affected_scope="Product Title",
                    recommended_operator_action="INVESTIGATE_RISK"
                )

        if "harmless" in val:
            return AuthorityIntelligenceAssessmentV1(
                classification=IntelligenceClassification.NO_ACTION_REQUIRED,
                summary="Routine update",
                reason="This is a known background process that requires no attention.",
                affected_scope="Product Title",
                recommended_operator_action="NONE"
            )
        elif "informational" in val:
            return AuthorityIntelligenceAssessmentV1(
                classification=IntelligenceClassification.INFORMATIONAL,        
                summary="Relevant update",
                reason="This is an informational update about a governed proposal.",
                affected_scope="Product Title",
                recommended_operator_action="NONE"
            )
        elif "drift" in val:
            return AuthorityIntelligenceAssessmentV1(
                classification=IntelligenceClassification.AUTHORITY_AT_RISK,    
                summary="Evidence drift detected",
                reason="Correlated event indicates the underlying production state has drifted.",
                evidence_refs=[h["event_id"] for h in related_history],
                affected_scope="Product Title",
                recommended_operator_action="INVESTIGATE_RISK"
            )
        elif "actionable" in val:
            return AuthorityIntelligenceAssessmentV1(
                classification=IntelligenceClassification.ACTION_REQUIRED,      
                summary="Immediate action needed",
                reason="System detected a critical mismatch requiring human intervention.",
                affected_scope="Product Settings",
                recommended_operator_action="MITIGATE_AND_CONTINUE"
            )
        else:
            return AuthorityIntelligenceAssessmentV1(
                classification=IntelligenceClassification.NO_ACTION_REQUIRED,   
                summary="Unknown",
                reason="Default suppression",
                affected_scope="None",
                recommended_operator_action="NONE"
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
        attention = store.get_attention(PipelineNamespace.AUTHORITY_INTELLIGENCE.value, attention_key)
        print(f"\n[OPERATOR ATTENTION]")
        print(f"Key: {attention_key}")
        print(f"Level: {attention.get('classification')}")
        print(f"Summary: {attention.get('summary')}")
        print(f"Recommended Action: {attention.get('recommended_operator_action')}")
        print(f"Evidence Refs: {attention.get('evidence_refs')}")
    else:
        print(f"\n[OPERATOR ATTENTION]")
        print("NOISE SUPPRESSED - No task created")


async def print_semantic_history_comparison(assessor: IntelligenceAssessor):
    event = make_event("evt-107", "drift condition")
    event.mutation_class = "product.vendor"
    current_event_a = event.model_dump()
    current_event_b = event.model_dump()

    canonical_a = json.dumps(current_event_a, sort_keys=True, separators=(",", ":"))
    canonical_b = json.dumps(current_event_b, sort_keys=True, separators=(",", ":"))

    shared_boundary = {
        "agency_id": event.agency_id,
        "shop_id": event.shop_id,
        "namespace": PipelineNamespace.AUTHORITY_INTELLIGENCE.value,
    }
    related_unresolved_history = [{
        **shared_boundary,
        "event_id": "evt-107-related-history",
        "classification": IntelligenceClassification.ACTION_REQUIRED.value,
        "status": "WAITING_FOR_HUMAN_AUTHORITY",
        "target_id": event.target_id,
        "mutation_class": event.mutation_class,
    }]
    unrelated_history = [{
        **shared_boundary,
        "event_id": "evt-107-unrelated-history",
        "classification": IntelligenceClassification.ACTION_REQUIRED.value,
        "status": "WAITING_FOR_HUMAN_AUTHORITY",
        "target_id": "gid://shopify/Product/998877",
        "mutation_class": "product.price",
    }]

    related_assessment = await assessor.assess(current_event_a, related_unresolved_history)
    unrelated_assessment = await assessor.assess(current_event_b, unrelated_history)

    print("\n--- SCENARIO: 7. STRUCTURED HISTORY SEMANTIC CORRELATION ---")
    print(f"Canonical current events identical: {canonical_a == canonical_b}")
    print(f"Related history records: {len(related_unresolved_history)}")
    print(f"Unrelated history records: {len(unrelated_history)}")
    print("\n[RELATED UNRESOLVED HISTORY]")
    print(f"Classification: {related_assessment.classification.value}")
    print(f"Recommended Action: {related_assessment.recommended_operator_action.value}")
    print(f"Reason: {related_assessment.reason}")
    print("\n[UNRELATED NON-EMPTY HISTORY]")
    print(f"Classification: {unrelated_assessment.classification.value}")
    print(f"Recommended Action: {unrelated_assessment.recommended_operator_action.value}")
    print(f"Reason: {unrelated_assessment.reason}")
    print(f"Structured assessments differ: {related_assessment != unrelated_assessment}")


async def run_hackathon_demo(live: bool):
    print("==================================================")
    print(f"AUTHORITY INTELLIGENCE PHASE 4 DEMO (LIVE GEMINI: {live})")
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

    # 4. LATER LOWER-SEVERITY EVENT
    evt4 = make_event("evt-104", "informational proposal")
    await print_run("4. LATER LOWER-SEVERITY EVENT", evt4, store, assessor)

    # 5. DUPLICATE REPLAY
    print(f"\n--- SCENARIO: 5. DUPLICATE REPLAY ---")
    print("Re-submitting evt-103 with exact same fingerprint...")
    result5 = await process_operational_event(evt3, store, assessor)
    print(f"Replay Status: {result5.get('status')}")

    # 6. SAME TARGET, DIFFERENT CONCERN
    evt6 = make_event("evt-106", "actionable condition")
    evt6.mutation_class = "product.price"
    await print_run("6. SAME TARGET, DIFFERENT CONCERN", evt6, store, assessor) 

    # 7. SAME CURRENT EVENT WITH TWO NON-EMPTY STRUCTURED HISTORIES
    await print_semantic_history_comparison(assessor)

    print("\n==================================================")
    print("DEMO COMPLETE")
    print("==================================================")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 3 Demo")
    parser.add_argument("--live", action="store_true", help="Use live Gemini ADK intelligence assessor")
    args = parser.parse_args()
    asyncio.run(run_hackathon_demo(args.live))
