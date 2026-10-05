# REVIEW — pipeline, tests, and documentation audit (verified v2)

Date: 2026-08-21 · Revised after line-level verification (see "Correction").

Companion artifact: `docs/TEST_CONTRACTS.md` (contract map + triage protocol).
Deliverable that drove this pass: the regression-triage protocol and
shared-function facet map in `docs/TEST_CONTRACTS.md`, plus `AGENTS.md`,
`.omp/RULES.md`, `.omp/WATCHDOG.md` updates.

## Correction (v1 → v2) — reading beats summarizing

The first version of this review shipped two headline findings sourced from
agent summaries that were not verified against the test files:

1. "`test_bm25.py` and `test_retrieval_unit.py` both own five identical BM25
   tests" — **false.** A full raw read shows the five BM25 tests live only in
   `test_bm25.py`; `test_retrieval_unit.py` contains `retrieve()` filter tests
   and `ask()` citation tests, no BM25 duplicates. An exact-name scan of all
   `def test_*` across the 54 files finds **exactly one** name collision in
   the whole suite (`test_summary_shape`, in `test_summaries.py` and
   `test_gathering_summaries.py`).
2. "The `DOCUMENTS_VERSION` refusal has no direct test" — **false.**
   `test_build_index.py::test_documents_version_refuses_stale` and
   `::test_documents_version_refuses_missing_marker` directly assert
   `load_documents()` raises `ValueError` on stale/missing markers.

Both errors came in because the finding matched the expected narrative (the
user reported the model makes regression errors → a review "expects" to find
duplication and uncovered guardrails → the summaries were accepted
uncritically). That is the same failure mode the engagement exists to stop,
and it validated the core recommendation before any code was touched: **facts
about this repo must be read from source lines, not inferred from summaries
that fit a prior.** No test was deleted or altered on a false premise, and the
brittle "assert DOCUMENTS_VERSION == 4" test proposed in v1 was dropped (it
would break on the next legitimate version bump).

## Findings (verified)

### Tests — genuinely clean; the redundancy concern was over-stated

The suite is better than the original audit claimed:
- **No duplicate BM25 tests, and the freshness guard is tested.** Both
  P0 candidates from v1 dissolve on verification.
- **One true name collision**: `test_summary_shape` appears in
  `test_summaries.py` (asserts `build_summary_documents` shape) and
  `test_gathering_summaries.py` (asserts `build_gathering_summaries` shape).
  Same contract name, two different builders. Fixed in this pass by renaming
  to `test_cdn_summary_shape` / `test_gathering_summary_shape` and adding
  per-file module docstrings naming each file's builder.
- **Real structural pattern: shared-function facet coverage.** One function is
  asserted across several files, each a distinct facet — this is normal and
  healthy layered coverage, but it is the load-bearing discoverability gap:
  - `retrieve()` — `test_bm25.py` (hybrid routing, comparison/general
    `n_results`), `test_retrieval_unit.py` (where-filters, default, citation),
    `test_where_filter.py` (operator + token post-fusion), `test_retrieval_trace.py`
    (trace fields), `test_retrieval_eval.py` (query-type pass-through).
  - `_hybrid_fuse` — `test_bm25.py` (RRF intersection, multiplier, bm25-only
    doc), `test_rerank.py` (tsys chunk/base caps, origin forms).
  A model editing `retrieve()` or `_hybrid_fuse` cannot learn the full
  constraint set from one file; nothing previously said so. This is now
  documented in `docs/TEST_CONTRACTS.md` L3.
- **Isolation and coverage remain strong**: session immutability guard
  (`conftest.py`), tmp-dir patches for wiki/derived paths, mocked servers,
  keys-only shape asserts, deterministic data. The rerank trio
  (`test_rerank.py` / `_client` / `_fallback`), L6 eval, and L7 loader suites
  are each non-redundant internally.

### Documentation & comments

- `AGENTS.md`, `.omp/RULES.md`, `.omp/WATCHDOG.md`, four skills: substantive,
  consistent, and unusual for the repo type. The advisor (second model) is
  enabled in `.omp/config.yml`.
- Real gap this pass closed: watchlist/rule text referenced unverified
  specifics ("duplicate BM25 tests", "unguarded version refusal"). Those are
  corrected to the verified `retrieve()`/`_hybrid_fuse` facet map and the
  tested version-refusal path.
- **`scripts/` is operational documentation** (verified line-level this pass):
  `golden_check.py` parses `{id, question, type, facts}` with
  any-variant-substring pass and `evaluation/queries.jsonl` fields exactly
  match their documented contracts; `rag_chat.py` (port 7860, embed:8081 +
  LLM:8080, one-shot) matches. Drift fixed: `pg_rag.py`'s `PG_ROOT` is
  `PG_RAG_ROOT` env-or-default, not "hardcoded" (AGENTS.md corrected).
  Completeness debt: `scripts/curator.py::create_curated_from_fragments` is
  self-labeled "simplified version - in production, would use LLM" — a stub
  generator shipping as curation; AGENTS.md does not disclose it (backlog).
- Comment quality on core symbols is mixed: `retriever.py`/`resolve.py` have
  strong inline WHY comments, but several load-bearing functions
  (`retriever._hybrid_fuse`, `retriever._rerank`, `build.py.generate_documents`,
  `build_index.build_index`, `spelling.correct_query`) lack docstrings, so a
  fresh model must grep to find the rationale. No TODO/FIXME debt markers exist
  anywhere in `src/`/`scripts/`.

## Backlog (re-scoped to verified reality)

### P0 — already landed this pass
1. **Corrected the review's false findings** in `docs/TEST_CONTRACTS.md`,
   `docs/REVIEW.md`, `AGENTS.md`, `.omp/RULES.md`, `.omp/WATCHDOG.md` — the
   guardrails now encode the verified test layout, not a plausible-sounding one.
2. **Disambiguated the one true test-name collision** (`test_summary_shape` →
   `test_cdn_summary_shape` / `test_gathering_summary_shape`) and added
   builder-owning module docstrings to `test_summaries.py`,
   `test_gathering_summaries.py`, `test_documents.py`.
3. **Added the ground-truth rule** to `TEST_CONTRACTS.md` (verify by reading
   source lines before encoding a finding) — the meta-guardrail this
   engagement produced.

### P1 — document, don't delete (LANDED 2026-08-21)
4. **Docstring pass on core symbols** — done: added to `retriever._term_overlap`,
   `_rerank`, `_hybrid_fuse`, `retrieve`; `build.generate_documents`;
   `build_index.build_index`; all 23 `builder.py` build_* docstrings (each
   metadata.table claim grep-verified against source). `spelling.correct_query`
   / `_where_matches` / `_name_injection_ids` were *already* documented and left
   untouched.
5. **Module docstrings for the remaining test files** — done: 34 added across
   the suite (files already having one skipped), each derived from the file it
   heads. Verified by AST-parse + full suite: **547 passed, 14 deselected,
   0 failures** at the time (suite later grew to 597 passed / 42 skipped /
   16 deselected; see AGENTS.md "Testing & QA" for the live tally).

### P2 — coverage depth (LANDED 2026-08-21, premise-corrected)
6. **Golden-set expansion** — premise stale at write time: the set was already
   **34** (not 5; docs said 5/"expand to 20-30"). Left well-formed; counts were
   corrected then in `evaluation/SKILL.md` + `pg-rag/SKILL.md`. Balance then:
   comparison = 1/34 vs entity = 18/34 — favoring comparison additions. The
   set has since grown to **43** (20 entity / 15 general / 5 comparison / 3
   recipe); `evaluation/SKILL.md` now tracks the live balance. Grounded
   against `documents.json` at review time (facts present, no fabrication).
7. **Pipeline-set redundancy** — landed via P1.5: every `test_pipeline_*.py`
   and `test_prompts.py` now carries a docstring splitting routing
   (`test_pipeline_*`) from prompt-content (`test_prompts`), and
   `TEST_CONTRACTS.md` L5 states the seam. Rejected "factor a shared fixture"
   — refactor risk on a passing suite for no coverage gain.
8. **Curator stub** — chosen to *document, not build LLM curation*: `curator.py`
   now states its heuristic-template nature (deterministic, offline, stable
   anchors) and removes the misleading "in production, would use LLM" line;
   AGENTS.md updated. LLM curation is available but deliberately rejected —
   it would break the suite's determinism and offline-test ethos.

## Addendum (2026-08-22) — bakeoff prefix verification

The Aug 22 "prefix fairness" pass (Tier 0) set `mxbai-xsmall` to nomic's
`search_query:`/`search_document:` on the assumption its embed is
instruction-tuned. Card verification refuted that: mxbai-embed-**xsmall** is a
bare, pooled (mean) model — `Represent this sentence for searching relevant
passages: ` belongs to mxbai-embed-**large** only. Measured on the 40-query
corpus, applying nomic's two prefixes (0.7744) cost mxbai ~0.13 MRR over true
bare (0.9042). A full-list review then caught two half-applied prefixes in
`BAKEOFF_CANDIDATES`: mxbai's `doc_prefix` still carried `search_document:` —
query-bare / doc-prefixed, an intermediate state measuring 0.9250, not the
bare score — and nomic's `doc_prefix` was missing (0.9437 → restored to
0.9563 with `search_document:`).

**Fix / rule:** prefixes are per-model card facts — read them from the model's
HF card, never borrow across families, and re-audit the whole `BAKEOFF_CANDIDATES`
+
## Addendum (2026-08-22) — production embed switched to bge-small-f16
## Addendum (2026-08-22) — production embed switched to bge-small-f16

Production embed moved from mxbai-xsmall to bge-small-f16 (bakeoff winner,
0.9875). Two findings drive the record; the how lives in code, not here:
- **Live-validation found bge-small's token ceiling is hard at 512** — llama-server
  rejects anything above with HTTP 400, and no `-c`/`-b`/`-ub` sweep raises it
  (unlike mxbai's 4096 ctx). 528 live docs (0.2%; computed skill profiles to
  2071 tokens) exceed it, so raw embedding would have crashed `build-index`.
- **Code review caught the query embed path first shipped without the model's
  query prompt**, silently running the bare ~0.90 config instead of the 0.9875
  winner.

The fix — `MAX_EMBED_CHARS` clip + per-text shrink, `QUERY_PREFIX` on queries
only (docs bare), `--pooling cls` at `-c 512`, and the mandatory full re-embed
(`embedding_hash` is content-based) — is owned by `llama_embeddings.py` and
AGENTS.md, not restated here. Tradeoff: the longest 0.2% of docs are indexed
by their first ~400 tokens.

## Addendum (2026-08-22) — rechunk + reassembly retires the 400-token truncation

The tradeoff above (long docs indexed only by their first ~400 tokens) is now
resolved, not accepted. The embed-capped families — `lorebook`, `skillprofile`,
`leveling`, `summary`, `curated` — are token-budgeted at build time
(`EMBED_WINDOW_TOKENS = 400`, measured with the vendored bge tokenizer at
`src/pgrag/resources/bge_tokenizer.json`), so **no document exceeds the
embedder window**. A character-only budget could not guarantee that: bge-small
tokenizes dense game tables ~2.4 chars/token vs ~4.9 for prose, so ~59% of the
1900–2000c chunks still exceeded its hard 512-token cap (verified live, e.g.
`leveling_BuckleArtistry` 687 tok @ 1998c vs `skillprofile_Archery_chunk_12`
in-window @ 1966c). Every chunk sits under **both** windows — ≤~447 tokens
incl. overlap + specials (< 512) and ≈1960c at max prose density (< 2000).
Every produced `_chunk_` doc carries `parent_id`, and retrieval reassembles
the artifact instead of serving a fragment:

- **entity/comparison dossiers** reassemble leveling families via the
  generalized `_family_chunks` (base id + `_chunk_<n>`, chunk-ordered) — the
  hub already collected `skillprofile_*` chunks by prefix; the leveling
  include was widened from exact-match to its full family.
- **general/lookup + gap-fill** reuse `expand_parents` (parent_id), unchanged —
  the linkage now covers CDN/computed splits, not just wiki.
- **`_find_matching_summary`** expands the winning summary artifact's siblings
  so "all recipes for X" isn't answered from one fragment.
- IR metrics were already correct: `canonical_doc_id` collapses `_chunk_` on
  both sides of every treated query.

`DOCUMENTS_VERSION` 5 → 6. Verified: 259,308 docs, **0 over 512 tokens**
(embed-capped), **0 over 2000 chars** (any); re-embed touched only 1,562
changed docs with **no** shrink-fallback triggers; `validate` all-clean. Tests
520 pass (contracts updated).

**RESOLVED (this pass) — `embed_batch`'s overflow fallback is now O(k log n)
bisection.** `_embed_batch_recursive` splits the batch on `_InputTooLong`,
retrying each half at the full budget; only a single-text leaf falls through to
the per-text `_embed_one` shrink. Now bounded ~O(k log n) leaf requests, order
preserved, in-window siblings keep their full budget. Covered by
`tests/test_embed_fallback.py`. `build_index` also gained a pre-embed window
guard that fails fast on any over-window doc (chunking regression) instead of
silently feeding the fallback. Environment-hygiene note retained below: multiple
leftover `build-index` processes contended on the single embed server until
40+ timed-out clients were observed; `taskkill /F /IM python.exe` is the only
reliable Windows kill (embed runs as `llama-server.exe`).

## Verdict

The test suite is in better shape than the original review claimed: no mass
duplication, and the version-freshness refusal — the trap that keeps biting —
is already directly tested. The real, verified problems are **discoverability**
(shared functions constrained by many files, with no map) and **process**
(findings were being encoded without line-level verification). Both are fixed
in `docs/TEST_CONTRACTS.md` and the rules files. No rearchitecting needed.

## Addendum (2026-10-04) — template shells no longer delete wiki content

`strom-farblood-favor` (new golden) exposed a **systemic wiki-builder gap**:
`strip_code()` deletes the entire content of multi-arg template shells, so
`{{Spoiler|label|body}}` and `{{Quote|source|body}}` vanished whole — favor
rewards (175 NPC pages), quest reward spoilers (763 pages), preference
reveals, NPC dialogue. `{{Favor|Tier}}` also vanished, leaving "At , Strom
will reveal…" with a blank. ~953 pages lose content to this (surveyed).

Fix (wiki_builder, both parse paths): balanced-brace unwrapping of
Spoiler/Quote shells + `{{Favor|X}}` → "favor tier X" + event/storage
templates kept as key = value lines, ordered after
`_preserve_template_names` so `{{Item|X}}` inside bodies keeps display
names. `CACHE_VERSION` 8→9 (cached docs would otherwise serve old text
forever — mtime-equal pages never reparse), `DOCUMENTS_VERSION` bumped.

Follow-on corpus gaps the same golden forced (builder, `DOCUMENTS_VERSION`
bumped twice more): quest **Currency** rewards (`StatehelmRenown` etc.) were
dropped entirely — 698 quests lost their Rewards section; now render
camel-split ("+25 Statehelm Renown"). NPC **gift preferences** (CDN
`Preferences`) were never rendered — 241 NPC docs now carry them
("Like: Battle Chemistry Equipment"). Contract tests in
`test_wiki_builder.py` (3 new) + `test_documents.py` (extended quest/NPC
shape tests). `golden-one strom-farblood-favor` PASS (pipeline + agentic).

`astounding-ink-acquisition` (new golden) exposed a resolver gap: sources
data references **summonable/event NPCs absent from `npcs.json`** (138 keys
total; `NPC_VendorFox` alone appears in 26 entries). These rendered as
"Vendor from Unknown NPC (NPC_VendorFox)". Fix (`resolver.py`): verified
alias table (`_NPC_KEY_ALIASES`) for the keys with wiki-verified display
names (Vendor Fox, Pennoc, Khyrulek, Raim, Moxie, Grasuul, Riger, seasonal
Velkort) — tested in `test_resolve.py`; truly unknown keys keep the Unknown
fallback. `DOCUMENTS_VERSION` bumped again.

`royal-jelly-gathering` (new golden) exposed a routing gap: "How do I
gather/get X?" matched `ENTITY_PATTERNS` (`how do i get`) and built the item
dossier — which is spend-side (uses/recipes/gifting). Acquisition facts live
in NPC barter pages and `sources_items` docs; the dossier structurally cannot
contain them (Royal Jelly's egg row lives on the *Midge* NPC page, linked to
`item_1337` only through that NPC). `_gap_fill` doesn't rescue it: it fires
only on explicit "I don't know", and the dossier answer is confident.

Fix (classifier, `AGGREGATION_PATTERNS`): acquisition-of-an-item phrasing —
`how/where do/can i get|gather|obtain|collect|farm` — routes **general**
(hybrid recall + wiki expansion), mirroring the existing grow-planting rule
("entity is the filter, not the answer"). Travel phrasing ("How do I get to
Gazluk Keep?") keeps its route via a `get(?!\s+to)` lookahead. Asserted by
`test_acquisition_of_item_routes_general` /
`test_acquisition_does_not_hijack_travel_or_describe` (L4). Full L3+L4+L5
regression set green; `golden-one royal-jelly-gathering` PASS.