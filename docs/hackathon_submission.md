# Taskmaster: CommerceGov Authority Agent

## ONE-LINE DESCRIPTION
A safe, bounded AI intelligence sandbox that assesses e-commerce mutations against complex policies without ever holding production write credentials.

## PROBLEM
Delegating AI authority over a merchant's production systems is inherently risky. If an autonomous agent hallucinates or makes a subjective misjudgment, it can cause immediate, unrecoverable damage to live storefronts (like Shopify). An agent needs a safe sandbox to assess a proposed change based on context and policy, without ever obtaining the credentials or ability to write directly to production.

## SOLUTION
Taskmaster acts as a rigid trust boundary. It ingests proposed commerce events and uses Gemini (via Google ADK) to reason about them against policy. However, the AI output is strictly bound by deterministic Python logic. The agent is physically isolated from the production control plane (CommerceGov) and has ZERO Shopify credentials. It outputs only a governed, versioned `CommerceGovProposalV1` that the downstream system can choose to approve or execute.

## HOW IT WORKS
1. **Normalize & Fingerprint:** An event arrives, and Taskmaster hashes it to guarantee deterministic evidence tracking.
2. **Single-flight Lease:** Taskmaster takes a Firestore lease to ensure no duplicate AI processing occurs.
3. **Structured Assessment:** Google ADK invokes Vertex AI (Gemini) to evaluate subjective guidelines (e.g., brand tone).
4. **Deterministic Enforcement:** Python forces a transition (like `BLOCKED` or `WAITING_FOR_HUMAN_AUTHORITY`) if the LLM hallucinated an unsafe continuation.
5. **Handoff:** A governed `CommerceGovProposalV1` is sent to the downstream authority. 

## GOOGLE TECHNOLOGIES USED
- **Google ADK:** Core framework for the structured LlmAgent.
- **Vertex AI (Gemini 3.1 Pro Preview):** Reasoning engine for subjective policy decisions.
- **Firestore:** ACID-compliant, distributed single-flight lease management.
- **Cloud Run:** Containerized for serverless deployment.
- **Pub/Sub:** Event ingress adapter.

## WHAT WAS BUILT FOR THE HACKATHON
For this hackathon, we built the entire Taskmaster sandbox repository:
- The Google ADK / Gemini integration.
- The deterministic state machine and single-flight lease system using Firestore.
- The 6 adversarial protection scenarios proving boundary security.
- The offline & live mock demonstration harness.

## WHAT COMMERCEGOV PROVIDES EXTERNALLY
CommerceGov is a pre-existing downstream governance control plane. Taskmaster does NOT build the execution layer, Shopify API connections, or the actual apply/verification workers. Taskmaster is the upstream "intelligence sandbox" that hands off to CommerceGov.

## TRUST / SAFETY BOUNDARY
**AI proposes; Python constrains; CommerceGov governs.**
The system guarantees that an ambiguous LLM timeout, a hallucinated "safe" classification on a risky change, or an event duplicate/replay will always fail closed, protecting the merchant's live store.

## DEMO
Our demo runs locally via CLI, showcasing 6 deterministic scenarios (including evidence drift and duplicates) and a live Vertex AI mode proving the ADK bounds the LLM effectively.

## CHALLENGES
Mapping a stochastic LLM output to a highly deterministic state machine. If an LLM call times out after being dispatched, we cannot safely assume it failed, nor can we safely retry it (to avoid duplicate billing or conflicting decisions). We solved this via the `ASSESSMENT_OUTCOME_UNKNOWN` state.

## WHAT WE LEARNED
By extracting the reasoning layer from the execution layer, we can confidently deploy cutting-edge AI models without compromising production safety invariants.

## NEXT STEPS
Integrate Taskmaster into the live CommerceGov event bus using Pub/Sub and deploy the containerized service to Cloud Run for production-scale traffic.
