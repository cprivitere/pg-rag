---
name: evaluation
description: The pg-rag RAG evaluation harness — golden set (fact-presence LLM checks) + pre-LLM IR suite (Recall@k/MRR/NDCG/Hit@k, reranker uplift, entity accuracy), before/after workflow. Use before and after any retrieval or document-generation change that should be measured.
---

# evaluation — RAG eval harness

The point of this repo's evaluation is making agent changes measurable instead
of "this seems better."

## Golden set — `data/golden/*.json`

Each file:

```json
{
  "id": "recipes-using-spider-silk",
  "question": "What recipes can I make with Spider Silk at Nature Appreciation 25?",
  "type": "recipe",
  "facts": [
    ["Spider Silk Tunic", "Spider Silk Robe"],
    ["Nature Appreciation", "25"]
  ]
}
```

- `facts` is a list of variant groups: the answer PASSES if ANY variant in
  each group appears (normalized substring) in the LLM answer.
- 59 files exist today (23 entity / 22 general / 7 recipe / 6 comparison),
  spanning recipes-by-ingredient, level-gated crafting, item acquisition/drops,
  ability lookups, comparisons, quest requirements, wiki lore, wiki how-to
  assembly, and player-state audits. 7 store-only cases (player facts,
  `"store": true`) count in this total but are excluded from the pipeline
  harnesses — they run via the agentic eval. Future additions should favor the
  balanced categories (comparison is 6/59 today — the thinnest bucket).

## Live-session goldens (answer-then-validate workflow)

The standing session workflow: the user asks a gameplay question → research
the answer against the live store + corpus → give the answer → **pin it as a
golden so the same question stays answerable**. The golden is not optional
documentation; it is the acceptance gate.

1. **Research** against the store (`data/sqlite_gorgon.db`) and corpus. Sync
   first when the user just played: `ingest_glogger`/`ingest_session` are
   incremental (or run `mise agentic-chat`, whose preflight syncs).
2. **Answer the user.** Ground every claim in a store table or corpus doc; if
   a fact needed manual derivation (JOIN, arithmetic, name-mapping), note it
   — that is exactly the part the model may fumble.
3. **Derive fact groups** from what you verified. Every fact must exist in the
   data you actually checked (grep the store / documents.json first — an
   un-grounded fact is a permanently-failing test).
4. **Route the golden:** facts from the player's own state (sheet abilities,
   stall ledger, kills, favor) → `"store": true` (pipeline tiers skip it;
   `mise agentic-eval` picks it up automatically). Facts from CDN/wiki corpus
   → plain golden, validated with `mise golden-one -- <id>`.
5. **Validate before committing:** run the golden through its eval path
   (`mise agentic-eval` for store-only, `golden_rerun.py --id <id>` for
   corpus). A FAIL means the model can't reproduce your answer — diagnose
   (`--diagnose` for pipeline, trace_rounds for agentic) and fix the gap
   (tool, prompt note, doc builder) before committing. A golden that fails on
   commit is a known-gap trap, not a convenience.
6. **Commit** the golden together with whatever fix it forced.

### Traps learned the hard way (each cost a debug session)

- **`abilities.level_req` is not a known-gate.** Shrine/riddle unlocks
  (TameBear/TameCat) carry `level_req=1` yet may be unknown. The known list
  lives ONLY in `char_reports` sheet `Skills[<skill>].Abilities`; non-skill
  abilities (CharmRat) are bucketed under the pseudo-skill `"Unknown"` — audit
  both buckets. `player_state section=abilities` does this diff.
- **`game_state_skills` is fresher than the sheet dump for LEVELS** (log-sourced
  vs export-time); the sheet is the only source for ability LISTS. Cross-check
  both; state which timestamp each claim comes from.
- **Pipeline context is corpus-side; player-state facts can never appear in
  it.** Diagnosing a store-only golden via the pipeline gives a guaranteed
  RET-SIDE/GEN-SIDE false signal — route first, diagnose second.
- **Name mismatches across sources:** wiki "Bear Anatomy" ↔ CDN/game "Bear and
  Bugbear Anatomy"; sheet keys have no spaces (`AnimalHandling`). Normalize
  before diffing.
- **A personal-data answer usually implies a schema/tool gap.** The AH audit
  session produced `player_state section=abilities`; the stall session
  produced the stall classifier. If you needed manual SQL gymnastics to answer
  a question the model will get again, that gymnastics belongs in a tool or
  doc-builder, not in the golden alone.
- **Pricing questions have two layers; name both or the model answers the wrong one.**
 `items.value` is the CDN base value; `stall_events` holds realized prices —
 but only from the player's OWN stall (the store has no other players'
 stalls, so no market sample exists). A bare "pricing" question answered
 only from `items.value` is a routing miss: the loop's schema summary now
 routes pricing/pricing-trend questions to stall_events and
 `player_state section=stall` (added for the fat-woe-cutting-circuit
 session) lists each realized sale individually — averages erase the price
 spread and truncated windows must point aggregates back at sql_query. An
 item with zero stall history (Woe Coin) is currency: fall back to
 `sources`→`npcs`/`quests` for where it's spent (Sven the Bleeder barters).

## Running it

- `uv run python scripts/golden_check.py` — needs embed (:8081) + LLM (:8080)
  up (`mise start`). Exits 1 with the missing facts on failure.
- `tests/test_golden_check.py` parametrizes over `data/golden/*.json` at
  collection time → new goldens are AUTO-collected as offline-skipped tests
  (skip unless servers up). No test code needed per golden.
- `mise golden-type recipe` — subset by type (entity|recipe|comparison|general,
  repeatable); `--max-facts N` caps case weight. Both `golden_check.py` and
  `golden_rerun.py` accept `--type`/`--max-facts`.
- **Store-only cases** (`"store": true` in the golden JSON): facts live in the
  agentic SQLite store (glogger play history), not in the RAG corpus — the
  pipeline harnesses (golden_check/golden_rerun/-m short/-m long) skip them;
  they run via the agentic tool loop instead (`mise agentic-eval`, which
  appends them to the short tier automatically).

## Retrieval eval suite (pre-LLM IR metrics)

- `mise eval-retrieval` (alias `ev`) — full per-stage run (needs embed :8081
  + rerank :8082 up; `build-index` required first). `scripts/retrieval_eval.py`.
- `mise eval-offline` — `--offline` variant: BM25 + query classifier + entity
  hub resolution only, no server deps. Use when you only touched retrieval
  stages that don't need Chroma/rerank.
- **Dataset**: `evaluation/queries.jsonl` — hand-authored gold input (tracked).
  Each line: `id`, `query`, `query_type`, `expected_classifier`,
  `target_entities`, `relevant_ids`, `relevant_names`, `category`.
- **Output**: `evaluation/results/latest.json` (gitignored, generated).
  `--baseline` reruns against a saved snapshot for diff/regression comparison
  (`compare_benchmarks`).
- The offline entity-hub path reuses `build_entity_context_offline`, so it
  measures the same wiki-linked dossier the live pipeline builds.
- Metrics count **canonical retrievable units**, not raw index docs: a wiki
  table's `_coverage` + `_row_<n>` docs and any `_chunk_<n>` split collapse to
  one unit (applied to both relevant and ranked, deduped). Without this the
  per-row explosion deflates recall@k and chunked ranked docs never match base
  relevant ids.
- **Comparison queries** (`query_type == "comparison"`, ≥2 entities) are scored
  on the multi-entity dossier production feeds the LLM
  (`build_multi_entity_context`), not the single-hub rerank window — both
  subjects must be present in the scored context.
- Retrieval surfaces terse-stub lookup docs (e.g. ability/area stubs) by
  **exact entity-name injection + promotion**: the bounded longest multi-token
  span that resolves to a known entity name is injected into the query and the
  matching explicit-id doc is promoted, so a stub that never appears via dense
  embedding still ranks. Fragments that only partially match are excluded.
- Add queries to `evaluation/queries.jsonl` to cover a regression case; prefer
  a new per-stage assertion in `tests/test_retrieval_eval.py` for pure metric
  or well-formedness guarantees.

## Embed-model bakeoff (`embed_eval.py`)

- `mise bakeoff` (alias `bk`) — `uv run python scripts/embed_eval.py --bakeoff`:
  ranks all `BAKEOFF_CANDIDATES` on the persisted `data/bakeoff_corpus.json`
  (40-query needle/fill/paraphrase set, `mise bakeoff-corpus` regenerates it)
  → MRR/Recall/NDCG@10, HardMRR, per-PID VRAM. Spawns each embedder on :8085.
- **Per-candidate `query_prefix`/`doc_prefix` are card-verified (2026-08-22)
  and live only in `BAKEOFF_CANDIDATES`** — a prefix is a per-model property,
  not a runtime flag. Sources: embeddinggemma `task: search result | query: `
  + `title: none | text: `; jina-v5-retrieval `Query:`/`Document:`; nomic
  `search_query:`/`search_document:` (both required); bge-small-en-v1.5
  `Represent this sentence for searching relevant passages: ` on queries
  only (docs bare); bge-m3 / MiniLM-L6 / mxbai-xsmall embed bare. When adding
  or changing a candidate, verify the prompt against the model's HF card — a
  prefix borrowed from another family is a confound (`mxbai-xsmall`'s bare is
  its card behavior, not an oversight). **Production embed is bge-small-f16**
  (wired 2026-08-22): `Represent this sentence...` query prefix, docs bare,
  pooling `cls`, hard 512-token server cap (input chars clipped to `llama_embeddings.MAX_EMBED_CHARS` = 2000).

## Two layers of measurement

1. **LLM end-to-end** — fact presence (subset recall of stated facts) over
   `data/golden/*.json`. Cheapest signal that the answer is right.
2. **Pre-LLM IR suite** (`src/pgrag/rag/retrieval_eval.py`) — per-stage
   ranking metrics over `evaluation/queries.jsonl`, so a regression can be
   pinned to the exact stage (BM25, dense, hybrid RRF, rerank, entity hub)
   instead of "LLM said something wrong":
   - Recall@k, MRR, NDCG@k, Hit@k
   - Reranker Uplift (Δ NDCG@5 rerank vs hybrid)
   - Entity accuracy (Jaccard / strict match on entity linkage)

## Before/after workflow

1. Pick a focused query set (or write a new golden for the failing case).
2. Run `golden_check.py` / targeted tests → record baseline.
3. Make the change (one stage).
4. Re-run — compare pass/fail AND inspect which docs ranked (scripts/retrieval.py).
5. If the "fix" only moved a ranking problem downstream, go back to stage
   localization (`retrieval` skill).

## Planned direction

- Golden set is **at 59 and growing** via live-session goldens (see the
  workflow above) — new questions from play sessions are the primary intake;
  comparison is the thinnest bucket (6/59). Beyond session intake, capture a
  *named regression case*,
  e.g.:
  - `grow-field-mushrooms` ("How do I grow Field Mushrooms?") — fails before
    wiki page expansion, passes after (already present).
  - a `recipes-using-spider-silk`-style metadata/keyword case — fails before
    recipe metadata enrichment, passes after (covered by the
    `recipes-using-basic-spider-silk` family).
- When adding a golden: ground every fact in the corpus first (`grep
  data/documents.json`) so it is actually retrievable — an un-grounded fact
  is a permanently-failing test.
- Extend `evaluation/queries.jsonl` (48 cases today) across the same
  categories, and promote benchmark snapshots into regression thresholds.