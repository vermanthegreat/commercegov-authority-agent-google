import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.agent.authority_agent import AuthorityAssessor
from app.models import AuthorityAssessment, ChangeEvent, Classification, ClaimResult, RecommendedNextAction, WorkflowStatus
from app.services.event_parser import EventParseError, parse_change_event
from app.services.firestore_store import RunStore

router = APIRouter()
logger = logging.getLogger("uvicorn.error")


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


from app.services.commercegov_client import CommerceGovClient, CommerceGovTransientError, CommerceGovDeterministicError

async def process_event(event: ChangeEvent, store: RunStore, assessor: AuthorityAssessor, cg_client: CommerceGovClient) -> dict[str, Any]:
    owner_id = str(uuid.uuid4())
    claim, run = store.claim_event(event, owner_id)

    if claim == ClaimResult.IN_PROGRESS:
        return run
    if claim == ClaimResult.TERMINAL_REPLAY:
        logger.info("authority_terminal_replay event_id=%s", event.event_id)
        return run
    if claim == ClaimResult.EVENT_ID_CONFLICT:
        raise HTTPException(status_code=409, detail="Event ID conflict")

    attempt = run["attempt"]
    store.begin_assessment(event.event_id, owner_id, attempt)

    from app.agent.authority_agent import TransientPreAssessmentError
    import pydantic

    try:
        assessment = await assessor.assess(event)  # exactly one bounded model call
    except TransientPreAssessmentError as exc:
        try:
            store.release_claim(event.event_id, owner_id, attempt)
        except Exception:
            pass
        raise HTTPException(status_code=503, detail="Transient authority assessment failure") from exc
    except pydantic.ValidationError as exc:
        try:
            store.settle(
                event.event_id,
                owner_id,
                attempt,
                status=WorkflowStatus.FAILED.value,
                reason=f"Deterministic assessor schema failure: {exc}",
            )
        except Exception:
            pass
        raise RuntimeError("Deterministic assessor schema failure") from exc
    except Exception as exc:
        # The ADK boundary cannot prove that transport failures occurred before
        # dispatch. Fail closed and require intervention instead of re-assessing.
        try:
            store.mark_assessment_unknown(
                event.event_id,
                owner_id,
                attempt,
                reason="Authority assessment outcome is unknown; manual intervention required",
            )
        except Exception:
            pass
        raise RuntimeError("Authority assessment outcome is unknown") from exc

    try:
        if assessment.change_id != event.change_id:
            raise ValueError("Assessment change_id does not match event")
        status = terminal_status(event, assessment)
        action = assessment.recommended_next_action
        classification = assessment.classification
        if event.requires_human_approval:
            classification = Classification.HUMAN_AUTHORITY_REQUIRED
            action = RecommendedNextAction.REQUEST_HUMAN_AUTHORITY
    except Exception as exc:
        try:
            store.settle(
                event.event_id,
                owner_id,
                attempt,
                status=WorkflowStatus.FAILED.value,
                reason="Deterministic authority enforcement failed",
            )
        except Exception:
            pass
        raise RuntimeError("Deterministic authority enforcement failed") from exc

    proposal_id = None
    if status == WorkflowStatus.AUTONOMOUSLY_CONTINUABLE and action == RecommendedNextAction.CONTINUE:
        # Taskmaster does NOT have apply or approve authority.
        # It submits a governed proposal and stops.
        try:
            # We encode the attempt into the idempotency key so that retries won't conflict 
            # if we accidentally repeat a process_event execution due to worker crash before settlement.
            # However, if we do crash before settlement, the event will remain ASSESSING and require human intervention
            # per current architecture.
            idem = f"tm-{event.event_id}-{attempt}"
            changes = {event.mutation_class.split(".")[-1]: event.proposed_value}
            
            proposal_id = await cg_client.submit_proposal(
                shop_id=event.shop_id,
                product_id=event.target_id,
                changes=changes,
                idempotency_key=idem
            )
        except CommerceGovTransientError as exc:
            # We already generated the assessment, and now we failed to submit the proposal due to transient error.
            # Because we haven't stored the assessment in a durable "waiting to submit" queue, we cannot 
            # safely retry the submission without re-running Gemini. We fail closed.
            try:
                store.mark_assessment_unknown(
                    event.event_id,
                    owner_id,
                    attempt,
                    reason=f"Failed to submit governed proposal due to transient error: {exc}"
                )
            except Exception:
                pass
            raise RuntimeError("Failed to submit governed proposal due to transient error") from exc
        except CommerceGovDeterministicError as exc:
            try:
                store.settle(
                    event.event_id,
                    owner_id,
                    attempt,
                    status=WorkflowStatus.FAILED.value,
                    reason=f"CommerceGov rejected proposal: {exc}"
                )
            except Exception:
                pass
            raise RuntimeError("CommerceGov rejected proposal") from exc
        except Exception as exc:
            try:
                store.mark_assessment_unknown(
                    event.event_id,
                    owner_id,
                    attempt,
                    reason=f"Failed to submit governed proposal due to unknown error: {exc}"
                )
            except Exception:
                pass
            raise RuntimeError("Failed to submit governed proposal due to unknown error") from exc

    # If we got a proposal_id, include it in the settled fields.
    settle_kwargs = {
        "status": status.value,
        "classification": classification.value,
        "risk_level": assessment.risk_level.value,
        "recommended_next_action": action.value,
        "reason": assessment.reason,
        "policy_observations": assessment.policy_observations,
    }
    if proposal_id:
        settle_kwargs["proposal_id"] = proposal_id

    settled = store.settle(event.event_id, owner_id, attempt, **settle_kwargs)
    return store.get(event.event_id) or settled


@router.post("/events/change")
async def post_change(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
        event = parse_change_event(payload)
    except (ValueError, EventParseError):
        raise HTTPException(status_code=400, detail="Invalid governed change event")
    try:
        return await process_event(event, request.app.state.run_store, request.app.state.assessor, request.app.state.commercegov_client)
    except RuntimeError:
        raise HTTPException(status_code=502, detail="Authority assessment failed")

@router.get("/runs")
async def list_runs(request: Request, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    # Simple operational projection endpoint for CommerceGov Agent Runs M1
    store: RunStore = request.app.state.run_store
    return store.list_events(limit=limit, offset=offset)

@router.get("/runs/stats")
async def get_stats(request: Request) -> dict[str, int]:
    # Simple operational projection endpoint for CommerceGov Agent Runs M1
    store: RunStore = request.app.state.run_store
    return store.get_stats()

@router.get("/runs/{event_id}")
async def get_run(request: Request, event_id: str) -> dict[str, Any]:
    store: RunStore = request.app.state.run_store
    run = store.get(event_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run
