---
name: testing
description: Regression-triage and test discipline for pg-rag — how to decide fix-source-vs-update-test when a test fails, contract-change rules, and where the layer→test map lives. Use when changing behavior, editing tests, or triaging a failing test.
---

# testing — regression triage & test discipline

The suite collects 483 tests; the default `not slow` marker deselects 78 (16
data-heavy tests + the 62 parametrized golden cases), leaving 405 in the default
offline run (one POSIX-only port-owner case skips on Windows). Everything is
offline — the golden gate is the only suite that needs a server, and only the
LLM (:8080). When a test fails, triage BEFORE
editing. The full layer→test contract map is `docs/TEST_CONTRACTS.md` — READ IT
(this skill is the decision procedure, not the map; the map is the single owner
of which file defends which contract).

## Triage (in order)

1. **Identify the layer.** `docs/TEST_CONTRACTS.md` maps every test file to its
   layer (document-gen / bm25 / agentic loop / store / loaders-curation / golden
   / llm-client / lint / compliance / service control / corpus-build lock) and the source it protects.
2. **Did the source legitimately change?**
   - Yes → update the test to the new contract (a CONTRACT CHANGE — see rules).
   - No → the failure is a real regression: **fix the source, never the test.**
3. **Wrong-layer check.** If your fix targets `data/documents.json`,
   `data/wiki/.parsed.json`, `data/derived/*`, `data/bm25_index.pkl`,
   `data/tool_documents.json`, `data/tool_bm25.pkl`, or a real
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

- BM25 / corpus change → `tests/test_bm25.py` + `tests/test_bm25_persist.py`,
  then `mise build-bm25` (and `mise build-tool-corpus` if the tool variant
  changed).
- Tool-loop change → the L3 suite (`test_agentic_tools.py`,
  `test_agentic_loop.py`, `test_agentic_tune.py`), then a `mise agentic-chat`
  smoke (LLM only).
- Store-ingestion change → L4 (`test_sqlstore_build.py`,
  `test_session_ingest.py`, `test_glogger_ingest.py`, `test_preflight_sync.py`,
  `test_publish_store.py`), then `mise sql-store`.
- Service-control change → L10 (`test_services.py`), then a live `mise start` /
  `mise status` / `mise down` smoke (the manager owns real :8080/:7860).
- Corpus-lock change → L11 (`test_buildlock.py`), then hold the lock from a
  second shell and confirm `mise build-tool-corpus` refuses (the pre-hook probes
  the same file, so an agent-issued run is blocked before it starts).
- Doc-gen change → inspect representative docs after `pgrag build-documents`.
- Golden change → `test_golden_check.py` must collect it offline (it runs
  through the loop and skips without the LLM). A full `mise golden` run is a
  long background job — use `mise golden-one <id>` while iterating.
- Doc/skill change → run `mise drift` — the docs, skills, AGENTS.md, RULES.md,
  and TEST_CONTRACTS must stay the single-owner-truthful set it checks.
- Compliance-gate change (`scripts/check_constraints.py`, the TOS/CoC policy
  doc, or the hook/task wiring) → `tests/test_constraints.py`, then
  `mise compliance`. §2's patterns and §6's wiring are the contract; §7 forbids
  relaxing either to make a violation pass.
