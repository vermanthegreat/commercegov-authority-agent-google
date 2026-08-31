import logging
import uuid
from typing import Any
import pydantic

from fastapi import APIRouter, Depends, HTTPException, Request

from app.agent.intelligence_agent import IntelligenceAssessor
from app.models import ChangeEvent, PipelineNamespace, IntelligenceClassification, OperatorAction, ClaimResult, WorkflowStatus
from app.services.event_parser import EventParseError, parse_change_event
from app.services.firestore_store import RunStore
from app.routes.events import get_taskmaster_token

router = APIRouter()
logger = logging.getLogger("uvicorn.error")


def _is_proven_governed_external_mismatch(event: ChangeEvent) -> bool:
    """Return true only for an externally detected mismatch in its governed field."""
    governed_field = event.policy_context.get("governed_field")
    return (
        event.policy_context.get("event_type") == "EXTERNAL_PRODUCTION_CHANGE_DETECTED"
        and isinstance(governed_field, str)
        and bool(governed_field)
        and event.mutation_class == f"{event.target_type}.{governed_field}"
        and event.current_value != event.proposed_value
    )


def _apply_external_mismatch_floor(event: ChangeEvent, assessment: Any) -> Any:
    """Prevent proven governed production divergence from being classified as noise."""
    severity = {
        IntelligenceClassification.NO_ACTION_REQUIRED: 0,
        IntelligenceClassification.INFORMATIONAL: 1,
        IntelligenceClassification.REVIEW_REQUIRED: 2,
        IntelligenceClassification.AUTHORITY_AT_RISK: 3,
        IntelligenceClassification.ACTION_REQUIRED: 4,
    }
    if not _is_proven_governed_external_mismatch(event):
        return assessment
    if severity[assessment.classification] >= severity[IntelligenceClassification.AUTHORITY_AT_RISK]:
        return assessment
    return assessment.model_copy(
        update={
            "classification": IntelligenceClassification.AUTHORITY_AT_RISK,
            "reason": f"{assessment.reason} Proven governed external production mismatch requires operator attention.",
            "evidence_refs": list(dict.fromkeys([*assessment.evidence_refs, event.event_id])),
            "recommended_operator_action": OperatorAction.INVESTIGATE_RISK,
        }
    )


def operational_read_projection(run: dict[str, Any]) -> dict[str, Any]:
    """Bounded public contract for Authority Intelligence event ingestion."""
    fields = (
        "event_id", "agency_id", "shop_id", "target_type", "target_id",
        "mutation_class", "status", "intelligence_classification", "summary",
        "reason", "affected_scope", "evidence_refs", "recommended_operator_action",
        "attention_key",
    )
    return {field: run.get(field) for field in fields}

async def process_operational_event(event: ChangeEvent, store: RunStore, assessor: IntelligenceAssessor) -> dict[str, Any]:
    owner_id = str(uuid.uuid4())
    claim, run = store.claim_event(PipelineNamespace.AUTHORITY_INTELLIGENCE.value, event, owner_id)

    if claim == ClaimResult.IN_PROGRESS:
        return run
    if claim == ClaimResult.TERMINAL_REPLAY:
        logger.info("intelligence_terminal_replay event_id=%s", event.event_id)
        return run
    if claim == ClaimResult.EVENT_ID_CONFLICT:
        raise HTTPException(status_code=409, detail="Event ID conflict")

    attempt = run["attempt"]
    store.begin_assessment(PipelineNamespace.AUTHORITY_INTELLIGENCE.value, event.event_id, owner_id, attempt)

    import json; import hashlib; canonical = json.dumps({"tenant": event.agency_id, "shop": event.shop_id, "target": event.target_id, "type": event.target_type, "concern": event.mutation_class}, sort_keys=True, separators=(",",":")); attention_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    try:
        history_runs = store.get_history(PipelineNamespace.AUTHORITY_INTELLIGENCE.value, attention_key, limit=5)
    
        # Provide substantive but bounded history context
        history = [
            {
                "event_id": r.get("event_id"), 
                "classification": r.get("intelligence_classification"), 
                "summary": r.get("summary"),
                "reason": r.get("reason"),
                "affected_scope": r.get("affected_scope"),
                "target_id": r.get("target_id"),
                "mutation_class": r.get("mutation_class", r.get("concern")),
                "status": r.get("status"),
                "evidence_refs": r.get("evidence_refs"),
                "created_at": r.get("created_at")
            } 
            for r in history_runs if r.get("intelligence_classification")
        ]
        
        assessment = await assessor.assess(event.model_dump(), history)
    except pydantic.ValidationError as exc:
        try:
            store.settle(
                PipelineNamespace.AUTHORITY_INTELLIGENCE.value,
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
                PipelineNamespace.AUTHORITY_INTELLIGENCE.value,
                event.event_id, owner_id, attempt,
                reason=f"Intelligence assessment outcome unknown: {exc}",
            )
        except Exception:
            pass
        raise RuntimeError("Intelligence assessment outcome is unknown") from exc

    assessment = _apply_external_mismatch_floor(event, assessment)

    # Deterministic enforcement
    if assessment.classification not in [e.value for e in IntelligenceClassification]:
        store.settle(
            PipelineNamespace.AUTHORITY_INTELLIGENCE.value,
            event.event_id, owner_id, attempt,
            status=WorkflowStatus.FAILED.value,
            reason="Invalid classification returned"
        )
        raise RuntimeError("Invalid intelligence classification")

    import json; import hashlib; canonical = json.dumps({"tenant": event.agency_id, "shop": event.shop_id, "target": event.target_id, "type": event.target_type, "concern": event.mutation_class}, sort_keys=True, separators=(",",":")); attention_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    
    if assessment.classification == IntelligenceClassification.NO_ACTION_REQUIRED:
        # Noise suppression
        settled = store.settle(
            PipelineNamespace.AUTHORITY_INTELLIGENCE.value,
            event.event_id, owner_id, attempt,
            status=WorkflowStatus.READY_FOR_GOVERNED_EXECUTION.value,
            intelligence_classification=assessment.classification.value,
            summary=assessment.summary,
            reason=assessment.reason,
            affected_scope=assessment.affected_scope,
            evidence_refs=assessment.evidence_refs,
            recommended_operator_action=assessment.recommended_operator_action.value,
        )
        return settled

    # For other classifications, we update operator attention
    
    severity_order = {
        IntelligenceClassification.NO_ACTION_REQUIRED.value: 0,
        IntelligenceClassification.INFORMATIONAL.value: 1,
        IntelligenceClassification.REVIEW_REQUIRED.value: 2,
        IntelligenceClassification.AUTHORITY_AT_RISK.value: 3,
        IntelligenceClassification.ACTION_REQUIRED.value: 4,
    }

    def _compute_attention_update(current_attention: dict[str, Any] | None) -> dict[str, Any]:
        current_severity = -1
        current_event_id = ""
        if current_attention:
            current_severity = severity_order.get(current_attention.get("classification"), -1)
            current_event_id = current_attention.get("last_event_id", "")

        new_severity = severity_order.get(assessment.classification.value, -1)  

        is_winning = False
        if new_severity > current_severity:
            is_winning = True
        elif new_severity == current_severity:
            is_winning = event.event_id > current_event_id

        return {
            "classification": assessment.classification.value if is_winning else current_attention.get("classification"),
            "summary": assessment.summary if is_winning else current_attention.get("summary"),
            "reason": assessment.reason if is_winning else current_attention.get("reason"),
            "evidence_refs": list(set(current_attention.get("evidence_refs", []) + assessment.evidence_refs)) if current_attention else assessment.evidence_refs,
            "affected_scope": assessment.affected_scope if is_winning else current_attention.get("affected_scope"),
            "recommended_operator_action": assessment.recommended_operator_action if is_winning else current_attention.get("recommended_operator_action"),
            "last_event_id": event.event_id if is_winning else current_attention.get("last_event_id")
        }

    store.upsert_attention(PipelineNamespace.AUTHORITY_INTELLIGENCE.value, attention_key, _compute_attention_update)


    status = WorkflowStatus.HUMAN_AUTHORITY_REQUIRED if assessment.classification in [
        IntelligenceClassification.ACTION_REQUIRED,
        IntelligenceClassification.AUTHORITY_AT_RISK,
        IntelligenceClassification.REVIEW_REQUIRED
    ] else WorkflowStatus.READY_FOR_GOVERNED_EXECUTION

    settled = store.settle(
        PipelineNamespace.AUTHORITY_INTELLIGENCE.value, event.event_id, owner_id, attempt,
        status=status.value,
        intelligence_classification=assessment.classification.value,
        summary=assessment.summary,
        reason=assessment.reason,
        affected_scope=assessment.affected_scope,
        evidence_refs=assessment.evidence_refs,
        recommended_operator_action=assessment.recommended_operator_action.value,
        attention_key=attention_key
    )
    return settled


@router.post("/events/operational")
async def post_operational_change(
    request: Request,
    _token: str = Depends(get_taskmaster_token),
) -> dict[str, Any]:
    try:
        payload = await request.json()
        event = parse_change_event(payload)
    except (ValueError, EventParseError):
        raise HTTPException(status_code=400, detail="Invalid operational event")
    try:
        run = await process_operational_event(event, request.app.state.run_store, request.app.state.intelligence_assessor)
        return operational_read_projection(run)
    except RuntimeError:
        raise HTTPException(status_code=502, detail="Intelligence assessment failed")
