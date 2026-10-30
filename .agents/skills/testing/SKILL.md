---
name: testing
description: Regression-triage and test discipline for pg-rag — how to decide fix-source-vs-update-test when a test fails, contract-change rules, and where the layer→test map lives. Use when changing behavior, editing tests, or triaging a failing test.
---

# testing — regression triage & test discipline

**537 collected / 82 deselected / 455 default** (the
default `not slow` marker deselects 82: 16 data-heavy tests + the 66
parametrized golden cases; one POSIX-only port-owner case skips on Windows) —
quoted verbatim because `mise drift` asserts the triple against a real
collection. Everything is
offline — the golden gate is the only suite that needs a server, and only the
LLM (:8080). When a test fails, triage BEFORE
editing. The full layer→test contract map is `docs/TEST_CONTRACTS.md` — READ IT
(this skill is the decision procedure, not the map; the map is the single owner
of which file defends which contract).

## Triage (in order)

1. **Identify the layer.** `docs/TEST_CONTRACTS.md` maps every test file to its
   layer (document-gen / agentic loop + FTS corpus / store / loaders-curation /
   golden / llm-client / lint / compliance / service control / docs
   drift) and the source it protects.
2. **Did the source legitimately change?**
   - Yes → update the test to the new contract (a CONTRACT CHANGE — see rules).
   - No → the failure is a real regression: **fix the source, never the test.**
3. **Wrong-layer check.** If your fix targets the store's `documents` rows,
   `data/derived/wiki_parsed.json`, `data/derived/*`, or a real
   `data/wiki/.meta.json` — STOP. Those are build outputs (`RULES.md #2`):
   change the builder that produces them, not the artifact.
4. **Never edit a test "to make it pass."** A test edit either fixes a broken
   test (prove the source is fine) or records a deliberate contract change
   (state the new contract, pass the sibling suite).

## Contract-change hard rules

- Editing what a test asserts is a **contract change, not a workaround**
  (`RULES.md #12`).
- State the new contract in the test (docstring or `# contract: …` comment) so
  intent survives.
- Run the layer's **sibling suite** (`docs/TEST_CONTRACTS.md`), not just the
  edited test — the old contract is usually asserted elsewhere too.
- Never weaken an assertion to dodge a real regression.
- **Ground verdicts by reading source/test lines** — do not encode a finding
  from a summary that "fits the expected story" (that is how wrong guardrails
  get written).

## Shared-function facet coverage

A single function is asserted across several files (each a *different facet*);
editing the function must keep every facet green. Watch `run_loop`
(`test_agentic_loop.py` = round protocol/parsing, `test_agentic_tune.py` =
sweep/compare, `test_golden_check.py` = the offline gate drives it via
`eval.run_case`) and `execute_tool` (`test_agentic_tools.py` owns it;
`test_agentic_loop.py` exercises it through `_execute`). Authoritative list:
`TEST_CONTRACTS.md` L3.

## After a change

- Corpus change → `tests/test_agentic_tools.py` (`corpus_search` is FTS5 over
  the store's `documents`), then `mise sql-store`.
- Tool-loop change → the L3 suite (`test_agentic_tools.py`,
  `test_agentic_loop.py`, `test_agentic_tune.py`), then a `mise agentic-chat`
  smoke (LLM only).
- Store-ingestion change → L4 (`test_sqlstore_build.py`,
  `test_session_ingest.py`, `test_glogger_ingest.py`, `test_preflight_sync.py`),
  then `mise sql-store`.
- Service-control change → L10 (`test_services.py`), then a live `mise start` /
  `mise status` / `mise stop` smoke (the manager owns real :8080/:7860).
- Doc-gen change → inspect representative rows after `mise sql-store`.
- Golden change → `test_golden_check.py` must collect it offline (it runs
  through the loop and skips without the LLM). A full `mise golden` run is a
  long background job — use `mise golden-one <id>` while iterating.
- Doc/skill change → run `mise drift` — the docs, skills, AGENTS.md, RULES.md,
  and TEST_CONTRACTS must stay the single-owner-truthful set it checks
  (L11: `test_check_docs.py` pins the pointer scan itself).
- Compliance-gate change (`scripts/check_constraints.py`, the TOS/CoC policy
  doc, or the hook/task wiring) → `tests/test_constraints.py`, then
  `mise compliance`. §2's patterns and §6's wiring are the contract; §7 forbids
  relaxing either to make a violation pass.
