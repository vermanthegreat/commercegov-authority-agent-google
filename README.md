# CommerceGov Authority Intelligence — Google Taskmaster

An event-driven agent that autonomously assesses operational commerce events, preserves bounded workflow state, and routes authority risk to a human without gaining production authority.

## Hackathon track

**Google All Things Agentic — The Taskmaster**

## Problem

Operational events cannot be judged safely in isolation. The same proposed change may be routine or authority-sensitive depending on the tenant, governed field, prior events, and current production evidence. Giving an AI agent production credentials would turn that assessment problem into an authority risk of its own.

This project separates autonomous assessment and routing from human-controlled production authority.

## What the Taskmaster does

For each event, the service autonomously:

- validates and binds the event to its tenant and governed target;
- acquires a single-flight claim with a bounded lease;
- retrieves relevant, tenant-isolated history;
- invokes Google ADK and Vertex AI Gemini once for semantic assessment;
- applies deterministic Python schema and authority safeguards;
- persists the run and operator-attention state in Firestore; and
- returns a bounded classification and recommended operator action.

Human involvement begins only when the workflow reaches an authority boundary. That stop is an intentional governance control, not incomplete automation.

## Live production architecture

The hackathon service is deployed on **Google Cloud Run** in `us-central1` as `commercegov-authority-agent`. The current production revision is `commercegov-authority-agent-00019-wb5`, receiving 100% of traffic.

Its runtime uses project `commercegov-vertex-2026`, Google ADK, Vertex AI Gemini (`gemini-3.5-flash` in `global`), and the Firestore `(default)` database. Production sets `USE_IN_MEMORY_STORE=false`.

See [docs/architecture.md](docs/architecture.md) for the one-minute architecture view.

## Primary Taskmaster path: Authority Intelligence

`POST /events/operational` accepts a representative operational event, retrieves bounded relevant history, obtains a structured semantic assessment from Gemini, applies deterministic safeguards, and persists the result.

```text
Operational event
    → Authority Intelligence Taskmaster
    → bounded tenant history
    → Google ADK + Gemini assessment
    → deterministic authority enforcement
    → Firestore run + operator_attention
    → bounded routing result
```

For a proven governed external production mismatch—`EXTERNAL_PRODUCTION_CHANGE_DETECTED`, a governed mutation class, and unequal current and proposed values—the result cannot be classified below `AUTHORITY_AT_RISK` and therefore routes to `HUMAN_AUTHORITY_REQUIRED`. If deterministic Python has to raise a lower-severity Gemini assessment to that floor, it also sets `INVESTIGATE_RISK`. If Gemini already returns `AUTHORITY_AT_RISK` or `ACTION_REQUIRED`, its schema-bounded operator recommendation is retained.

Authority Intelligence does **not** submit a CommerceGov proposal, approve or apply a change, or write to Shopify.

## Separate proposal path: Authority Agent

`POST /runs` assesses a proposed change before it enters the governed production workflow. Its bounded outcomes are `READY_FOR_GOVERNED_EXECUTION`, `HUMAN_AUTHORITY_REQUIRED`, or `BLOCKED`.

Only when deterministic policy permits continuation may this path submit a versioned proposal to the external CommerceGov control plane. Its authority boundary is always `PROPOSE_ONLY`: it cannot approve, apply, or write directly to Shopify.

## Gemini and deterministic Python

**Gemini is the semantic reasoning component.** Through Google ADK, it receives the current event plus bounded relevant history and returns a schema-constrained assessment.

**Python enforces the safety properties.** It validates schemas and identity bindings, fingerprints evidence, rejects conflicting event replays, coordinates single-flight leases, limits history, enforces terminal routing, and prevents proven governed external mismatches from being classified below `AUTHORITY_AT_RISK`. When Python raises a lower-severity Gemini assessment to that floor, it sets `INVESTIGATE_RISK`; otherwise, Gemini's schema-bounded operator recommendation is retained.

## Google technology used

- **Google ADK** — bounded agent execution with structured output and at most one model call per assessment.
- **Vertex AI Gemini** — `gemini-3.5-flash` semantic assessment over the event and relevant history.
- **Firestore** — durable run state, transactional single-flight claims and leases, replay evidence, bounded history, and `operator_attention` state.
- **Cloud Run** — the deployed event-driven service runtime.

## Durable background workflow

The Taskmaster is event-driven: one authenticated request starts one bounded workflow. It is not a polling loop or a continuously running `while true` process.

Firestore collection `authority_agent_runs` records workflow ownership, attempts, evidence identity, assessment state, and terminal results. Collection `operator_attention` keeps the durable, severity-aware attention state for related operational events. Firestore is therefore part of workflow coordination and evidence, not merely a log sink.

## Authority and safety boundary

- No Shopify credentials are present in the Taskmaster runtime.
- No approval authority is delegated to Gemini or the Taskmaster.
- No apply operation or direct production write is available to the Taskmaster.
- Authority Intelligence stops at assessment, durable operator attention, and bounded routing.
- Authority Agent can only propose to CommerceGov when policy permits.
- CommerceGov remains the external, human-governed production authority boundary.

## Live demo

Send a **representative operational event** describing an external governed title mismatch to the authenticated production endpoint:

```http
POST /events/operational
Authorization: Bearer <redacted>
Content-Type: application/json
```

Conceptually, the event identifies the tenant and product, sets `event_type` to `EXTERNAL_PRODUCTION_CHANGE_DETECTED`, identifies `title` as the governed field, uses mutation class `product.title`, and supplies different current and proposed values.

The deployed flow has returned the bounded result:

```json
{
  "status": "HUMAN_AUTHORITY_REQUIRED",
  "intelligence_classification": "AUTHORITY_AT_RISK",
  "recommended_operator_action": "INVESTIGATE_RISK"
}
```

The complete run is persisted in `authority_agent_runs`, and the related operator-attention record is created or updated in `operator_attention`. No production token or secret is included in this repository or demo instruction.

## Reproducible Testing

The project includes two reproducible paths: an offline deterministic test path that requires no Google credentials, and a Google-backed local path using Vertex AI Gemini and Firestore.

### 1. Clone and install

```powershell
git clone https://github.com/vermanthegreat/commercegov-authority-agent-google.git
cd commercegov-authority-agent-google
git checkout hackathon/authority-intelligence

py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

### 2. Run the deterministic reproducible test set

This path exercises the authority-risk floor, history-dependent routing, replay identity, and proof that the operational route never calls CommerceGov. It requires no Google credentials or network access.

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_operational_endpoint.py::test_external_governed_mismatch_has_authority_risk_floor tests/test_operational_endpoint.py::test_event_identity_binding_replay_and_history tests/test_operational_endpoint.py::test_operational_route_never_uses_commercegov_client tests/test_intelligence.py::test_history_dependent_semantic_correlation
```

Expected result: all four selected tests pass.

### 3. Run the full test suite

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The suite covers bounded model invocation, schema validation, identity and fingerprint conflicts, replay behavior, lease ownership, tenant isolation, history-based routing, deterministic external-mismatch enforcement, and Firestore-compatible workflow behavior.

### 4. Run with Google credentials

Authenticate with Application Default Credentials, select a Google Cloud project with Vertex AI and Firestore access, configure a local bearer token, and start the event-driven service:

```powershell
gcloud auth application-default login

$env:GOOGLE_CLOUD_PROJECT="<your-gcp-project>"
$env:GOOGLE_CLOUD_LOCATION="global"
$env:GEMINI_MODEL="gemini-3.5-flash"
$env:FIRESTORE_DATABASE="(default)"
$env:USE_IN_MEMORY_STORE="false"
$env:TASKMASTER_API_TOKEN="<local-demo-token>"

.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

### 5. Reproduce the authority workflow

Send a representative event to:

```text
POST http://127.0.0.1:8080/events/operational
```

Use the bearer token configured in `TASKMASTER_API_TOKEN`. A governed external production mismatch should terminate with the bounded authority outcome:

```text
status = HUMAN_AUTHORITY_REQUIRED
intelligence_classification = AUTHORITY_AT_RISK
```

The resulting workflow state is persisted in Firestore collection `authority_agent_runs`, with related operator-attention state in `operator_attention`.

## Pre-existing system disclosure

CommerceGov is a pre-existing external production-governance system. The hackathon work in this repository implements the Google-powered Taskmaster adapter, including the Authority Agent and Authority Intelligence workflows, Google ADK + Gemini assessment, Cloud Run deployment, Firestore workflow state, tenant binding, idempotency, single-flight handling, bounded history, and authority-risk routing.

CommerceGov is not required to execute the demonstrated Authority Intelligence path. The demo uses a representative operational event sent directly to the Taskmaster endpoint; it does not claim a live Shopify webhook-to-CommerceGov-to-Taskmaster production chain.

## Repository and deployment status

- Repository: `commercegov-authority-agent-google`
- Certified application-code baseline: `63331870a3e9097e28a855eb05229424d03515ce`
- Subsequent submission changes are documentation-only; application code is unchanged.
- Google Cloud project: `commercegov-vertex-2026`
- Cloud Run service: `commercegov-authority-agent`
- Region: `us-central1`
- Production revision: `commercegov-authority-agent-00019-wb5` (100% traffic)
- Primary demo endpoint: `POST /events/operational`
