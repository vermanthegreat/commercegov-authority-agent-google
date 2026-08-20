from datetime import UTC, datetime
from typing import Any, Protocol

from app.models import TERMINAL_STATUSES, AuthorityAssessment, ChangeEvent, WorkflowStatus


class RunStore(Protocol):
    def get(self, event_id: str) -> dict[str, Any] | None: ...
    def record_received(self, event: ChangeEvent) -> dict[str, Any]: ...
    def update(self, event_id: str, **fields: Any) -> None: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


class InMemoryRunStore:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}

    def get(self, event_id: str) -> dict[str, Any] | None:
        item = self.runs.get(event_id)
        return dict(item) if item else None

    def record_received(self, event: ChangeEvent) -> dict[str, Any]:
        existing = self.get(event.event_id)
        if existing:
            return existing
        now = _now()
        result = {
            "event_id": event.event_id, "change_id": event.change_id,
            "shop_id": event.shop_id, "target_type": event.target_type,
            "target_id": event.target_id, "status": WorkflowStatus.RECEIVED.value,
            "created_at": now, "updated_at": now,
        }
        self.runs[event.event_id] = result
        return dict(result)

    def update(self, event_id: str, **fields: Any) -> None:
        if event_id not in self.runs:
            raise KeyError(event_id)
        self.runs[event_id].update(fields | {"updated_at": _now()})


class FirestoreRunStore:
    COLLECTION = "authority_agent_runs"

    def __init__(self, project: str, database: str) -> None:
        from google.cloud import firestore
        self._client = firestore.Client(project=project, database=database)

    def _doc(self, event_id: str):
        return self._client.collection(self.COLLECTION).document(event_id)

    def get(self, event_id: str) -> dict[str, Any] | None:
        snapshot = self._doc(event_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    def record_received(self, event: ChangeEvent) -> dict[str, Any]:
        doc = self._doc(event.event_id)
        existing = doc.get()
        if existing.exists:
            return existing.to_dict()
        now = _now()
        result = {
            "event_id": event.event_id, "change_id": event.change_id, "shop_id": event.shop_id,
            "target_type": event.target_type, "target_id": event.target_id,
            "status": WorkflowStatus.RECEIVED.value, "created_at": now, "updated_at": now,
        }
        doc.create(result)
        return result

    def update(self, event_id: str, **fields: Any) -> None:
        self._doc(event_id).update(fields | {"updated_at": _now()})


def is_terminal(run: dict[str, Any]) -> bool:
    return run.get("status") in {status.value for status in TERMINAL_STATUSES}
