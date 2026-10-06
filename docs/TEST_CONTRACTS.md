# TEST_CONTRACTS — what breaks when, and what to touch

The load-bearing guardrail for pg-rag: a layer → tests → contract map
with an explicit **regression-triage protocol**. The point is that a fresh
model run (no conversation history) can look at a failing test and decide
correctly — *fix the source* vs *update the test* — instead of patching a
symptom, weakening an assertion, or hand-editing a build artifact.

Read all of this before changing behavior and running a test against it.

**Ground-truth rule:** facts go into this map (contracts, regressions,
duplication, "known gaps") only after being verified by reading the exact
source/test lines. A summary or peer judgment that "fits the expected story"
is not evidence — an unverified finding encoded into a guardrail becomes a
wrong guardrail that future runs trust.

---

## Regression-triage protocol (the decision procedure)

When a test fails, in order:

1. **Identify the layer.** Find the file in the map below. That tells you
   which source path the contract comes from and what your change may
   legitimately affect.
2. **Did the source behavior legitimately change?**
   - **Yes** — your edit was *supposed* to alter that contract (e.g. you
     changed chunk sizing and a chunk-count test failed). → Update the test to
     the new contract. This is a **contract change**, which has hard rules
     (step 4).
   - **No** — the behavior change was incidental or wrong. → The failure is a
     real regression. **Fix the source. Never touch the test.**
3. **Wrong-layer check.** If your next edit targets
   `data/documents.json`, `data/wiki/.parsed.json`, `data/derived/*`,
   `data/bm25_index.pkl`, or a real `data/wiki/.meta.json` — **stop**.
   Those are build outputs (RULES.md #2). Fix the builder/loader that
   produces them. A failing test that reads an artifact points at its
   generator, not the artifact.
4. **Contract-change hard rules** (any edit that changes what a test
   *asserts*):
   - State the new contract in the test (a module/function docstring or a
     `# contract: …` comment) so the intent survives.
   - Run the **full sibling suite** for that layer (the column below), not
     just the test you edited — the failure often implies a sibling also
     asserts the old contract.
   - Do not weaken an assertion to dodge a real regression. If the old
     assertion was wrong, say why in the comment.
5. **Duplication watch.** The map marks contracts owned by **two files**.
   If you fix one, check/consolidate the other — otherwise the duplicate
   re-blooms as an unexplained failure.

Never edit a test "to make it pass." A test edit is either fixing a broken
test (prove the source is fine) or recording a deliberate contract change
(state it, run the sibling suite).

---

## The map

"Change ⇒ run" is the minimum command set after touching that layer.

### L1 — Document generation (builders)

- **Tests**: `test_documents.py`, `test_chunking.py`, `test_skill_profiles.py`,
  `test_summaries.py`, `test_gathering_summaries.py`, `test_doc_quality.py`,
  `test_flatten.py`, `test_resolve.py`, `test_metadata.py`,
  `test_wiki_expansion.py`, `test_wiki_builder.py`, `test_leveling.py`,
  `test_creature_zones.py`, `test_combat_xp.py`
- **Source**: `src/pgrag/documents/` (`builder.py`, `wiki_builder.py`,
  `chunking.py`, `resolver.py`, `skill_profiles.py`, `summaries.py`,
  `creature_zones.py`), `src/pgrag/build.py`
- **Contracts**: doc key shape `{id, type, text, metadata}`; metadata
  Chroma-safety (add-only, scalar-only, multi-value `" | "`-delimited —
  `test_doc_quality.py`); chunk max/overlap + `_chunk_N` ids; summary +
  gathering grouping/ranking; resolver cross-refs; leveling doc shape.
  `creature_zones.py` (`build_creature_zones_documents`): one
  `creature_<Name>` wiki doc per MOB page with ≥1 zone, `table=creatures`,
  `metadata.creature_type`/`locations`; zone order = MOB `area=` then zone
  categories, deduped; lead description stripped of all `{{`/`}}` wiki
  residue (hygiene guard — multi-line MOB templates must not leak into the
  description). `build_leveling_documents` emits `type="leveling"`.
  `_prepend_barter_options` (`builder.py`): item docs whose CDN
  `sources_items` entry is `type=Barter` get the NPC page's barter-table trade
  lines PREPENDED — to the item's wiki `How to Obtain`/`Uses` docs (entity_id,
  section match), else the CDN `source_items_<key>` doc. A table qualifies only
  on reward-cell evidence (last row cell word-boundary-matches the item name;
  single-cell rowspan rows prove nothing) — a table that merely lists the item
  as an ingredient is NOT appended, and items with no qualifying table are left
  untouched. The prepend (not append) keeps the explicit trade in the first
  chunk; chunk_tail placement let retrieval rank it low (royal-jelly-gathering,
  2026-10-06).
- **Change ⇒** `uv run pytest tests/test_documents.py tests/test_chunking.py
  tests/test_metadata.py tests/test_creature_zones.py tests/test_doc_quality.py`
  (+ the file you edited).
- **Version coupling**: changing document **shape** means bumping
  `DOCUMENTS_VERSION` in `src/pgrag/config.py` and re-stamping via
  `build-documents` — `build-index` refuses a stale generation (see L2).
- **Change ⇒** `uv run pytest tests/test_documents.py tests/test_chunking.py
  tests/test_metadata.py tests/test_doc_quality.py` (+ the file you edited).
  Then inspect representative docs after `pgrag build-documents` (RULES #7).
- **Version coupling**: changing document **shape** means bumping
  `DOCUMENTS_VERSION` in `src/pgrag/config.py` and re-stamping via
  `build-documents` — `build-index` refuses a stale generation (see L2).
- **Duplication watch**: `build_summary_documents` is defended by
  *three* files — `test_documents.py`, `test_summaries.py`,
  `test_gathering_summaries.py`. Their module docstrings name which
  function each owns; if you edit summary generation, run all three.

### L2 — Index / build / persistence

- **Tests**: `test_build_index.py`, `test_build_integration.py`,
  `test_hashes.py`, `test_health_check.py`, `test_embed_validation.py`,
  `test_embed_fallback.py`, `test_bm25_persist.py`
- **Source**: `src/pgrag/vectorstore/build_index.py`, `hashes.py`,
  `health_check.py`; `src/pgrag/rag/bm25.py` (persistence);
  `src/pgrag/embeddings/llama_embeddings.py` (embed client + validation)
- **Contracts**: `EMBEDDING_DIM=384` validation on upsert; incremental
  re-embed skip (embedding_hash = id+text only → metadata-only changes never
  re-embed); metadata add-only (`collection.update` merges, never replaces);
  health_check as documents↔index gatekeeper; bm25 persisted index ≡ in-memory
  rankings and mtime-derived cache invalidation; embed overflow fallback
  (batch bisection on `_InputTooLong` isolating the over-window text, not
  serializing the whole batch) and `validate_embeddings` dim/type checks.
- **Change ⇒** `uv run pytest tests/test_build_index.py tests/test_hashes.py
  tests/test_health_check.py tests/test_embed_validation.py
  tests/test_embed_fallback.py tests/test_bm25_persist.py`, then
  `uv run pgrag validate`.
- **Covered directly**: `load_documents()`'s refusal of a stale/missing
  `DOCUMENTS_VERSION` (`build_index.py:20-39`) is asserted by
  `test_build_index.py::test_documents_version_refuses_stale` and
  `::test_documents_version_refuses_missing_marker` (tmp marker, expect
  `ValueError` mentioning "stale"/"build-documents"). The check is
  version-agnostic — do **not** hardcode `DOCUMENTS_VERSION == 4` in a test;
  it must fail on any stored≠current. This guard is tested, not a gap.

### L3 — Retrieval core (the regression set)

- **Tests**: `test_bm25.py`, `test_retrieval_unit.py`,
  `test_retriever_spelling.py`, `test_spelling.py`, `test_where_filter.py`,
  `test_entity_retrieval.py`, `test_entity_retrieval_cache.py`
- **Source**: `src/pgrag/rag/retriever.py`, `bm25.py`, `spelling.py`,
  `entity_retrieval.py`, `query_plan.py` (native extraction), `where_filter`
  predicates
- **Contracts**: BM25 lexical scoring; `retrieve()` call shape (where-filters,
  `n_results` per query type: comparison=20, general=15/3); spelling
  correction **before** embedding; where-predicate operators
  (`== <= >= > < !=`, delimited membership, compound); `_hybrid_fuse` RRF;
  entity dossier ordering/budget; deterministic skill-ability pull for
  skill dossiers (all standalone ability docs where metadata.skill == hub
  skill code — regression guard
  `test_skill_dossier_pulls_own_abilities_deterministically`); doc cache
  invalidation.
- **Change ⇒** (any retrieval change — this is the documented regression set)
  `uv run pytest tests/test_bm25.py tests/test_retrieval_unit.py
  tests/test_rerank.py tests/test_rerank_client.py tests/test_rerank_fallback.py
  tests/test_retriever_spelling.py`
- **Shared-function facet coverage (not duplication — but read before editing
  the function)**: a single function is asserted across files, each testing a
  *different facet*. Editing the function must keep every facet green; no file
  is the sole owner.
  - `retrieve()` — `test_bm25.py` (hybrid routing, comparison/general
    `n_results`), `test_retrieval_unit.py` (metadata where-filters, default
    no-filter, citation), `test_where_filter.py` (operator + token
    post-fusion filters), `test_retrieval_trace.py` (trace fields),
    `test_retrieval_eval.py` (query-type pass-through).
  - `_hybrid_fuse` — `test_bm25.py` (RRF intersection, multiplier, bm25-only
    doc) **and** `test_rerank.py` (tsys chunk/base caps, origin forms).
  - The five BM25-class tests (`test_bm25_rank_known_doc_highest` etc.) live
    **only** in `test_bm25.py` — there is no second copy; do not "consolidate"
    a duplicate that does not exist.

### L4 — Rerank, classifier, query plan, entity detection

- **Tests**: `test_rerank.py`, `test_rerank_client.py`,
  `test_rerank_fallback.py`, `test_query_classifier.py`,
  `test_query_classifier_cache.py`, `test_query_plan.py`,
  `test_entity_detection.py`
- **Source**: `src/pgrag/rag/reranker_client.py`, `query_classifier.py`,
  `query_plan.py`; `retriever._rerank_or_cross_encoder`, `_run_rerank`,
  `_entity_name_match`, `_name_injection_ids`
- **Contracts**: lexical `_term_overlap` fallback scores (1.0/0.25/0.0);
  client truncation + `/llama.cpp` batch caps (`<8192` tokens); server-down
  fallback keeps lexical order + records failure stats; classifier intents
  (entity/comparison/general/leveling, 50+ parametrized cases); `plan_query`
  native `$and` extraction + ingredient stopword phrasing; entity
  word-boundary/plural/typo resolution. **Creature-location routing**
  ("locations with/where … deer|sheep|goats|…"): classifier `AGGREGATION`
  keeps such listings `general` (not a single-entity skill-trainer hijack),
  and `plan_query` returns the high-confidence plan
  `{"native": {"table": "creatures"}, "token": {}, "label": "creature locations"}`
  when a location phrase AND an animal/creature token co-occur — asserted by
  `test_query_classifier.py::test_location_listing_of_animals_stays_general`
  and `test_query_plan.py::test_creature_location_listing_plans_to_creatures_table`.
- **Change ⇒** `uv run pytest tests/test_rerank*.py tests/test_query_classifier*.py
  tests/test_query_plan.py tests/test_entity_detection.py`
- **Change ⇒** `uv run pytest tests/test_rerank*.py tests/test_query_classifier*.py
  tests/test_query_plan.py tests/test_entity_detection.py`
- Note: this trio is **not** redundant — `test_rerank.py` = lexical scoring +
  fusion caps, `test_rerank_client.py` = HTTP protocol, `test_rerank_fallback.py`
  = orchestration paths.

### L5 — Pipeline, LLM, prompts

- **Tests**: `test_pipeline_ask.py`, `test_pipeline_stream.py`,
  `test_pipeline_entity.py`, `test_pipeline_lookup.py`,
  `test_pipeline_comparison.py`, `test_pipeline_summary.py`,
  `test_pipeline_synthesis.py`, `test_pipeline_synonyms.py`, `test_llm.py`,
  `test_prompts.py`
- **Source**: `src/pgrag/rag/pipeline.py` (incl. `_expand_synonyms`,
  covered by `test_pipeline_synonyms.py`), `llm.py`, `prompts.py`,
  `resolve.py`
- **Contracts**: `ask()` end-to-end (entity dossier vs general top-k);
  `_gap_fill` bounds (`_AGENTIC_MAX_ROUNDS = 1`, id-deduped, budget-capped);
  deterministic `temp=0`/`seed=0`; streaming token emission + gap-fill reset;
  prompt fabrication-block directives ("never fabricate"); source citation
  formatting. `_prepare_general` widens the surfaced count to **80** (vs 40)
  when the plan's native filter narrows Chroma to a single enumeration
  family table (`creatures` spawn zones, or `summaries` for the computed
  per-NPC gift summaries) so list-all queries — "locations with deer…",
  "who can I gift X to?" — surface the family's complete member list
  instead of dropping rarer members at the rerank cut — asserted by
  `test_pipeline_ask.py::test_creature_location_plan_filters_table_and_widens_count`.
- **Change ⇒** `uv run pytest tests/test_pipeline_*.py tests/test_llm.py
  tests/test_prompts.py`
- Note: the `test_pipeline_*.py` files are **not** one contract each — they
  overlap on routing concerns (comparison routing ↔ `test_prompts.py`; summary
  scoring ↔ prompt content; entity↔general fallback ↔ `test_pipeline_ask.py`).
  Routing tests live in `test_pipeline_*`, prompt-content tests in
  `test_prompts.py`. If you touch pipeline routing, run the whole `L5` set.

### L6 — Golden + IR evaluation

- **Tests**: `test_golden_check.py`, `test_golden_xfail_gate.py`,
  `test_golden_rerun.py`, `test_retrieval_eval.py`,
  `test_retrieval_trace.py`, `test_bakeoff_corpus.py`
- **Source**: `scripts/golden_check.py`, `scripts/golden_rerun.py`,
  `src/pgrag/rag/retrieval_eval.py`,
  `rag/retriever.retrieve` (trace), `scripts/bakeoff_corpus.py`
- **Contracts**: golden shape `{id, question, type, facts: [[variants…]], xfail?: bool}` — `xfail: true` marks a known-gap probe: runner+test treat a persistent miss as expected, but a pass (gap just closed) is a FAIL forcing unflagging; offline guard in test_golden_xfail_gate.py. `"store": true` marks a store-only case (facts in the agentic SQLite store, e.g. glogger play history): pipeline harnesses skip it; it runs via `mise agentic-eval` (appended to the short tier).
  (changing it breaks offline auto-collection — RULES/WATCHDOG trap); IR
  metric canonical-unit counting (`_row_`/`_coverage`/`_chunk_` collapse);
  Tiered split: `test_golden_facts_short` (`-m short`, 8 files, ~3-5 min) and `test_golden_facts_long` (`-m long`, 43 files, ~11-23 min); bare run includes all 51 pipeline files (store-only cases excluded — they run via `mise agentic-eval`, whose short tier is 16 cases).
  trace field set + `ask()`-fill no-payload-mutation; bakeoff corpus
  type-stratified queries.
- **Change ⇒** `uv run pytest tests/test_golden_check.py tests/test_retrieval_eval.py
  tests/test_retrieval_trace.py tests/test_bakeoff_corpus.py`
  Use `-m short` for the quick tier (8 files, ~3-5 min) or `-m long` for the full tier (43 files).
  Include `tests/test_golden_rerun.py` in the sibling run when the rerun
  tooling shape changes.
  **Golden-rerun tooling** (`scripts/golden_rerun.py`, offline tests in
  `test_golden_rerun.py`): `mise golden-one -- <id[,id2]>` reruns named
  case(s) fast; every harness run appends one
  `{id, missing, missing_facts, query_type, attempt_n, ts}` record to
  `data/golden/history.jsonl`; `mise golden-flaky` selects ids whose
  recent-10 miss-share exceeds 50% (plus manual entries in
  `data/golden/FLAKY.txt`); `mise golden-flaky-list` audits shares.
  history.jsonl is an accumulating ledger, **not a build output** — its
  tests use tmp paths only. `mise golden-type <type...>` runs one type's
  subset via `scripts/golden_check.py --type` (repeatable; `--max-facts N`
  caps case weight); both runners accept the flags. Fact specs are *variant lists* exactly so
  inflections of the same phrase (e.g. fireball-ability's
  "throw"/"throws a ball of fire") count as the same fact: add a variant,
  never delete one, when the corpus wording legitimately differs from the
  fact's first form.

### L7 — Loaders, wiki sync, curation, source guards

- **Tests**: `test_wiki_loader.py`, `test_download_wiki.py`,
  `test_data_immutability.py`, `test_curator.py`, `test_curator_scheduler.py`,
  `test_constants.py`, `test_connectivity.py`; plus `tests/conftest.py`'
  **session-scoped immutability guard** (snapshots `data/cdn` + `data/wiki`
  at session start, asserts unchanged at teardown).
- **Source**: `src/pgrag/loaders/`, `scripts/curator*.py`
- **Contracts**: wiki display names come only from `data/wiki/.meta.json`
  (`RULES.md` #11); filename `{safe_title}_<sha256-8>.txt`; download batch ≤50;
  explicit-title extras (`WIKI_TITLE_EXTRAS`) merge into the same queue;
  immutability-guard scope (only `*.txt` + `.meta.json` are wiki source).
- **Isolation rule (absolute)**: any test touching `data/wiki/.meta.json`,
  `data/cdn`, or `data/derived/*` must use tmp dirs and patch the paths —
  real source is read-only (what `conftest.py` enforces). Never let a test or
  a fix write the real meta/documents.

### L8 — Full offline pipeline integrity (`pgrag validate`)

- **Tests**: `test_validation.py`
- **Source**: `src/pgrag/validation.py`
- **Contracts**: `validate_all` runs four layered, server-free checks with a
  **warn vs. fail split**. Hard fail (1): corruption a valid atomic build
  never emits — unreadable/non-list/empty `documents.json`, missing/empty
  `id`, duplicate ids, unknown `metadata.source`, missing `metadata.table`,
  empty `text` — plus the index layer delegated to
  `vectorstore.health_check` (its failure propagates; actionable in-place).
  Warnings (0, with a remediation pointer): upstream transient states whose
  only remedy is another command — missing/empty sources (CDN JSON, wiki
  dumps), a missing `documents.json`, a stale `DOCUMENTS_VERSION`
  (regenerated-but-not-indexed; **must not gate** or it fires the mise
  rebuild fallback into a refusing `build-index`), and wiki `.meta.json`
  drift (tracked-missing / orphaned files, reported not deleted — cleanup
  stays owned by `download-wiki`). `.meta.json` is strictly read-only here.
  Deterministic, no servers.
- **Change ⇒** `uv run pytest tests/test_validation.py tests/test_health_check.py`,
  then `uv run pgrag validate`.


### L-agentic — SQLite store + agentic tool loop

- **Tests**: `test_sqlstore_build.py`, `test_agentic_tools.py`,
  `test_session_ingest.py`, `test_agentic_loop.py`, `test_agentic_tune.py`,
  `test_glogger_ingest.py`, `test_preflight_sync.py`
- **Source**: `src/pgrag/agentic/` (`store.py`, `session.py`, `tools.py`,
  `loop.py`, `glogger.py`), `scripts/build_sqlstore.py`,
  `scripts/agentic_chat.py`, `scripts/agentic_tune.py`
- **Contracts**: `build_store` is idempotent (INSERT OR REPLACE + per-file
  manifest mtimes in `data/sqlite_state.json`) and builds from tmp fixture
  sources in tests (never real `data/`); ingredients without `ItemCode`
  keep `ItemKeys`/`Desc` (NULL item_code is valid); wiki titles come only
  from `.meta.json` (never hashed filenames); Lint_* keyword markers stay in
  `raw`, filtered out of the joined keyword columns; session parsers are
  tolerant (unmatched lines are counted and skipped, never fatal) and
  re-ingest of a changed file replaces its rows exactly (no dupes); missing
  session dir skips those tables and the build still succeeds.
  `agentic_chat._preflight_sync` refreshes session + glogger data before any
  chat (`--no-sync` skips; the eval path never syncs), prints only tables
  with nonzero row counts (watermark/snapshot metadata keys filtered), and
  degrades to a printed note instead of raising when a source is unreadable
  — a glogger failure after a successful session refresh reports both
  facts rather than claiming the store is untouched.
  `ingest_glogger` WAL-safe copies the glogger DB (never opens the live
  file), mirrors event tables incrementally by AUTOINCREMENT id watermark
  (monotonic even across source deletes), re-copies per-snapshot tables when
  the latest `character_snapshots.id` advances, skips glogger's own CDN
  mirrors (ours is fresher), and is a no-op (not an error) when the source
  is absent. Rerun with unchanged sources reports 0 rows for every table
  (idempotent counts). `execute_tool` returns errors as strings (model self-corrects), allows
  only single read-only SELECTs (write/PRAGMA rejected, LIMIT 200 injected,
  2M-op progress-handler budget), and caps rows/cells/pages. `run_loop`
  mirrors pipeline.ask()'s 5-key return shape, executes at most
  `max_rounds` tool rounds (native tool_calls OR fenced ```tool blocks),
  caps each tool-result payload at 12000 chars, and forces a final no-tools
  answer (`tools=None`) once the round budget is exhausted. Tests
  monkeypatch `loop._post` — never a live LLM.
  `scripts/agentic_tune.py` sweeps (model, temperature, seed, reasoning
  budget, max-rounds) into `data/agentic_tune/<run-id>.json`
  (skip-if-exists, `--force` re-runs; `--compare` tabulates). Parity: the
  `golden-short` eval set ALWAYS runs temperature 0 / seed 0 — the harness
  pins it regardless of CLI args. Tuning cases live in
  `data/eval/tune_cases.json` (never `data/golden/` — that set is run by
  the shared `ask()` harness). Golden fact variants may gain a variant only
  when the fact is observably present in the answer in different wording
  (e.g. blacksmithing-leveling-25-30 accepts "750 xp" = reward_xp_first
  quoted from SQL); the assertion set must not shrink.
- **Change ⇒** `uv run pytest tests/test_sqlstore_build.py
  tests/test_agentic_tools.py tests/test_session_ingest.py
  tests/test_agentic_loop.py tests/test_agentic_tune.py
  tests/test_glogger_ingest.py tests/test_preflight_sync.py` (+ the file you
  edited), then `mise lint`.
  Any change to tool behavior also wants an end-to-end
  `mise agentic-chat --question ...` smoke (needs LLM :8080 only).

### L9 — Lint gate (`ruff`)

- **Tests**: `test_lint.py`
- **Source**: `ruff.toml` (repo root — the single rule source), `mise.toml`
  `[tasks.lint]/[tasks.fmt]`, `pyproject.toml` `[dependency-groups].dev`
  (ruff pinned there, not in runtime deps)
- **Contracts**: `uv run ruff check src scripts tests` returns 0 — any new
  finding in the linted trees fails `mise test`. `ruff.toml` `extend-exclude`
  (`data/`, caches) plus per-file-ignores (`scripts/**`:
  BLE001/PLW1510/S110/S112/DTZ005/RUF059/F841; `scripts/pg_rag.py`: E402;
  `tests/**`: BLE001/RUF059) are deliberate, not drift. Re-export lines
  (`chunking.py` MAX_EMBED_CHARS) use `as`-form so F401 fix doesn't strip
  them.
- **Change ⇒** `uv run pytest tests/test_lint.py -q`; adding a rule =
  clean the new findings in the same commit (never raise the gate then
  violate it elsewhere). Format-only work is `mise fmt` — the gate does not
  check formatting.
- This layer replaces **none**: it guards hygiene across all layers; the
  functional layers above remain the authority on behavior.

## Hard rules (no exceptions)

---

## Hard rules (no exceptions)

1. A test that **changes an assertion** is a **contract change** — state the
   new contract in the test and run the layer's sibling suite (L1–L7 above).
2. A test that **fails without a contract change** is a **regression in
   source** — fix the source, not the test.
3. Never hand-edit a build artifact to satisfy a test (`documents.json`,
   `bm25_index.pkl`, `wiki_parsed.json`, `data/derived/*`, real
   `data/wiki/.meta.json`). Change the generator (RULES.md #2).
4. Never change the embedding model or its dimension — `EMBEDDING_DIM=384` is
   a fixed contract with the Chroma collection (RULES.md #10).
5. A retrieval change is not done until the **L3 + L4** suite passes.

## Writing a good test (for NEW tests)

- One observable contract per test; a deterministic, isolated, full-suite-safe
  case. Test the contract, not plumbing, source text, or incidental defaults.
- **Module docstring**: one line stating the file's single contract (the
  layer it defends) — a fresh model reads that before the test bodies.
- Name tests as `test_<behavior that can regress>`; assert meaning, not
  implementation.
- Temp-dir isolation for anything touching `data/` (never real meta/docs).