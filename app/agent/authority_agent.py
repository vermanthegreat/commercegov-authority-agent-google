import json
import logging
from typing import Protocol

from app.agent.prompts import AUTHORITY_AGENT_INSTRUCTION
from app.agent.schemas import AUTHORITY_ASSESSMENT_SCHEMA
from app.config import Settings
from app.models import AuthorityAssessment, ChangeEvent

logger = logging.getLogger(__name__)


class AuthorityAssessor(Protocol):
    async def assess(self, event: ChangeEvent) -> AuthorityAssessment: ...


def build_authority_agent(model: str):
    """Create the one ADK agent used by this service.

    Imports are local so offline tests never need cloud client initialization.
    """
    from google.adk.agents import LlmAgent

    return LlmAgent(
        name="commercegov_authority_assessor",
        model=model,
        instruction=AUTHORITY_AGENT_INSTRUCTION,
        output_schema=AUTHORITY_ASSESSMENT_SCHEMA,
    )


class AdkGeminiAuthorityAssessor:
    """Runs the single ADK LlmAgent through ADK's bounded Runner API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._agent = None

    async def assess(self, event: ChangeEvent) -> AuthorityAssessment:
        # ADK uses ADC / Google Cloud configuration; no credentials are supplied here.
        from google.adk.agents.run_config import RunConfig
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types

        if self._agent is None:
            self._agent = build_authority_agent(self._settings.gemini_model)
        sessions = InMemorySessionService()
        session = await sessions.create_session(
            app_name="commercegov-authority-agent",
            user_id="event-processor",
        )
        runner = Runner(
            agent=self._agent,
            app_name="commercegov-authority-agent",
            session_service=sessions,
        )
        payload = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
        final_text: str | None = None
        logger.info("authority_model_invocation event_id=%s", event.event_id)
        async for agent_event in runner.run_async(
            user_id="event-processor",
            session_id=session.id,
            new_message=types.Content(parts=[types.Part(text=payload)]),
            run_config=RunConfig(max_llm_calls=1),
        ):
            if agent_event.is_final_response() and agent_event.content:
                final_text = "".join(part.text or "" for part in agent_event.content.parts)
        if not final_text:
            raise RuntimeError("ADK returned no final authority assessment")
        return AuthorityAssessment.model_validate_json(final_text)
