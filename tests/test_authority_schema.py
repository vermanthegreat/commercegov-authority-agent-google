import pytest
from pydantic import ValidationError

from app.models import AuthorityAssessment


def test_valid_classification_is_accepted():
    result = AuthorityAssessment.model_validate({
        "change_id": "chg_001", "classification": "HUMAN_AUTHORITY_REQUIRED",
        "risk_level": "medium", "reason": "Approval required.",
        "policy_observations": [], "recommended_next_action": "REQUEST_HUMAN_AUTHORITY",
    })
    assert result.classification == "HUMAN_AUTHORITY_REQUIRED"


def test_invalid_classification_is_rejected():
    with pytest.raises(ValidationError):
        AuthorityAssessment.model_validate({
            "change_id": "chg_001", "classification": "GRANT_PRODUCTION_AUTHORITY",
            "risk_level": "medium", "reason": "No.", "policy_observations": [],
            "recommended_next_action": "CONTINUE",
        })
