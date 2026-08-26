from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import RLock
from time import monotonic
from typing import Any, Callable, Protocol

from app.models import ClaimResult, ChangeEvent, TERMINAL_STATUSES, WorkflowStatus


class RunStore(Protocol):
    def get(self, namespace: str, event_id: str) -> dict[str, Any] | None: ...
    def claim_event(self, namespace: str, event: ChangeEvent, owner_id: str, lease_seconds: int = 60) -> tuple[ClaimResult, dict[str, Any]]: ...
    def begin_assessment(self, namespace: str, event_id: str, owner_id: str, attempt: int) -> dict[str, Any]: ...
    def mark_assessment_unknown(self, namespace: str, event_id: str, owner_id: str, attempt: int, reason: str) -> dict[str, Any]: ...
    def settle(self, namespace: str, event_id: str, owner_id: str, attempt: int, **fields: Any) -> dict[str, Any]: ...
    def release_claim(self, namespace: str, event_id: str, owner_id: str, attempt: int) -> dict[str, Any]: ...
    def list_events(self, namespace: str, shop_id: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]: ...
    def get_stats(self, namespace: str) -> dict[str, int]: ...
    def upsert_attention(self, attention_key: str, data: dict[str, Any]) -> dict[str, Any]: ...
    def get_attention(self, attention_key: str) -> dict[str, Any] | None: ...
    def get_history(self, attention_key: str, limit: int = 10) -> list[dict[str, Any]]: ...

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InMemoryRunStore:
    def __init__(self, *, monotonic_clock: Callable[[], float] = monotonic) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.attentions: dict[str, dict[str, Any]] = {}
        self._lock = RLock()
        self._monotonic_clock = monotonic_clock
        self._claim_clocks: dict[str, float] = {}

    def upsert_attention(self, attention_key: str, update_fn: Callable[[dict[str, Any] | None], dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            now = _now()
            existing = self.attentions.get(attention_key)
            new_data = update_fn(deepcopy(existing) if existing else None)
            if existing:
                self.attentions[attention_key].update(new_data)
                self.attentions[attention_key]["updated_at"] = now
            else:
                self.attentions[attention_key] = deepcopy(new_data)
                self.attentions[attention_key]["created_at"] = now
                self.attentions[attention_key]["updated_at"] = now
            return deepcopy(self.attentions[attention_key])

    def get_attention(self, attention_key: str) -> dict[str, Any] | None:
        with self._lock:
            item = self.attentions.get(attention_key)
            return deepcopy(item) if item else None

    def get_history(self, attention_key: str, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            matching = [
                run for run in self.runs.values() 
                if run.get("attention_key") == attention_key
            ]
            matching.sort(key=lambda r: r.get("created_at", ""), reverse=True)
            return [deepcopy(r) for r in matching[:limit]]

    def get(self, namespace: str, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self.runs.get(f"{namespace}:{event_id}")
            return deepcopy(item) if item else None

    def claim_event(self, namespace: str, event: ChangeEvent, owner_id: str, lease_seconds: int = 60) -> tuple[ClaimResult, dict[str, Any]]:
        with self._lock:
            now_str = _now()
            claim_clock = self._monotonic_clock()

            key = f"{namespace}:{event.event_id}"
            if key not in self.runs:
                result = {
                    "event_id": event.event_id,
                    "namespace": namespace,
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
                self.runs[key] = result
                self._claim_clocks[key] = claim_clock
                return ClaimResult.CLAIM_ACQUIRED, deepcopy(result)

            existing = self.runs[key]
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

            age = claim_clock - self._claim_clocks.get(key, claim_clock)
            if age < existing.get("lease_seconds", lease_seconds):
                return ClaimResult.IN_PROGRESS, deepcopy(existing)

            existing.update({
                "claim_owner_id": owner_id,
                "claimed_at": now_str,
                "lease_seconds": lease_seconds,
                "attempt": existing.get("attempt", 0) + 1,
                "updated_at": now_str,
            })
            self._claim_clocks[key] = claim_clock
            return ClaimResult.STALE_CLAIM_RECOVERED, deepcopy(existing)

    def begin_assessment(self, namespace: str, event_id: str, owner_id: str, attempt: int) -> dict[str, Any]:
        with self._lock:
            existing = self._owned_active(namespace, event_id, owner_id, attempt, WorkflowStatus.PROCESSING)
            now = _now()
            existing.update({
                "status": WorkflowStatus.ASSESSING.value,
                "assessment_admitted_at": now,
                "updated_at": now,
            })
            return deepcopy(existing)

    def mark_assessment_unknown(self, namespace: str, event_id: str, owner_id: str, attempt: int, reason: str) -> dict[str, Any]:
        return self.settle(
            namespace,
            event_id,
            owner_id,
            attempt,
            status=WorkflowStatus.ASSESSMENT_OUTCOME_UNKNOWN.value,
            reason=reason,
        )

    def release_claim(self, namespace: str, event_id: str, owner_id: str, attempt: int) -> dict[str, Any]:
        with self._lock:
            existing = self._owned_active(namespace, event_id, owner_id, attempt, WorkflowStatus.ASSESSING)
            existing.update({
                "status": WorkflowStatus.PROCESSING.value,
                "updated_at": _now(),
            })
            # Reset claim clock to allow immediate reclaim
            self._claim_clocks[f"{namespace}:{event_id}"] = 0.0
            return deepcopy(existing)

    def settle(self, namespace: str, event_id: str, owner_id: str, attempt: int, **fields: Any) -> dict[str, Any]:
        _require_terminal_status(fields)
        with self._lock:
            key = f"{namespace}:{event_id}"
            if key not in self.runs:
                raise KeyError(event_id)
            existing = self.runs[key]
            _require_owner(existing, owner_id, attempt)
            _require_shop_id_immutable(existing, fields)
            if is_terminal(existing):
                return deepcopy(existing)
            if existing.get("status") != WorkflowStatus.ASSESSING.value:
                raise RuntimeError("Settlement requires ASSESSING state")
            existing.update(deepcopy(fields) | {"updated_at": _now()})
            return deepcopy(existing)

    def list_events(self, namespace: str, shop_id: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            # Filter before pagination. Missing legacy bindings never match.
            matching_runs = (run for run in self.runs.values() if run.get("shop_id") == shop_id and run.get("namespace") == namespace)
            sorted_runs = sorted(matching_runs, key=lambda r: r.get("created_at", ""), reverse=True)
            return [deepcopy(r) for r in sorted_runs[offset:offset+limit]]

    def get_stats(self, namespace: str) -> dict[str, int]:
        with self._lock:
            runs = [r for k, r in self.runs.items() if k.startswith(f"{namespace}:")]
            total = len(runs)
            processing = sum(1 for r in runs if r.get("status") == WorkflowStatus.PROCESSING.value)
            assessing = sum(1 for r in runs if r.get("status") == WorkflowStatus.ASSESSING.value)
            terminal = sum(1 for r in runs if r.get("status") in {s.value for s in TERMINAL_STATUSES})
            proposals = sum(1 for r in runs if r.get("proposal_id"))
            return {
                "events_total": total,
                "events_processing": processing,
                "events_assessing": assessing,
                "events_terminal": terminal,
                "proposals_total": proposals,
            }

    def _owned_active(self, namespace: str, event_id: str, owner_id: str, attempt: int, status: WorkflowStatus) -> dict[str, Any]:
        key = f"{namespace}:{event_id}"
        if key not in self.runs:
            raise KeyError(event_id)
        existing = self.runs[key]
        _require_owner(existing, owner_id, attempt)
        if existing.get("status") != status.value:
            raise RuntimeError(f"Operation requires {status.value} state")
        return existing


class FirestoreRunStore:
    COLLECTION = "authority_agent_runs"
    ATTENTION_COLLECTION = "operator_attention"

    def __init__(self, project: str, database: str) -> None:
        from google.cloud import firestore
        self._client = firestore.Client(project=project, database=database)     

    def _doc(self, namespace: str, event_id: str):
        return self._client.collection(self.COLLECTION).document(f"{namespace}:{event_id}")

    def upsert_attention(self, attention_key: str, update_fn: Callable[[dict[str, Any] | None], dict[str, Any]]) -> dict[str, Any]:
        from google.cloud import firestore
        doc_ref = self._client.collection(self.ATTENTION_COLLECTION).document(attention_key)

        @firestore.transactional
        def _transactional_upsert(transaction):
            snapshot = doc_ref.get(transaction=transaction)
            existing = snapshot.to_dict() if snapshot.exists else None
            new_data = update_fn(existing)
            if snapshot.exists:
                updated = deepcopy(new_data)
                updated["updated_at"] = firestore.SERVER_TIMESTAMP
                transaction.update(doc_ref, updated)
                return snapshot.to_dict() | updated
            else:
                created = deepcopy(new_data)
                created["created_at"] = firestore.SERVER_TIMESTAMP
                created["updated_at"] = firestore.SERVER_TIMESTAMP
                transaction.create(doc_ref, created)
                return created

        return _transactional_upsert(self._client.transaction())

    def get_attention(self, attention_key: str) -> dict[str, Any] | None:
        doc_ref = self._client.collection(self.ATTENTION_COLLECTION).document(attention_key)
        snapshot = doc_ref.get()
        return snapshot.to_dict() if snapshot.exists else None

    def get_history(self, attention_key: str, limit: int = 10) -> list[dict[str, Any]]:
        from google.cloud.firestore_v1.base_query import FieldFilter
        # Requires composite index in production, but okay for mock/testing
        query = self._client.collection(self.COLLECTION).where(
            filter=FieldFilter("attention_key", "==", attention_key)
        )
        matching = (doc.to_dict() for doc in query.stream())
        sorted_matching = sorted(matching, key=lambda r: str(r.get("created_at", "")), reverse=True)
        return sorted_matching[:limit]

    def get(self, namespace: str, event_id: str) -> dict[str, Any] | None:
        snapshot = self._doc(namespace, event_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    def claim_event(self, namespace: str, event: ChangeEvent, owner_id: str, lease_seconds: int = 60) -> tuple[ClaimResult, dict[str, Any]]:
        doc_ref = self._doc(namespace, event.event_id)
        from google.cloud import firestore

        @firestore.transactional
        def _transactional_claim(transaction):
            snapshot = doc_ref.get(transaction=transaction)

            if not snapshot.exists:
                result = {
                    "event_id": event.event_id,
                    "namespace": namespace,
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

    def begin_assessment(self, namespace: str, event_id: str, owner_id: str, attempt: int) -> dict[str, Any]:
        doc_ref = self._doc(namespace, event_id)
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

    def mark_assessment_unknown(self, namespace: str, event_id: str, owner_id: str, attempt: int, reason: str) -> dict[str, Any]:
        return self.settle(
            namespace,
            event_id,
            owner_id,
            attempt,
            status=WorkflowStatus.ASSESSMENT_OUTCOME_UNKNOWN.value,
            reason=reason,
        )

    def release_claim(self, namespace: str, event_id: str, owner_id: str, attempt: int) -> dict[str, Any]:
        doc_ref = self._doc(namespace, event_id)
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

    def settle(self, namespace: str, event_id: str, owner_id: str, attempt: int, **fields: Any) -> dict[str, Any]:
        _require_terminal_status(fields)
        doc_ref = self._doc(namespace, event_id)
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

    def list_events(self, namespace: str, shop_id: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        # Firestore performs the tenant filter. Sorting and pagination operate only
        # on the already tenant-scoped result set and need no composite index.
        query = self._client.collection(self.COLLECTION).where(
            filter=FieldFilter("shop_id", "==", shop_id)
        ).where(
            filter=FieldFilter("namespace", "==", namespace)
        )
        matching_runs = (doc.to_dict() for doc in query.stream())
        sorted_runs = sorted(matching_runs, key=lambda run: str(run.get("created_at", "")), reverse=True)
        return sorted_runs[offset:offset+limit]

    def get_stats(self, namespace: str) -> dict[str, int]:
        from google.cloud.firestore_v1.base_query import FieldFilter
        # Using aggregation queries for stats where possible
        # Or retrieving all documents in a very unoptimized way for the hackathon
        # Since this is a hackathon, we might want to do it simply
        query = self._client.collection(self.COLLECTION).where(
            filter=FieldFilter("namespace", "==", namespace)
        )
        docs = [doc.to_dict() for doc in query.stream()]
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
