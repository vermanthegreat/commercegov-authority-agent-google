# Taskmaster: CommerceGov Authority Agent

## TRACK
The Taskmaster

## ONE-LINE DESCRIPTION
A safe, bounded AI intelligence sandbox that assesses operational commerce events against their structured history without ever holding production write credentials.

## PROBLEM
Delegating AI authority over a merchant's production systems is inherently risky. Furthermore, the same operational event can require a drastically different authority response depending on its historical context. An agent needs a safe sandbox to assess a proposed change based on context and history, without ever obtaining the credentials or ability to write directly to production.

## SOLUTION
Taskmaster acts as a rigid trust boundary. It ingests proposed commerce events and uses Gemini (via Google ADK) to reason about them against bounded, structured operational history. However, the AI output is strictly bound by deterministic Python logic. The agent is physically isolated from the production control plane (CommerceGov) and has ZERO Shopify credentials. It outputs only a governed, versioned `CommerceGovProposalV1` that the downstream system can choose to approve or execute.

## HOW IT WORKS
1. **Event → Taskmaster:** An event arrives, and Taskmaster hashes it to guarantee deterministic evidence tracking.
2. **Bounded History:** Taskmaster fetches relevant, tenant-isolated structured history from Firestore.
3. **Gemini / Authority Intelligence:** Google ADK invokes Vertex AI (Gemini) to evaluate the event considering the retrieved history.
4. **Attention / Escalation:** Python maintains attention states and forces a fail-closed transition (like `BLOCKED`) if the LLM hallucinated an unsafe continuation or if evidence drift occurred.
5. **Governed Proposal → CommerceGov:** A governed `CommerceGovProposalV1` is sent to the downstream authority.

## GOOGLE TECHNOLOGIES USED
- **Google ADK:** Core framework for the structured LlmAgent.
- **Vertex AI (Gemini 3.5 Flash):** Reasoning engine for subjective policy decisions and historical correlations.
- **Firestore:** ACID-compliant, distributed single-flight lease management.
- **Cloud Run:** Containerized for serverless deployment.
- **Pub/Sub:** Event ingress adapter.

## WHAT WAS BUILT FOR THE HACKATHON
For this hackathon, we built the entire Taskmaster sandbox repository:
- The Google ADK / Gemini integration mapping history and event context.
- The deterministic state machine and single-flight lease system using Firestore.
- The adversarial protection scenarios proving boundary security.
- The offline & live mock demonstration harness showcasing Phase 4 structured intelligence.

## WHAT COMMERCEGOV PROVIDES EXTERNALLY
CommerceGov is a pre-existing downstream governance control plane. Taskmaster does NOT build the execution layer, Shopify API connections, or the actual apply/verification workers. Taskmaster is the upstream "intelligence sandbox" that hands off to CommerceGov.

## TRUST / SAFETY BOUNDARY
**AI proposes; Python constrains; CommerceGov governs.**
The system guarantees that an ambiguous LLM timeout, a hallucinated "safe" classification on a risky change, or an event duplicate/replay will always fail closed, protecting the merchant's live store.

## DEMO
Our demo runs locally via CLI (`python hackathon_demo.py`), showcasing 3 deterministic scenarios:
1. **Safe Continuation:** An event with no relevant risk history is safely suppressed.
2. **Killer Demo (Same Event, Different History):** Identical events are assessed differently because one has a related risk history.
3. **Adversarial Protection:** A secure rejection of an event ID replay with evidence drift.

## CHALLENGES
Mapping a stochastic LLM output to a highly deterministic state machine. Correlating semantic meaning across time requires bounded history matching, but injecting too much history confuses the model. We solved this by using strict namespace isolation and bounded composite attention keys.

## WHAT WE LEARNED
By extracting the reasoning layer from the execution layer and bounding it with structured history, we can confidently deploy cutting-edge AI models to escalate only truly risky mutations without compromising production safety invariants.

## NEXT STEPS
Integrate Taskmaster into the live CommerceGov event bus using Pub/Sub and deploy the containerized service to Cloud Run for production-scale traffic.