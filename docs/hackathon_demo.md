# Taskmaster Hackathon Demo Guide

## Problem Statement
Delegating AI authority over a merchant's production systems is risky. An autonomous agent needs a safe sandbox to assess a proposed change based on context and policy, without ever obtaining the credentials or ability to write directly to production.

## Architecture & Trust Boundary
1. **CommerceGov (Downstream)** is the production authority layer. It connects to Shopify and applies changes securely.
2. **Taskmaster (This Agent)** is the intelligence layer. It receives a proposed event, securely leases it to prevent duplicate execution, asks an LLM to assess it against policy, and submits a **governed proposal** to CommerceGov. 
3. Taskmaster has **ZERO Shopify credentials**. Its only output is a versioned proposal DTO handed to CommerceGov. 
4. The system is designed to gracefully handle adversarial failures, evidence drift, and duplicate events safely. Python deterministic logic completely overrides model outputs for safety constraints.

## How to Run the Demo
We provide a local CLI runner that simulates 6 adversarial scenarios, proving that Taskmaster's internal state machine strictly bounds the model.

```bash
# Run deterministic offline scenarios
python hackathon_demo.py

# Run live with Vertex AI Gemini model (requires ADC set up)
python hackathon_demo.py --live
```

## What the Judge Should Observe
Observe the `[DECISION BOUNDARY]` and `[COMMERCEGOV HANDOFF]` sections of the output:
- **Scenario 1 (Safe Continuation):** Model returns `AUTONOMOUSLY_CONTINUE` -> Taskmaster yields a `CommerceGovProposalV1` via Handoff API.
- **Scenario 2 (Human Authority):** Model detects a subjective decision -> Taskmaster transitions to `WAITING_FOR_HUMAN_AUTHORITY`. No proposal is created.
- **Scenario 3 (Policy Block):** Model detects a banned keyword -> Taskmaster halts execution as `BLOCKED`.
- **Scenario 4 (Duplicate Replay):** Identical event received while previous execution exists. Taskmaster returns `AUTONOMOUSLY_CONTINUABLE` from memory without calling the LLM a second time.
- **Scenario 5 (Evidence Drift):** A new request reuses the old `event_id` but modifies the payload. Taskmaster detects the mismatched fingerprint and securely halts with `409: Event ID conflict`.
- **Scenario 6 (Ambiguous Outcome):** Simulates a connection timeout after the LLM request is dispatched. Taskmaster securely transitions to `ASSESSMENT_OUTCOME_UNKNOWN` to ensure no unsafe retry occurs.

## Why Gemini?
Gemini uses its reasoning capabilities to determine whether complex tone guidelines and product definitions are aligned (e.g. classifying a change as subjective or safe).

## Why is Python Authoritative?
While Gemini recommends an outcome, the Python layer validates the schema, manages state machine transitions, enforces lease ownership, prevents duplication via hashing, and handles failure invariants.

## Google Technologies Used
- **Google ADK:** For bounded and schema-constrained LLM agent invocation.
- **Vertex AI Gemini:** `gemini-3.1-pro-preview` for reasoning.
- **Firestore:** Used for ACID-compliant distributed locking and single-flight lease management.

## Limitations & Next Steps
- This hackathon version focuses purely on bounding the agent and idempotency guarantees.
- The next step is a live integration with the unified CommerceGov platform and a fully persistent Cloud Run deployment with Pub/Sub ingress.