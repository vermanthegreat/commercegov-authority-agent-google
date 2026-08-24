from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import RLock
from time import monotonic
from typing import Any, Callable, Protocol

from app.models import ClaimResult, ChangeEvent, TERMINAL_STATUSES, WorkflowStatus


class RunStore(Protocol):
    def get(self, event_id: str) -> dict[str, Any] | None: ...
    def claim_event(self, event: ChangeEvent, owner_id: str, lease_seconds: int = 60) -> tuple[ClaimResult, dict[str, Any]]: ...
    def begin_assessment(self, event_id: str, owner_id: str, attempt: int) -> dict[str, Any]: ...
    def mark_assessment_unknown(self, event_id: str, owner_id: str, attempt: int, reason: str) -> dict[str, Any]: ...
    def settle(self, event_id: str, owner_id: str, attempt: int, **fields: Any) -> dict[str, Any]: ...
    def release_claim(self, event_id: str, owner_id: str, attempt: int) -> dict[str, Any]: ...
    def list_events(self, shop_id: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]: ...
    def get_stats(self) -> dict[str, int]: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InMemoryRunStore:
    def __init__(self, *, monotonic_clock: Callable[[], float] = monotonic) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self._lock = RLock()
        self._monotonic_clock = monotonic_clock
        self._claim_clocks: dict[str, float] = {}

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self.runs.get(event_id)
            return deepcopy(item) if item else None

    def claim_event(self, event: ChangeEvent, owner_id: str, lease_seconds: int = 60) -> tuple[ClaimResult, dict[str, Any]]:
        with self._lock:
            now_str = _now()
            claim_clock = self._monotonic_clock()

            if event.event_id not in self.runs:
                result = {
                    "event_id": event.event_id,
                    "change_id": event.change_id,
                    "shop_id": event.shop_id,
                    "target_type": event.target_type,
                    "target_id": event.target_id,
                    "status": WorkflowStatus.PROCESSING.value,
                    "fingerprint": event.fingerprint,
                    "claim_owner_id": owner_id,
                    "claimed_at": now_str,
                    "lease_seconds": lease_seconds,
                    "attempt": 1,
                    "created_at": now_str,
                    "updated_at": now_str,
                }
                self.runs[event.event_id] = result
                self._claim_clocks[event.event_id] = claim_clock
                return ClaimResult.CLAIM_ACQUIRED, deepcopy(result)

            existing = self.runs[event.event_id]
            if existing.get("shop_id") != event.shop_id:
                # Missing legacy bindings and cross-shop replays both fail closed.
                return ClaimResult.EVENT_ID_CONFLICT, deepcopy(existing)
            if existing.get("fingerprint") != event.fingerprint:
                return ClaimResult.EVENT_ID_CONFLICT, deepcopy(existing)
            if is_terminal(existing):
                return ClaimResult.TERMINAL_REPLAY, deepcopy(existing)
            if existing.get("status") == WorkflowStatus.ASSESSING.value:
                return ClaimResult.IN_PROGRESS, deepcopy(existing)
            if existing.get("status") != WorkflowStatus.PROCESSING.value:
                return ClaimResult.IN_PROGRESS, deepcopy(existing)

            age = claim_clock - self._claim_clocks.get(event.event_id, claim_clock)
            if age < existing.get("lease_seconds", lease_seconds):
                return ClaimResult.IN_PROGRESS, deepcopy(existing)

            existing.update({
                "claim_owner_id": owner_id,
                "claimed_at": now_str,
                "lease_seconds": lease_seconds,
                "attempt": existing.get("attempt", 0) + 1,
                "updated_at": now_str,
            })
            self._claim_clocks[event.event_id] = claim_clock
            return ClaimResult.STALE_CLAIM_RECOVERED, deepcopy(existing)

    def begin_assessment(self, event_id: str, owner_id: str, attempt: int) -> dict[str, Any]:
        with self._lock:
            existing = self._owned_active(event_id, owner_id, attempt, WorkflowStatus.PROCESSING)
            now = _now()
            existing.update({
                "status": WorkflowStatus.ASSESSING.value,
                "assessment_admitted_at": now,
                "updated_at": now,
            })
            return deepcopy(existing)

    def mark_assessment_unknown(self, event_id: str, owner_id: str, attempt: int, reason: str) -> dict[str, Any]:
        return self.settle(
            event_id,
            owner_id,
            attempt,
            status=WorkflowStatus.ASSESSMENT_OUTCOME_UNKNOWN.value,
            reason=reason,
        )

    def release_claim(self, event_id: str, owner_id: str, attempt: int) -> dict[str, Any]:
        with self._lock:
            existing = self._owned_active(event_id, owner_id, attempt, WorkflowStatus.ASSESSING)
            existing.update({
                "status": WorkflowStatus.PROCESSING.value,
                "updated_at": _now(),
            })
            # Reset claim clock to allow immediate reclaim
            self._claim_clocks[event_id] = 0.0
            return deepcopy(existing)

    def settle(self, event_id: str, owner_id: str, attempt: int, **fields: Any) -> dict[str, Any]:
        _require_terminal_status(fields)
        with self._lock:
            if event_id not in self.runs:
                raise KeyError(event_id)
            existing = self.runs[event_id]
            _require_owner(existing, owner_id, attempt)
            _require_shop_id_immutable(existing, fields)
            if is_terminal(existing):
                return deepcopy(existing)
            if existing.get("status") != WorkflowStatus.ASSESSING.value:
                raise RuntimeError("Settlement requires ASSESSING state")
            existing.update(deepcopy(fields) | {"updated_at": _now()})
            return deepcopy(existing)

    def list_events(self, shop_id: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            # Filter before pagination. Missing legacy bindings never match.
            matching_runs = (run for run in self.runs.values() if run.get("shop_id") == shop_id)
            sorted_runs = sorted(matching_runs, key=lambda r: r.get("created_at", ""), reverse=True)
            return [deepcopy(r) for r in sorted_runs[offset:offset+limit]]

    def get_stats(self) -> dict[str, int]:
        with self._lock:
            total = len(self.runs)
            processing = sum(1 for r in self.runs.values() if r.get("status") == WorkflowStatus.PROCESSING.value)
            assessing = sum(1 for r in self.runs.values() if r.get("status") == WorkflowStatus.ASSESSING.value)
            terminal = sum(1 for r in self.runs.values() if r.get("status") in {s.value for s in TERMINAL_STATUSES})
            proposals = sum(1 for r in self.runs.values() if r.get("proposal_id"))
            return {
                "events_total": total,
                "events_processing": processing,
                "events_assessing": assessing,
                "events_terminal": terminal,
                "proposals_total": proposals,
            }

    def _owned_active(self, event_id: str, owner_id: str, attempt: int, status: WorkflowStatus) -> dict[str, Any]:
        if event_id not in self.runs:
            raise KeyError(event_id)
        existing = self.runs[event_id]
        _require_owner(existing, owner_id, attempt)
        if existing.get("status") != status.value:
            raise RuntimeError(f"Operation requires {status.value} state")
        return existing


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

    def claim_event(self, event: ChangeEvent, owner_id: str, lease_seconds: int = 60) -> tuple[ClaimResult, dict[str, Any]]:
        doc_ref = self._doc(event.event_id)
        from google.cloud import firestore

        @firestore.transactional
        def _transactional_claim(transaction):
            snapshot = doc_ref.get(transaction=transaction)

            if not snapshot.exists:
                result = {
                    "event_id": event.event_id,
                    "change_id": event.change_id,
                    "shop_id": event.shop_id,
                    "target_type": event.target_type,
                    "target_id": event.target_id,
                    "status": WorkflowStatus.PROCESSING.value,
                    "fingerprint": event.fingerprint,
                    "claim_owner_id": owner_id,
                    "claimed_at": firestore.SERVER_TIMESTAMP,
                    "lease_seconds": lease_seconds,
                    "attempt": 1,
                    "created_at": firestore.SERVER_TIMESTAMP,
                    "updated_at": firestore.SERVER_TIMESTAMP,
                }
                transaction.create(doc_ref, result)
                return ClaimResult.CLAIM_ACQUIRED, result

            existing = snapshot.to_dict()
            if existing.get("shop_id") != event.shop_id:
                # Missing legacy bindings and cross-shop replays both fail closed.
                return ClaimResult.EVENT_ID_CONFLICT, existing
            if existing.get("fingerprint") != event.fingerprint:
                return ClaimResult.EVENT_ID_CONFLICT, existing
            if is_terminal(existing):
                return ClaimResult.TERMINAL_REPLAY, existing
            if existing.get("status") == WorkflowStatus.ASSESSING.value:
                return ClaimResult.IN_PROGRESS, existing
            if existing.get("status") != WorkflowStatus.PROCESSING.value:
                return ClaimResult.IN_PROGRESS, existing

            claimed_at = existing.get("claimed_at")
            if claimed_at is None or snapshot.read_time is None:
                return ClaimResult.IN_PROGRESS, existing
            age = snapshot.read_time - claimed_at
            if age < timedelta(seconds=existing.get("lease_seconds", lease_seconds)):
                return ClaimResult.IN_PROGRESS, existing

            updated_fields = {
                "claim_owner_id": owner_id,
                "claimed_at": firestore.SERVER_TIMESTAMP,
                "lease_seconds": lease_seconds,
                "attempt": existing.get("attempt", 0) + 1,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
            transaction.update(doc_ref, updated_fields)
            existing.update(updated_fields)
            return ClaimResult.STALE_CLAIM_RECOVERED, existing

        return _transactional_claim(self._client.transaction())

    def begin_assessment(self, event_id: str, owner_id: str, attempt: int) -> dict[str, Any]:
        doc_ref = self._doc(event_id)
        from google.cloud import firestore

        @firestore.transactional
        def _transactional_begin(transaction):
            snapshot = doc_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise KeyError(event_id)
            existing = snapshot.to_dict()
            _require_owner(existing, owner_id, attempt)
            if existing.get("status") != WorkflowStatus.PROCESSING.value:
                raise RuntimeError("Assessment admission requires PROCESSING state")
            updated_fields = {
                "status": WorkflowStatus.ASSESSING.value,
                "assessment_admitted_at": firestore.SERVER_TIMESTAMP,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
            transaction.update(doc_ref, updated_fields)
            existing.update(updated_fields)
            return existing

        return _transactional_begin(self._client.transaction())

    def mark_assessment_unknown(self, event_id: str, owner_id: str, attempt: int, reason: str) -> dict[str, Any]:
        return self.settle(
            event_id,
            owner_id,
            attempt,
            status=WorkflowStatus.ASSESSMENT_OUTCOME_UNKNOWN.value,
            reason=reason,
        )

    def release_claim(self, event_id: str, owner_id: str, attempt: int) -> dict[str, Any]:
        doc_ref = self._doc(event_id)
        from google.cloud import firestore

        @firestore.transactional
        def _transactional_release(transaction):
            snapshot = doc_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise KeyError(event_id)
            existing = snapshot.to_dict()
            _require_owner(existing, owner_id, attempt)
            if existing.get("status") != WorkflowStatus.ASSESSING.value:
                raise RuntimeError("Release requires ASSESSING state")
            updated_fields = {
                "status": WorkflowStatus.PROCESSING.value,
                "claimed_at": None, # Force immediate expiration
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
            transaction.update(doc_ref, updated_fields)
            existing.update(updated_fields)
            return existing

        return _transactional_release(self._client.transaction())

    def settle(self, event_id: str, owner_id: str, attempt: int, **fields: Any) -> dict[str, Any]:
        _require_terminal_status(fields)
        doc_ref = self._doc(event_id)
        from google.cloud import firestore

        @firestore.transactional
        def _transactional_settle(transaction):
            snapshot = doc_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise KeyError(event_id)
            existing = snapshot.to_dict()
            _require_owner(existing, owner_id, attempt)
            _require_shop_id_immutable(existing, fields)
            if is_terminal(existing):
                return existing
            if existing.get("status") != WorkflowStatus.ASSESSING.value:
                raise RuntimeError("Settlement requires ASSESSING state")
            updated_fields = fields | {"updated_at": firestore.SERVER_TIMESTAMP}
            transaction.update(doc_ref, updated_fields)
            existing.update(updated_fields)
            return existing

        return _transactional_settle(self._client.transaction())

    def list_events(self, shop_id: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        # Firestore performs the tenant filter. Sorting and pagination operate only
        # on the already tenant-scoped result set and need no composite index.
        query = self._client.collection(self.COLLECTION).where(
            filter=FieldFilter("shop_id", "==", shop_id)
        )
        matching_runs = (doc.to_dict() for doc in query.stream())
        sorted_runs = sorted(matching_runs, key=lambda run: str(run.get("created_at", "")), reverse=True)
        return sorted_runs[offset:offset+limit]

    def get_stats(self) -> dict[str, int]:
        # Using aggregation queries for stats where possible
        # Or retrieving all documents in a very unoptimized way for the hackathon
        # Since this is a hackathon, we might want to do it simply
        docs = [doc.to_dict() for doc in self._client.collection(self._collection).stream()]
        total = len(docs)
        processing = sum(1 for r in docs if r.get("status") == WorkflowStatus.PROCESSING.value)
        assessing = sum(1 for r in docs if r.get("status") == WorkflowStatus.ASSESSING.value)
        terminal = sum(1 for r in docs if r.get("status") in {s.value for s in TERMINAL_STATUSES})
        proposals = sum(1 for r in docs if r.get("proposal_id"))
        return {
            "events_total": total,
            "events_processing": processing,
            "events_assessing": assessing,
            "events_terminal": terminal,
            "proposals_total": proposals,
        }


def _require_owner(run: dict[str, Any], owner_id: str, attempt: int) -> None:
    if run.get("claim_owner_id") != owner_id or run.get("attempt") != attempt:
        raise RuntimeError("Stale owner settlement blocked")


def _require_terminal_status(fields: dict[str, Any]) -> None:
    if fields.get("status") not in {status.value for status in TERMINAL_STATUSES}:
        raise ValueError("Settlement requires a terminal status")


def _require_shop_id_immutable(run: dict[str, Any], fields: dict[str, Any]) -> None:
    if "shop_id" in fields and fields["shop_id"] != run.get("shop_id"):
        raise ValueError("shop_id is immutable")


def is_terminal(run: dict[str, Any]) -> bool:
    return run.get("status") in {status.value for status in TERMINAL_STATUSES}
