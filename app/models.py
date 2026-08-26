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
    agency_id: str = Field(default="default-agency", min_length=1)
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
        if data.get("agency_id") == "default-agency":
            del data["agency_id"]
        canonical = json.dumps(data, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

class ClaimResult(str, Enum):
    CLAIM_ACQUIRED = "CLAIM_ACQUIRED"
    IN_PROGRESS = "IN_PROGRESS"
    TERMINAL_REPLAY = "TERMINAL_REPLAY"
    EVENT_ID_CONFLICT = "EVENT_ID_CONFLICT"
    STALE_CLAIM_RECOVERED = "STALE_CLAIM_RECOVERED"


class Classification(str, Enum):
    READY_FOR_GOVERNED_EXECUTION = "READY_FOR_GOVERNED_EXECUTION"
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


class CommerceGovProposalV1(BaseModel):
    schema_version: str = "v1"
    source: str = "taskmaster_authority_agent"
    event_id: str
    event_fingerprint: str
    attempt: int
    shop_id: str
    target_type: str
    target_id: str
    requested_changes: dict[str, Any]
    authority_classification: str
    idempotency_key: str


class AuthorityAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_id: str
    classification: Classification
    risk_level: RiskLevel
    reason: str = Field(min_length=1, max_length=2000)
    policy_observations: list[str] = Field(default_factory=list, max_length=30)
    recommended_next_action: RecommendedNextAction


class IntelligenceClassification(str, Enum):
    NO_ACTION_REQUIRED = "NO_ACTION_REQUIRED"
    INFORMATIONAL = "INFORMATIONAL"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    AUTHORITY_AT_RISK = "AUTHORITY_AT_RISK"
    ACTION_REQUIRED = "ACTION_REQUIRED"


class OperatorAction(str, Enum):
    NONE = "NONE"
    REVIEW_AND_APPROVE = "REVIEW_AND_APPROVE"
    INVESTIGATE_RISK = "INVESTIGATE_RISK"
    REJECT = "REJECT"
    MITIGATE_AND_CONTINUE = "MITIGATE_AND_CONTINUE"


class AuthorityIntelligenceAssessmentV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "v1"
    classification: IntelligenceClassification
    summary: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    affected_scope: str
    recommended_operator_action: OperatorAction


class PipelineNamespace(str, Enum):
    AUTHORITY_ASSESSMENT = "authority_assessment"
    AUTHORITY_INTELLIGENCE = "authority_intelligence"


class WorkflowStatus(str, Enum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    ASSESSING = "ASSESSING"
    HUMAN_AUTHORITY_REQUIRED = "HUMAN_AUTHORITY_REQUIRED"
    READY_FOR_GOVERNED_EXECUTION = "READY_FOR_GOVERNED_EXECUTION"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    ASSESSMENT_OUTCOME_UNKNOWN = "ASSESSMENT_OUTCOME_UNKNOWN"

TERMINAL_STATUSES = {
    WorkflowStatus.HUMAN_AUTHORITY_REQUIRED,
    WorkflowStatus.READY_FOR_GOVERNED_EXECUTION,
    WorkflowStatus.BLOCKED,
    WorkflowStatus.FAILED,
    WorkflowStatus.ASSESSMENT_OUTCOME_UNKNOWN,

}

