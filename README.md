# CommerceGov Authority Agent — Google Hackathon

## WHAT IS THIS?
This project is an intelligent "Taskmaster" agent designed to safely sandbox autonomous decision-making for e-commerce. It acts as an authority assessment layer before any production changes occur.

## WHY DOES IT EXIST?
Delegating AI authority over a merchant's production systems is extremely risky. An autonomous agent needs a safe sandbox to assess a proposed change based on context and policy, without ever obtaining the credentials or ability to write directly to production.

## WHAT DOES GEMINI DO?
Gemini (via Google ADK) uses its reasoning capabilities to assess complex, subjective policy guidelines against a proposed event (e.g., determining if a product title change aligns with a "professional brand tone"). It outputs a structured recommendation (e.g., `AUTONOMOUSLY_CONTINUE`, `HUMAN_AUTHORITY_REQUIRED`, `BLOCKED`).

## WHAT DOES PYTHON ENFORCE?
While Gemini recommends an outcome, the deterministic Python layer:
- Validates the schema.
- Manages state machine transitions.
- Enforces single-flight processing leases.
- Prevents duplication via cryptographic fingerprinting.
- Handles failure invariants securely (failing closed).
- Submits the final versioned `CommerceGovProposalV1`.

## WHAT DOES COMMERCEGOV DO?
CommerceGov is the external, pre-existing governance control plane. It holds the production authority. It receives the `CommerceGovProposalV1` from this agent, executes the change in Shopify securely if approved, and handles verification. 

## WHAT GOOGLE TECHNOLOGY IS USED?
- **Google ADK:** For bounded, schema-constrained LLM agent invocation.
- **Vertex AI Gemini:** `gemini-3.1-pro-preview` model for reasoning and structured generation.
- **Firestore:** Used for ACID-compliant distributed locking, single-flight lease management, and checkpointing.
- **Cloud Run:** Fully containerized and prepared for serverless deployment.
- **Pub/Sub:** Compatible event ingress via an envelope adapter.

## HOW DO I RUN THE DEMO?
Run the deterministically faked, offline test (no network calls, proves the state machine):
```bash
python hackathon_demo.py
```

Run with live Gemini (requires Application Default Credentials and Vertex API enabled):
```bash
python hackathon_demo.py --live
```

## WHAT DOES NOT HAPPEN?
- **Taskmaster DOES NOT write directly to Shopify.** It possesses ZERO Shopify credentials.
- **Taskmaster DOES NOT grant final production approval.** It only yields a governed proposal.
- **Taskmaster DOES NOT execute or apply changes.** Applied ≠ Verified. Assessment ≠ Production Approval.
