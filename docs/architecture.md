# Architecture

Phase 1 is a small event-to-checkpoint service. It accepts the hackathon
adapter contract directly or inside a Pub/Sub push envelope, validates it,
calls one bounded ADK/Gemini assessment, validates the result in Python, and
persists a workflow checkpoint in `authority_agent_runs`.

```text
Pub/Sub push or local JSON
        │
        ▼
FastAPI event adapter ──► Firestore checkpoint
        │                         │
        ▼                         ▼
Gemini / Authority Agent     deterministic idempotency
        │
        │ assessment only
        ▼
Human Authority Boundary
        │
        │ future explicit CommerceGov decision
        ▼
CommerceGov control plane
        │
        ▼
CommerceGov governed execution
```

**The agent does not own production authority.** Gemini recommends an
assessment only. The application always maps an event with
`requires_human_approval: true` to `WAITING_FOR_HUMAN_AUTHORITY`, even if a
model returns an autonomous classification. Phase 1 has no execution code.

For one `event_id`, a terminal persisted checkpoint is returned on replay
without a second model call. The Firestore document ID is the event ID. The
store and assessor are dependency-injected; tests use both in-memory and fake
implementations and make no network calls.
