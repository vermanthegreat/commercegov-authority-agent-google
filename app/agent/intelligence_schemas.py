from enum import Enum
from pydantic import BaseModel, ConfigDict, Field

class IntelligenceClassification(str, Enum):
    NO_ACTION_REQUIRED = "NO_ACTION_REQUIRED"
    INFORMATIONAL = "INFORMATIONAL"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    AUTHORITY_AT_RISK = "AUTHORITY_AT_RISK"
    ACTION_REQUIRED = "ACTION_REQUIRED"

class AuthorityIntelligenceAssessmentV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "v1"
    classification: IntelligenceClassification
    summary: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    affected_scope: str
    recommended_operator_action: str
