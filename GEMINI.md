# Gemini CLI Operating Contract - Taskmaster Hackathon

This document defines the persistent operating rules and change discipline for Gemini CLI when working in this repository.

## Gemini Execution Discipline

This section defines strict execution rules to ensure fast feedback, minimum tool cost, and exact scope control.

### P0 - Hard Safety & Correctness
1. **ANCHOR FIRST**: Before the first source or tool action, explicitly verify you are in the correct repository and branch. On mismatch, STOP immediately; perform no exploratory work.
2. **NO ARCHITECTURE REDISCOVERY**: If the prompt supplies architectural facts, exact file paths, or target symbols, treat them as authoritative. Do not run broad discovery to re-verify them.
3. **DIRECT EDITS ONLY**: Edit source files directly. Never create scratch files (e.g., `*_copy.py`, `fix*.py`, `tmp*.py`), ad-hoc patch scripts, or multiple copy generations unless strictly necessary due to a tool failure.
4. **REMEDIATION CONTRACT RULE**: If a defect is known and bounded, fix it directly. Do not restart architecture discovery.
5. **NO DRIFT**: Do not drift into other repositories (e.g., `C:/Projekti/CommerceGov`).
6. **NO SHOPIFY WRITES**: No direct Shopify write under any circumstances.

### P1 - Execution Efficiency
7. **NO-NEW-INFORMATION STOP**: If two consecutive tool calls produce no decision-relevant new information, STOP that tactic and choose a more direct strategy.
8. **MINIMUM SUFFICIENT INVESTIGATION**: Once target file/symbol is known, use targeted reads.
9. **SINGLE BROAD SEARCH MAXIMUM**: Unless a genuinely new unresolved question requires another, limit broad search to one.
10. **TEST EARLY**: After the first coherent patch, run the smallest relevant test.
11. **AVOID SHELL QUOTING LOOPS**: If two shell syntax/quoting attempts fail, change method. Do not continue quoting experiments.
12. **PURPOSEFUL ACTIONS**: Before every tool call ask: "Which explicit requirement does this action advance?" If none, do not call the tool.
13. **NO RE-PROVING**: Do not re-prove an already established invariant without contradictory evidence.

### P2 - Hygiene & Cleanup
14. **TEMP ARTIFACT CONTROL**: Do not create temporary patch frameworks or `dump*.txt` files. If a temporary file is genuinely required, keep it tracked in one designated temp area and remove it before completion.
15. **EVIDENCE-BASED DISCIPLINE**: Maintain internally: ESTABLISHED, CURRENT_HYPOTHESIS, NEXT_ACTION, STOP_CONDITION.

