import base64
import json

import pytest

from app.services.event_parser import EventParseError, parse_change_event


def test_direct_json_is_accepted(event_payload):
    assert parse_change_event(event_payload).event_id == "evt_001"


def test_pubsub_push_envelope_is_accepted(event_payload):
    encoded = base64.b64encode(json.dumps(event_payload).encode()).decode()
    parsed = parse_change_event({"message": {"data": encoded}, "subscription": "projects/p/subscriptions/s"})
    assert parsed.change_id == "chg_001"


def test_malformed_payload_is_rejected():
    with pytest.raises(EventParseError):
        parse_change_event({"message": {"data": "not base64"}})
