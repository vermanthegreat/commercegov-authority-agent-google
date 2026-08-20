import base64
import json
from typing import Any

from pydantic import ValidationError

from app.models import ChangeEvent


class EventParseError(ValueError):
    pass


def parse_change_event(payload: Any) -> ChangeEvent:
    """Accept direct adapter JSON or the supported Pub/Sub push envelope."""
    if not isinstance(payload, dict):
        raise EventParseError("Event body must be a JSON object")
    candidate = payload
    if "message" in payload:
        message = payload.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("data"), str):
            raise EventParseError("Pub/Sub envelope must contain message.data")
        try:
            decoded = base64.b64decode(message["data"], validate=True)
            candidate = json.loads(decoded.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EventParseError("Pub/Sub message.data must be base64 JSON") from exc
    try:
        return ChangeEvent.model_validate(candidate)
    except ValidationError as exc:
        raise EventParseError("Invalid governed change event") from exc
