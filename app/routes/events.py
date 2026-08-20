from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.agent.authority_agent import AuthorityAssessor
from app.models import AuthorityAssessment, ChangeEvent, Classification, RecommendedNextAction, WorkflowStatus
from app.services.event_parser import EventParseError, parse_change_event
from app.services.firestore_store import RunStore, is_terminal

router = APIRouter()


def terminal_status(event: ChangeEvent, assessment: AuthorityAssessment) -> WorkflowStatus:
    # This is the deterministic authority invariant. A model recommendation never
    # confers production authority and human-required events cannot be autonomous.
    if event.requires_human_approval:
        return WorkflowStatus.WAITING_FOR_HUMAN_AUTHORITY
    if assessment.classification == Classification.HUMAN_AUTHORITY_REQUIRED:
        return WorkflowStatus.WAITING_FOR_HUMAN_AUTHORITY
    if assessment.classification == Classification.BLOCKED:
        return WorkflowStatus.BLOCKED
    return WorkflowStatus.AUTONOMOUSLY_CONTINUABLE


async def process_event(event: ChangeEvent, store: RunStore, assessor: AuthorityAssessor) -> dict[str, Any]:
    existing = store.get(event.event_id)
    if existing and is_terminal(existing):
        return existing
    store.record_received(event)
    store.update(event.event_id, status=WorkflowStatus.PROCESSING.value)
    try:
        assessment = await assessor.assess(event)  # exactly one bounded model call
        if assessment.change_id != event.change_id:
            raise ValueError("Assessment change_id does not match event")
        status = terminal_status(event, assessment)
        # Normalize a bypass attempt into the external human boundary.
        action = assessment.recommended_next_action
        classification = assessment.classification
        if event.requires_human_approval:
            classification = Classification.HUMAN_AUTHORITY_REQUIRED
            action = RecommendedNextAction.REQUEST_HUMAN_AUTHORITY
        store.update(event.event_id, status=status.value, classification=classification.value,
                     risk_level=assessment.risk_level.value, recommended_next_action=action.value,
                     reason=assessment.reason, policy_observations=assessment.policy_observations)
        return store.get(event.event_id) or {"event_id": event.event_id, "status": status.value}
    except Exception as exc:
        try:
            store.update(event.event_id, status=WorkflowStatus.FAILED.value, reason="Authority assessment failed")
        except Exception:
            pass
        raise RuntimeError("Authority assessment failed") from exc


@router.post("/events/change")
async def post_change(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
        event = parse_change_event(payload)
    except (ValueError, EventParseError):
        raise HTTPException(status_code=400, detail="Invalid governed change event")
    try:
        return await process_event(event, request.app.state.run_store, request.app.state.assessor)
    except RuntimeError:
        raise HTTPException(status_code=502, detail="Authority assessment failed")
