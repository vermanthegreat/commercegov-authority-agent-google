import json
import logging
from typing import Protocol, Any

from app.config import Settings
from app.models import AuthorityIntelligenceAssessmentV1, ChangeEvent

logger = logging.getLogger("uvicorn.error")

INTELLIGENCE_AGENT_INSTRUCTION = """You analyze an operational commerce event and a history of previous relevant events.

Your goal is to determine if this event actually deserves human operator attention, and why.
Most operational events should NOT become operator tasks (NO_ACTION_REQUIRED).
Only escalate when authority is at risk or human action is strictly required based on the sequence of events.
You do NOT redefine production authority, approve, apply, or verify changes.
Your response must conform to the configured structured output schema.
"""

class IntelligenceAssessor(Protocol):
    async def assess(self, event: dict[str, Any], history: list[dict[str, Any]]) -> AuthorityIntelligenceAssessmentV1: ...


def build_intelligence_agent(model: str):
    from google.adk.agents import LlmAgent

    return LlmAgent(
        name="commercegov_intelligence_assessor",
        model=model,
        instruction=INTELLIGENCE_AGENT_INSTRUCTION,
        output_schema=AuthorityIntelligenceAssessmentV1,
    )


class AdkGeminiIntelligenceAssessor:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._agent = None

    async def assess(self, event: dict[str, Any], history: list[dict[str, Any]]) -> AuthorityIntelligenceAssessmentV1:
        from google.adk.agents.run_config import RunConfig
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types

        if self._agent is None:
            self._agent = build_intelligence_agent(self._settings.gemini_model)
            
        sessions = InMemorySessionService()
        session = await sessions.create_session(
            app_name="commercegov-intelligence-agent",
            user_id="event-processor",
        )
        runner = Runner(
            agent=self._agent,
            app_name="commercegov-intelligence-agent",
            session_service=sessions,
        )
        
        payload_dict = {
            "current_event": event,
            "historical_events": history
        }
        
        payload = json.dumps(payload_dict, separators=(",", ":"))
        final_text: str | None = None
        logger.info("intelligence_model_invocation event_id=%s", event.get("event_id", "unknown"))

        try:
            async for agent_event in runner.run_async(
                user_id="event-processor",
                session_id=session.id,
                new_message=types.Content(parts=[types.Part(text=payload)]),
                run_config=RunConfig(max_llm_calls=1),
            ):
                if agent_event.is_final_response() and agent_event.content:
                    final_text = "".join(part.text or "" for part in agent_event.content.parts)
        except Exception as exc:
            error_str = str(exc).lower()
            if "transport" in error_str or "unavailable" in error_str or "timeout" in error_str or "connection" in error_str:
                raise RuntimeError("Transient failure before outcome") from exc
            raise

        if not final_text:
            raise RuntimeError("ADK returned no final intelligence assessment")
        return AuthorityIntelligenceAssessmentV1.model_validate_json(final_text)
