---
name: testing
description: Regression-triage and test discipline for pg-rag — how to decide fix-source-vs-update-test when a test fails, contract-change rules, and where the layer→test map lives. Use when changing behavior, editing tests, or triaging a failing test.
---

# testing — regression triage & test discipline

The suite is 597 passing tests (655 collected, 42 skipped — offline server-guards; 16 slow deselected). When a test
fails, triage BEFORE editing. The full layer→test contract map is
`docs/TEST_CONTRACTS.md` — READ IT (this skill is the decision procedure, not
the map; the map is the single owner of which file defends which contract).

## Triage (in order)

1. **Identify the layer.** `docs/TEST_CONTRACTS.md` maps every test file to
   its layer (document-gen / index / retrieval / rerank-classifier / pipeline /
   eval / loaders) and the source it protects.
2. **Did the source legitimately change?**
   - Yes → update the test to the new contract (a CONTRACT CHANGE — see rules).
   - No → the failure is a real regression: **fix the source, never the test.**
3. **Wrong-layer check.** If your fix targets `data/documents.json`,
   `data/wiki/.parsed.json`, `data/derived/*`, `data/bm25_index.pkl`, or a real
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
editing the function must keep every facet green. Watch `retrieve()`
(`test_bm25.py`, `test_retrieval_unit.py`, `test_where_filter.py`,
`test_retrieval_trace.py`, `test_retrieval_eval.py`) and `_hybrid_fuse`
(`test_bm25.py` + `test_rerank.py`). Authoritative list: `TEST_CONTRACTS.md` L3.

## After a change

- Retrieval change → run the L3 + L4 regression set (`TEST_CONTRACTS.md`).
- Doc-gen change → inspect representative docs after `pgrag build-documents`.
- Index change → `tests/test_build_index.py` siblings + `pgrag validate`.
- Doc/skill change → run `mise drift` — the docs, skills, AGENTS.md, RULES.md,
  and TEST_CONTRACTS must stay the single-owner-truthful set it checks.