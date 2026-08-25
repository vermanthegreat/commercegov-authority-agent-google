# CommerceGov Authority Agent — Google Hackathon

## Problem

Governed commerce changes should proceed automatically only until a real
production-authority decision is needed. This service is the Phase 1 walking
skeleton for assessing that boundary and saving the result.

## Architecture

`event -> validate/normalize -> deterministic fingerprint -> single-flight/lease -> ADK + Gemini structured assessment -> deterministic Python authority enforcement -> evidence-bound checkpoint -> versioned CommerceGov proposal handoff -> terminal/replay-safe state`

**Important Note on Authority Boundaries:**
- Taskmaster has no Shopify access.
- Taskmaster makes no direct production mutations.
- CommerceGov review, approval, apply, and verification steps remain entirely external and decoupled.
- Automated tests use a fake assessor and in-memory store.
- Live Gemini mode is optional (via `python hackathon_demo.py --live`).
- Exactly one Gemini assessment is executed per unique event.

## What is new for the hackathon

This independent repository adds a Google ADK authority agent, Gemini structured
assessment, Pub/Sub-compatible event adapter, Firestore checkpoint abstraction,
idempotency, and Cloud Run packaging.

## What CommerceGov provides externally

CommerceGov remains the pre-existing governance control plane. Its production
review, approval, command, worker, and verification systems are not included or
changed here.

## Local setup

Requires Python 3.12.

```bash
python -m venv .venv
.venv\\Scripts\\activate  # Windows PowerShell: .venv\\Scripts\\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
```

Set `USE_IN_MEMORY_STORE=true` for a local server without Firestore. Tests
inject their own in-memory store and fake assessor.

```bash
uvicorn app.main:app --reload
```

`POST /events/change` accepts the documented direct JSON adapter contract or a
Pub/Sub push envelope whose `message.data` is base64-encoded JSON.

## Testing

```bash
pytest
```

Tests make no external network calls and consume zero Gemini tokens.

## Required Google Cloud services

For a deployed runtime: Cloud Run, Firestore (Native mode), Pub/Sub, Vertex AI
/ Gemini access, and a service account with only the required runtime access.
Enable services and bind identities deliberately; this repository does not
provision infrastructure.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `GOOGLE_CLOUD_PROJECT` | GCP project for ADC-backed Google clients |
| `GOOGLE_CLOUD_LOCATION` | Gemini/Agent Platform location (default `us-central1`) |
| `GOOGLE_GENAI_USE_VERTEXAI` | Set to `True` to select the Vertex AI Gemini backend |
| `GEMINI_MODEL` | ADK Gemini model (default `gemini-2.5-flash`) |
| `FIRESTORE_DATABASE` | Firestore database ID (default `(default)`) |
| `USE_IN_MEMORY_STORE` | Local-only persistence switch |

## Cloud Run deployment outline

Build and deploy only after choosing a project and configuring credentials:

```bash
gcloud run deploy commercegov-authority-agent \
  --source . --region YOUR_REGION --project YOUR_PROJECT \
  --no-allow-unauthenticated --min-instances 0
```

Use an attached Cloud Run runtime service account with Firestore and Vertex AI
permissions. Authentication uses Application Default Credentials; no
service-account JSON key or `GOOGLE_APPLICATION_CREDENTIALS` is required. Set
`GOOGLE_GENAI_USE_VERTEXAI=True` along with the environment variables above.
Cloud Run provides `PORT`; the image binds `0.0.0.0:$PORT`. Keep min instances
at zero, use no GPU, and retain one Gemini invocation per unique event.

## Current implementation status

Phase 1 is implemented: normalized input, one bounded assessment, deterministic
authority invariant, terminal checkpoints, and terminal replay idempotency.

Limitations:

- No CommerceGov production mutation is performed.
- No human approval callback exists yet.
- No Shopify access exists in this repository.
