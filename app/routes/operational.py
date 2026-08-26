import logging
import uuid
from typing import Any
import pydantic

from fastapi import APIRouter, HTTPException, Request

from app.agent.intelligence_agent import IntelligenceAssessor
from app.models import ChangeEvent, IntelligenceClassification, ClaimResult, WorkflowStatus
from app.services.event_parser import EventParseError, parse_change_event
from app.services.firestore_store import RunStore

router = APIRouter()
logger = logging.getLogger("uvicorn.error")

async def process_operational_event(event: ChangeEvent, store: RunStore, assessor: IntelligenceAssessor) -> dict[str, Any]:
    owner_id = str(uuid.uuid4())
    claim, run = store.claim_event(event, owner_id)

    if claim == ClaimResult.IN_PROGRESS:
        return run
    if claim == ClaimResult.TERMINAL_REPLAY:
        logger.info("intelligence_terminal_replay event_id=%s", event.event_id)
        return run
    if claim == ClaimResult.EVENT_ID_CONFLICT:
        raise HTTPException(status_code=409, detail="Event ID conflict")

    attempt = run["attempt"]
    store.begin_assessment(event.event_id, owner_id, attempt)

    try:
        history_runs = store.get_history(event.shop_id, event.target_id, limit=5)
        # We only pass minimal history to avoid prompt bloat
        history = [{"event_id": r.get("event_id"), "classification": r.get("intelligence_classification"), "created_at": r.get("created_at")} for r in history_runs if r.get("intelligence_classification")]
        
        assessment = await assessor.assess(event.model_dump(), history)
    except pydantic.ValidationError as exc:
        try:
            store.settle(
                event.event_id, owner_id, attempt,
                status=WorkflowStatus.FAILED.value,
                reason=f"Deterministic assessor schema failure: {exc}",
            )
        except Exception:
            pass
        raise RuntimeError("Deterministic intelligence schema failure") from exc
    except Exception as exc:
        try:
            store.mark_assessment_unknown(
                event.event_id, owner_id, attempt,
                reason=f"Intelligence assessment outcome unknown: {exc}",
            )
        except Exception:
            pass
        raise RuntimeError("Intelligence assessment outcome is unknown") from exc

    # Deterministic enforcement
    if assessment.classification not in [e.value for e in IntelligenceClassification]:
        store.settle(
            event.event_id, owner_id, attempt,
            status=WorkflowStatus.FAILED.value,
            reason="Invalid classification returned"
        )
        raise RuntimeError("Invalid intelligence classification")

    attention_key = f"{event.shop_id}_{event.target_type}_{event.target_id}_{event.mutation_class}"
    
    if assessment.classification == IntelligenceClassification.NO_ACTION_REQUIRED:
        # Noise suppression
        settled = store.settle(
            event.event_id, owner_id, attempt,
            status=WorkflowStatus.AUTONOMOUSLY_CONTINUABLE.value,
            intelligence_classification=assessment.classification.value,
            reason=assessment.reason
        )
        return settled

    # For other classifications, we update operator attention
    attention_data = {
        "classification": assessment.classification.value,
        "summary": assessment.summary,
        "reason": assessment.reason,
        "evidence_refs": assessment.evidence_refs,
        "affected_scope": assessment.affected_scope,
        "recommended_operator_action": assessment.recommended_operator_action,
        "last_event_id": event.event_id
    }
    store.upsert_attention(attention_key, attention_data)

    status = WorkflowStatus.WAITING_FOR_HUMAN_AUTHORITY if assessment.classification in [
        IntelligenceClassification.ACTION_REQUIRED,
        IntelligenceClassification.AUTHORITY_AT_RISK,
        IntelligenceClassification.REVIEW_REQUIRED
    ] else WorkflowStatus.AUTONOMOUSLY_CONTINUABLE

    settled = store.settle(
        event.event_id, owner_id, attempt,
        status=status.value,
        intelligence_classification=assessment.classification.value,
        reason=assessment.reason,
        attention_key=attention_key
    )
    return settled


@router.post("/events/operational")
async def post_operational_change(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
        event = parse_change_event(payload)
    except (ValueError, EventParseError):
        raise HTTPException(status_code=400, detail="Invalid operational event")
    try:
        return await process_operational_event(event, request.app.state.run_store, request.app.state.intelligence_assessor)
    except RuntimeError:
        raise HTTPException(status_code=502, detail="Intelligence assessment failed")
