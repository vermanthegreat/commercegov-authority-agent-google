# CommerceGov Authority Agent - Google Hackathon

## WHAT IS THIS?
This project is an intelligent "Taskmaster" agent designed to safely sandbox autonomous decision-making for e-commerce. It acts as an authority assessment layer before any production changes occur.

## WHY DOES IT EXIST?
The same operational event can require a different authority response because of its history and context. Delegating AI authority over a merchant's production systems is extremely risky. An autonomous agent needs a safe sandbox to assess a proposed change based on context and history, without ever obtaining the credentials or ability to write directly to production.

## WHAT DOES GEMINI DO?
Gemini receives a bounded structured context (the current event + relevant operational history) and performs a semantic assessment. It determines if an event genuinely requires human operator attention or if it is safe to suppress based on recent context (e.g., escalating if a title change was repeatedly attempted after being previously flagged).

## WHAT DOES PYTHON ENFORCE?
While Gemini recommends an outcome, the deterministic Python layer:
- Validates the schema.
- Enforces single-flight processing leases.
- Maintains bounded, tenant-isolated structured history.
- Prevents duplication via cryptographic fingerprinting.
- Rejects adversarial evidence drift deterministically.
- Submits the final versioned governed proposal.

## WHAT DOES COMMERCEGOV DO?
CommerceGov is the external, pre-existing governance control plane. It holds the production authority. It receives the proposal from this agent and handles approval and execution.

## ARCHITECTURE
Event → Taskmaster → bounded history → Gemini / Authority Intelligence → attention → governed proposal → CommerceGov

## WHAT GOOGLE TECHNOLOGY IS USED?
- **Google ADK:** For bounded, schema-constrained LLM agent invocation.
- **Vertex AI Gemini:** `gemini-3.1-pro-preview` model for reasoning and structured generation over historical context.
- **Firestore:** Used for ACID-compliant distributed locking, single-flight lease management, and checkpointing.
- **Cloud Run:** Fully containerized and prepared for serverless deployment.    

## HOW DO I RUN THE DEMO?
Run the offline deterministic demo (requires no external credentials, proves the Phase 4 intelligence invariants):
```bash
python hackathon_demo.py
```
*Offline demo mode reproduces the same structured assessment contract without requiring external credentials. The live path uses Gemini over the bounded context.*

**The demo exercises 3 key scenarios:**
1. **Scenario 1 — SAFE CONTINUATION:** An event with no relevant risk history is safely suppressed.
2. **Scenario 2 — KILLER DEMO (SAME EVENT, DIFFERENT HISTORY):** The exact same current event is processed twice, but with two different historical contexts. The deterministic state machine proves that related risk history elevates the authority response, while unrelated history does not.
3. **Scenario 3 — ADVERSARIAL PROTECTION:** Deterministic fail-closed logic securely rejects an event ID replay with evidence drift, independent of the model.

## WHAT DOES NOT HAPPEN?
- **The Agent DOES NOT write directly to Shopify.** It possesses ZERO Shopify credentials.
- **The Agent DOES NOT grant itself production authority.** It only yields a governed proposal.
- **The Deterministic Offline Demo DOES NOT require Shopify or CommerceGov live access.**