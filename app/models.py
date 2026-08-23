import hashlib
import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChangeEvent(BaseModel):
    """Hackathon adapter contract, not a CommerceGov production schema."""
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    change_id: str = Field(min_length=1)
    shop_id: str = Field(min_length=1)
    target_type: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    mutation_class: str = Field(min_length=1)
    current_value: str
    proposed_value: str
    policy_context: dict[str, Any] = Field(default_factory=dict)
    authority_context: dict[str, Any] = Field(default_factory=dict)

    @property
    def requires_human_approval(self) -> bool:
        return self.authority_context.get("requires_human_approval") is True

    @property
    def fingerprint(self) -> str:
        data = self.model_dump(exclude={"event_id"})
        canonical = json.dumps(data, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

class ClaimResult(str, Enum):
    CLAIM_ACQUIRED = "CLAIM_ACQUIRED"
    IN_PROGRESS = "IN_PROGRESS"
    TERMINAL_REPLAY = "TERMINAL_REPLAY"
    EVENT_ID_CONFLICT = "EVENT_ID_CONFLICT"
    STALE_CLAIM_RECOVERED = "STALE_CLAIM_RECOVERED"


class Classification(str, Enum):
    AUTONOMOUSLY_CONTINUE = "AUTONOMOUSLY_CONTINUE"
    HUMAN_AUTHORITY_REQUIRED = "HUMAN_AUTHORITY_REQUIRED"
    BLOCKED = "BLOCKED"


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class RecommendedNextAction(str, Enum):
    CONTINUE = "CONTINUE"
    REQUEST_HUMAN_AUTHORITY = "REQUEST_HUMAN_AUTHORITY"
    BLOCK = "BLOCK"


class AuthorityAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_id: str
    classification: Classification
    risk_level: RiskLevel
    reason: str = Field(min_length=1, max_length=2000)
    policy_observations: list[str] = Field(default_factory=list, max_length=30)
    recommended_next_action: RecommendedNextAction


class WorkflowStatus(str, Enum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    ASSESSING = "ASSESSING"
    WAITING_FOR_HUMAN_AUTHORITY = "WAITING_FOR_HUMAN_AUTHORITY"
    AUTONOMOUSLY_CONTINUABLE = "AUTONOMOUSLY_CONTINUABLE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    ASSESSMENT_OUTCOME_UNKNOWN = "ASSESSMENT_OUTCOME_UNKNOWN"


TERMINAL_STATUSES = {
    WorkflowStatus.WAITING_FOR_HUMAN_AUTHORITY,
    WorkflowStatus.AUTONOMOUSLY_CONTINUABLE,
    WorkflowStatus.BLOCKED,
    WorkflowStatus.FAILED,
    WorkflowStatus.ASSESSMENT_OUTCOME_UNKNOWN,
}
