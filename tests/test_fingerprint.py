from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.models import ChangeEvent, PipelineNamespace


@pytest.fixture
def event_payload():
    return {
        "event_id": "evt_001",
        "change_id": "chg_001",
        "shop_id": "demo-shop",
        "target_type": "product",
        "target_id": "12345",
        "mutation_class": "product.title",
        "current_value": "Original",
        "proposed_value": "Proposed",
        "policy_context": {"a": 1, "nested": {"x": 1, "y": 2}},
        "authority_context": {"role": "admin"},
    }


def test_fingerprint_same_payload_same_fingerprint(event_payload):
    assert ChangeEvent(**event_payload).fingerprint == ChangeEvent(**event_payload).fingerprint


def test_fingerprint_nested_dictionary_key_order_is_canonical(event_payload):
    reordered = deepcopy(event_payload)
    reordered["policy_context"] = {"nested": {"y": 2, "x": 1}, "a": 1}
    assert ChangeEvent(**event_payload).fingerprint == ChangeEvent(**reordered).fingerprint


def test_fingerprint_omitted_and_explicit_empty_context_are_equivalent(event_payload):
    omitted = deepcopy(event_payload)
    omitted.pop("policy_context")
    omitted.pop("authority_context")
    explicit = omitted | {"policy_context": {}, "authority_context": {}}
    assert ChangeEvent(**omitted).fingerprint == ChangeEvent(**explicit).fingerprint


def test_null_context_is_rejected_not_normalized_unsafely(event_payload):
    invalid = deepcopy(event_payload)
    invalid["policy_context"] = None
    with pytest.raises(ValidationError):
        ChangeEvent(**invalid)


@pytest.mark.parametrize("field,value", [
    ("proposed_value", "Different"),
    ("shop_id", "other-shop"),
    ("target_id", "other-target"),
    ("mutation_class", "product.price"),
])
def test_semantic_change_changes_fingerprint(event_payload, field, value):
    changed = deepcopy(event_payload)
    changed[field] = value
    assert ChangeEvent(**event_payload).fingerprint != ChangeEvent(**changed).fingerprint


def test_event_id_is_intentionally_excluded(event_payload):
    changed = deepcopy(event_payload)
    changed["event_id"] = "evt_002"
    assert ChangeEvent(**event_payload).fingerprint == ChangeEvent(**changed).fingerprint
