# Taskmaster Hackathon Demo Script & Talk Track

## 30-Second Opening
"Delegating AI authority over a merchant's production systems is risky. Today, we're demonstrating 'Taskmaster' - an intelligent intelligence layer that acts as a safe sandbox. It assesses proposed changes based on complex context and policy using Gemini, without ever holding the credentials or ability to write directly to production. Taskmaster proposes; CommerceGov (our downstream production system) governs and executes."

## 90-Second Architecture
"When a commerce event arrives, Taskmaster normalizes it and generates a deterministic fingerprint. It uses Firestore for a single-flight lease to prevent race conditions. We invoke the Google ADK and Vertex AI's Gemini model to perform a structured assessment. 
Why Gemini? Because determining if a product title aligns with a 'professional tone' is highly subjective. 
However, Gemini does NOT receive production authority. Our Python deterministic layer enforces the final rules, creates an evidence-bound checkpoint, and yields a versioned `CommerceGovProposalV1` to the downstream control plane."

## Demo Sequence
To run the demo:
```bash
# Run deterministic offline scenarios (proves the state machine)
python hackathon_demo.py

# Run live with Vertex AI Gemini model
python hackathon_demo.py --live
```

### The Six Scenarios:
1. **Safe Continuation:** The model correctly determines the change aligns with policy. Taskmaster returns `AUTONOMOUSLY_CONTINUABLE` and creates a `CommerceGovProposalV1`.
2. **Human Authority Required:** The model flags the change as subjective or risky. Taskmaster transitions to `WAITING_FOR_HUMAN_AUTHORITY`. No proposal is handed off.
3. **Policy Block:** The model detects a banned keyword. Taskmaster halts execution as `BLOCKED`.
4. **Duplicate Replay:** An identical event is received while a previous execution exists. Taskmaster returns `TERMINAL_REPLAY` safely without calling the LLM again.
5. **Evidence Drift:** A new request reuses the old `event_id` but modifies the payload. Taskmaster detects the mismatched fingerprint and securely halts with `EVENT_ID_CONFLICT`.
6. **Ambiguous Outcome:** We simulate a connection timeout after the LLM request is dispatched. Taskmaster securely transitions to `ASSESSMENT_OUTCOME_UNKNOWN` to ensure no unsafe autonomous retries occur.

## Google Technologies Used
- **Google ADK:** For bounded and schema-constrained LLM agent invocation.
- **Vertex AI Gemini:** `gemini-3.5-flash` for deep reasoning.
- **Firestore:** Distributed locking and single-flight lease management.
- **Cloud Run:** Readied for serverless Dockerized deployment.
- **Pub/Sub:** Compatible adapter for seamless async integration.

## Closing
"Taskmaster can decide how far an autonomous system may proceed based on deep contextual reasoning. But CommerceGov remains the system that governs actual production authority. AI proposes; Python constrains; CommerceGov governs."
