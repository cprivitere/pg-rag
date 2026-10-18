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

**Scope note (2026-10-09 cutover):** the dense retrieval path was deleted —
embeddings, Chroma, the reranker, RRF fusion, the query classifier, the
synthesis/pipeline layer, and the pre-LLM IR-metric harness. The layers below
are what survives; the old retrieval-regression set (L3/L4 in previous
revisions) no longer exists. Every test file in `tests/` is covered by exactly
one layer below (`scripts/check_docs.py` asserts that each is *named
somewhere*; layers repeat their files in the Change ⇒ line, so a name is not
unique by construction).

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
3. **Wrong-layer check.** If your next edit targets the store's `documents`
   rows, `data/derived/wiki_parsed.json`, `data/derived/*`, or a real
   `data/wiki/.meta.json` — **stop**. Those are build outputs
   (RULES.md #2). Fix the builder/loader that produces them. A failing test
   that reads an artifact points at its generator, not the artifact.
4. **Contract-change hard rules** (any edit that changes what a test
   *asserts*):
   - State the new contract in the test (a module/function docstring or a
     `# contract: …` comment) so the intent survives.
   - Run the **full sibling suite** for that layer (the column below), not
     just the test you edited — the failure often implies a sibling also
     asserts the old contract.
   - Do not weaken an assertion to dodge a real regression. If the old
     assertion was wrong, say why in the comment.
5. **Duplication watch.** The map marks contracts owned by **two or more
   files**. If you fix one, check/consolidate the others — otherwise the
   duplicate re-blooms as an unexplained failure.

Never edit a test "to make it pass." A test edit is either fixing a broken
test (prove the source is fine) or recording a deliberate contract change
(state it, run the sibling suite).

---

## The map

"Change ⇒ run" is the minimum command set after touching that layer.

### L1 — Document generation (builders)

- **Tests**: `test_documents.py`, `test_chunking.py`, `test_skill_profiles.py`,
  `test_summaries.py`, `test_gathering_summaries.py`, `test_doc_quality.py`,
  `test_flatten.py`, `test_metadata.py`, `test_wiki_builder.py`,
  `test_leveling.py`, `test_creature_zones.py`, `test_combat_xp.py`
- **Source**: `src/pgrag/documents/` (`builder.py`, `wiki_builder.py`,
  `chunking.py`, `tokenizer.py`, `resolver.py`, `skill_profiles.py`,
  `summaries.py`, `creature_zones.py`, `combat_xp.py`) — built in-process by
  `src/pgrag/agentic/store.py::_ingest_documents` into the store's `documents`
  table (no intermediate artifact).
- **Contracts**: doc key shape `{id, type, text, metadata}`; metadata
  scalar-safety (add-only, scalar-only, multi-value `" | "`-delimited —
  `test_doc_quality.py`); chunk max/overlap + `_chunk_N` ids + `parent_id`
  reassembly links; summary + gathering grouping/ranking; resolver
  cross-refs; leveling/combat-xp doc shapes.
  `creature_zones.py` (`build_creature_zones_documents`): one
  `creature_<Name>` wiki doc per MOB page with ≥1 zone, `table=creatures`,
  `metadata.creature_type`/`locations`; zone order = MOB `area=` then zone
  categories, deduped; lead description stripped of all `{{`/`}}` wiki
  residue. `build_leveling_documents` emits `type="leveling"`.
  `wiki_builder.py`: `strip_code()` must not delete content of multi-arg
  template shells — `{{Spoiler|…}}`/`{{Quote|…}}` unwrap (keep the body),
  `{{Favor|X}}` renders as "favor tier X", event/storage templates keep
  `key = value` lines; `{{NPC training row}}`/`{{NPC training unlock row}}`
  render one line per row (`Unlock: <Skill>: <what> - skill level N - cost C
  - favor F` / `Teaches: <ability> - …`) — they are the only carrier of trainer
  prices, and dropping them left 209 training sections as an empty table shell
  with no unlock cost anywhere in the corpus; quest **Currency** rewards and
  NPC **Preferences** render; `_build_entity_index` falls back to the record
  key for the `skills` table (`_KEY_IS_NAME_TABLES`) so key-only CDN records
  still link their wiki page. `_prepend_barter_options` (`builder.py`): item docs whose CDN
  `sources_items` entry is `type=Barter` get the NPC page's barter-table trade
  lines **prepended** (chunk-tail placement once ranked it low).
- **Change ⇒** `uv run pytest tests/test_documents.py tests/test_chunking.py
  tests/test_metadata.py tests/test_creature_zones.py tests/test_doc_quality.py
  tests/test_wiki_builder.py` (+ the file you edited). Then inspect
- **Version coupling**: changing document **shape** means bumping
  `DOCUMENTS_VERSION` in `src/pgrag/config.py` (see L2). Bumping the shape
  without a bump here ships rows rendered by an older generator with no
  version witness.
- **Duplication watch**: `build_summary_documents` is defended by *three*
  files — `test_documents.py`, `test_summaries.py`,
  `test_gathering_summaries.py`. Their module docstrings name which function
  each owns; if you edit summary generation, run all three.

### L2 — Generated corpus in the store (`documents` + `documents_fts`)

- **Source**: `src/pgrag/agentic/store.py` (`_ingest_documents`, the
  `documents` table + `documents_fts` external-content FTS5 index),
  `src/pgrag/agentic/tools.py` (`_corpus_search`, `_fts_query`,
  `_QUERY_STOPWORDS`, `_TOOL_FILTER_TABLES`)
- **Contracts**: the store build runs `build_documents(db)` in-process and
  inserts every doc (`id, type, text, source, tbl, name, keywords`) stamped
  with `DOCUMENTS_VERSION` — no intermediate artifact, no staleness refusal.
  `corpus_search` is one FTS5 query (`bm25()`-ranked, OR-terms with prefix
  matching); the hit line keeps its shape (`id | name (table) | snippet`,
  capped at `_SEARCH_SNIPPET`); `corpus='tool'` filters the SQL-table
  renderings (`_TOOL_FILTER_TABLES`); `tools._QUERY_STOPWORDS` filters
  function words query-side only (the FTS index keeps every token), and an
  all-stopword query keeps its terms rather than matching nothing. Owned by
  L3's `test_agentic_tools.py` (the `corpus_search` FTS facet).
- **Change ⇒** `uv run pytest tests/test_agentic_tools.py`, then
  `mise sql-store` and inspect `corpus_search --corpus full|tool`.

### L3 — Agentic tool loop + tuning

- **Tests**: `test_agentic_tools.py`, `test_agentic_loop.py`,
  `test_agentic_tune.py`
- **Source**: `src/pgrag/agentic/tools.py` (`execute_tool`), `loop.py`
  (`run_loop`, `_post`, `_parse_tool_blocks`, `_cap`), `eval.py`
  (`run_case`/`run_eval`), `scripts/agentic_tune.py`
- **Contracts**: the five tools (`sql_query, find_entities, get_page,
  corpus_search, player_state`) and the `AVAILABLE_TOOLS` string; `execute_tool`
  returns errors as strings (model self-corrects) — and it allows only single
  read-only SELECTs (write/PRAGMA rejected, LIMIT 200 injected, 2M-op
  progress-handler budget), and caps rows/cells/pages. `player_state
  section=stall` reports the player's OWN stall ledger only: each realized
  sale individually with buyer and per-unit price (never averaged), sold
  separated from asking (`visible`/`configured`), plus the queried item's CDN
  base value (`items.value`) and `sources` rows — an item with no stall pricing
  falls back to base value + sources instead of dead-ending. Price gathering
  also ATTEMPTS `chat_events` best-effort: social-channel rows (Trade/Global/
  Nearby/Help/Tell/Guild/Party) mentioning the queried item and carrying a
  price token (number + c/k/g/gold/each/per/ppu, or `@<number>`) are appended
  as dated raw mentions under a `CHAT PRICE MENTIONS … NOT realized sales`
  header; item-naming chatter, noisy channels and unit-less numbers are
  dropped, and no mentions ⇒ no block. Item-name lookups in this section are
  singular-aware (`_item_name_like`): a plural query ("Phoenix Eggs") also
  matches the singular CDN name ("Phoenix Egg") — before that, a plural query
  hid the realized sale and reported "no events" — with the shortest matching
  name winning so a "Work Order for …" row never shadows the item itself.
  The section also prints a **work-order block** when the item is a Collect
  objective of a work-order quest (`quests.objectives` × `json_each` +
  `rewards` containing `WorkOrderCurrency`): fixed councils ÷ required count =
  the guaranteed per-unit floor, plus the Industry gate from the
  `Work Order for X` scroll; items with no work order print no block.
  `run_loop` returns the 5-key shape `{answer, documents, sources, rounds,
  trace_rounds}` (plus legacy `query_type`/`rerank_used` in every return arm),
  executes at most `max_rounds` tool rounds (native tool_calls
  OR two text forms: fenced ```` ```tool ```` JSON, and the Qwen template's
  `<tool_call><function=…><parameter=…>` XML — three counting native), caps each tool-result payload at
  12000 chars (row-aware for tables), and forces a final no-tools answer once
  the budget is exhausted. Tests monkeypatch `loop._post` — never a live LLM.
  `loop._post` reads its transport config at CALL time: `PGRAG_LLM_URL`,
  `PGRAG_LLM_MODEL`, `PGRAG_LLM_NATIVE_TOOLS`. `agentic_tune.py` sweeps (model, temperature,
  seed, reasoning budget, max-rounds) into `data/agentic_tune/<run-id>.json`
  (skip-if-exists, `--force`, `--compare`).
- **Change ⇒** `uv run pytest tests/test_agentic_tools.py
  tests/test_agentic_loop.py tests/test_agentic_tune.py` (+ the file you
  edited), then `mise lint`. A tool-behavior change also wants an end-to-end
  `mise agentic-chat` smoke (needs LLM :8080 only).
- **Shared-function facet coverage** (read before editing the function): a
  single function is asserted across files, each testing a *different facet*;
  no file is the sole owner.
  - `run_loop` — `test_agentic_loop.py` (round protocol, tool parsing,
    forced-final, payload cap), `test_agentic_tune.py` (sweep/compare over
    `eval.run_case`), `test_golden_check.py` (the offline golden gate drives
    `eval.run_case` → `run_loop`).
  - `execute_tool` — `test_agentic_tools.py` owns it; `test_agentic_loop.py`
    exercises it through `_execute`.

### L4 — SQLite store + source ingestion

- **Tests**: `test_sqlstore_build.py`, `test_session_ingest.py`,
  `test_glogger_ingest.py`, `test_preflight_sync.py`
- **Source**: `src/pgrag/agentic/` (`store.py`, `session.py`, `glogger.py`),
  `scripts/build_sqlstore.py`, `scripts/agentic_chat.py`
- **Contracts**: `build_store` is idempotent (INSERT OR REPLACE + per-file
  manifest mtimes in `data/sqlite_state.json`) and builds from tmp fixture
  sources in tests (never real `data/`); ingredients without `ItemCode` keep
  `ItemKeys`/`Desc` (NULL item_code is valid); wiki titles come only from
  `.meta.json` (never hashed filenames); Lint_* keyword markers stay in `raw`,
  filtered out of the joined keyword columns; session parsers are tolerant
  (unmatched lines are counted and skipped, never fatal) and re-ingest of a
  changed file replaces its rows exactly (no dupes); missing session dir skips
  those tables and the build still succeeds.
  `ingest_glogger` WAL-safe copies the glogger DB (never opens the live file),
  mirrors event tables incrementally by AUTOINCREMENT id watermark, re-copies
  per-snapshot tables when the latest `character_snapshots.id` advances, skips
  glogger's own CDN mirrors, and is a no-op (not an error) when the source is
  absent. `agentic_chat._preflight_sync` refreshes session + glogger data before
  any chat (`--no-sync` skips), prints only tables with nonzero row counts, and
  degrades to a printed note instead of raising when a source is unreadable.
  (`scripts/publish_store.py` + `tests/test_publish_store.py` and the
  `mise upload-store` / `upload-docs` tasks were retired with the molab
  notebook — no publish path ships.)
- **Change ⇒** `uv run pytest tests/test_sqlstore_build.py
  tests/test_session_ingest.py tests/test_glogger_ingest.py
  tests/test_preflight_sync.py` (+ the file you
  edited), then `mise sql-store` and `mise lint`.
- **Fixture hazard**: the `build_store` defaults are LIVE sources (real game
  dir, real glogger DB) — a fixture that omits `session_dir=`/`glogger_db=`
  tmp paths ingests live data and races it.

### L5 — Loaders, wiki sync, curation, source guards

- **Tests**: `test_wiki_loader.py`, `test_download_wiki.py`,
  `test_data_immutability.py`, `test_curator.py`, `test_curator_scheduler.py`;
  plus `tests/conftest.py`' **session-scoped immutability guard** (snapshots
  `data/cdn` + `data/wiki` at session start, asserts unchanged at teardown).
- **Source**: `src/pgrag/loaders/`, `scripts/curator.py`,
  `scripts/curator_scheduler.py`
- **Contracts**: wiki display names come only from `data/wiki/.meta.json`
  (`RULES.md` #11); filename `{safe_title}_<sha256-8>.txt`; download batch ≤50;
  atomic metadata saves + stale/orphan purge; the curator regenerates stale
  docs on source change and the scheduler orders state persistence after the
  store rebuild (curator → `scripts/build_sqlstore.py`); immutability-guard
  scope (only `*.txt` + `.meta.json` are wiki source).
- **Isolation rule (absolute)**: any test touching `data/wiki/.meta.json`,
  `data/cdn`, or `data/derived/*` must use tmp dirs and patch the paths — real
  source is read-only (what `conftest.py` enforces). Never let a test or a fix
  write the real meta or the real store.

### L6 — Golden gate (`data/golden/*.json` through the loop)

- **Tests**: `test_golden_check.py`, `test_golden_rerun.py`
- **Source**: `scripts/golden_check.py`, `scripts/golden_rerun.py`,
  `src/pgrag/agentic/eval.py` (normalization, tier membership, retry-once,
  report assembly)
- **Contracts**: golden shape `{id, question, type, facts: [[variants…]],
  xfail?: bool, store?: bool}` — `xfail: true` marks a known-gap probe
  (runner+test treat a persistent miss as expected, but a pass is an XPASS
  forcing unflagging); `"store": true` marks a case whose facts live in the
  SQLite store (glogger play history) — the loop reads the store, so it runs
  through the same path with no separate harness. Fact specs are *variant
  lists* so inflections of the same phrase count as one fact: add a variant,
  never delete one, when the corpus wording legitimately differs. The pytest
  gate parametrizes over every file at collection time and skips unless the LLM
  (:8080) is up (**LLM only**) — `-m short` selects the 8-case
  `SHORT_TIER_IDS`; the default `not slow` marker deselects the long tier.
  retry-once for reasoning-ON noise; both attempts failing = regression.
  `SHORT_TIER_IDS`/`GOLDEN_DIR` are single-sourced in
  `pgrag.agentic.eval` (asserted by `scripts/check_docs.py`).
- **Change ⇒** `uv run pytest tests/test_golden_check.py -m short` (offline
  collection + skip shape, LLM-less) and `uv run pytest
  tests/test_golden_rerun.py` (the `audit_rows`/`flaky_ids` selection filters —
  these tests are unmarked, so they must NOT be on the `-m short` command,
  which would deselect all of them), then a live `mise golden-short` for the
  actual facts.
  **Per-case cost is dominated by the reasoning model's rounds** — a
  store-only case answers in tens of seconds; a 5-6 round wiki case takes
  minutes; the full 62-case set is a long background run, not a foreground
  watch. Use `mise golden-one <id>` while iterating.
- **Golden-rerun tooling** (`scripts/golden_rerun.py`, `mise golden-one`/
  `golden-flaky`/`golden-flaky-list`): rerun named case(s); every run appends
  one `{id, missing, missing_facts, query_type, attempt_n, ts}` record to
  `data/golden/history.jsonl`; `flaky` selects ids whose recent-10 miss-share
  exceeds 50% (plus manual entries in `data/golden/FLAKY.txt`); `list` audits
  shares. `history.jsonl` is an accumulating ledger, **not a build output** —
  its tests use tmp paths only. Note: the classic harness's offline rerun and
  xfail-gate tests were deleted with the dense path: the run/record path is
  exercised via the CLI, while the audit/selection filters (`audit_rows`,
  `flaky_ids`) are unit-tested offline in `test_golden_rerun.py`.

### L7 — LLM client

- **Tests**: `test_llm.py`, `test_connectivity.py`
- **Source**: `src/pgrag/rag/llm.py`
- **Contracts**: SSE streaming token parse; server-error handling
  (unreachable/timeout raise domain errors carrying the URL) — offline tests
  assert these types, never a live server.
- **Change ⇒** `uv run pytest tests/test_llm.py tests/test_connectivity.py`.

### L8 — Lint gate (`ruff`) + TypeScript parse gate

- **Tests**: `test_lint.py`
- **Source**: `ruff.toml` (repo root — the single rule source), `mise.toml`
  `[tasks.lint]/[tasks.format]`, `pyproject.toml` `[dependency-groups].dev`
  (ruff pinned there, not in runtime deps); for TypeScript: `mise.toml`
  `[tools] node` + `[tasks.lint-ts]/[tasks.ts-deps]`, `scripts/check_ts.mjs`,
  and `package.json` (the `typescript` devDependency, installed into gitignored
  `node_modules/`)
- **Contracts**: `uv run ruff check src scripts tests` returns 0 — any new
  finding in the linted trees fails `mise test`. `ruff.toml` `extend-exclude`
  (`data/`, caches) plus the per-file-ignores it carries (`scripts/**`:
  RUF059/F841; `tests/**`: RUF059) are deliberate, not drift — and every
  suppression must name a code `lint.select` actually carries, resolved inside
  the code's linter family (`B` covers `B008`, never `BLE001`): a suppression for
  a family that is *off* suppresses nothing while looking like a safeguard.
  L11 §8 enforces that. And `mise lint-ts`
  (`node scripts/check_ts.mjs`) must parse the repo's script-class JS
  (`scripts/*.mjs`, covered by the gate itself) clean — every diagnostic
  fails, including a planted syntax error, which the layer self-tests. No
  node / no `node_modules` (a checkout that never ran `mise install` +
  `mise ts-deps`) ⇒ the two TS tests skip loudly (developer tooling, not a
  runtime dep of this repo).
- **Change ⇒** `uv run pytest tests/test_lint.py -q`; adding a rule = clean the
  new findings in the same commit (never raise the gate then violate it
  elsewhere). Format-only work is `mise format` — the gate does not check
  formatting.
- This layer replaces **none**: it guards hygiene across all layers; the
  functional layers above remain the authority on behavior.

### L9 — Compliance gate (Project Gorgon TOS/CoC hard lines)

- **Tests**: `test_constraints.py`
- **Source**: `scripts/check_constraints.py` (the scanner + its four pattern
  categories and glob set); policy owner
  `docs/research/pg-rag-game-interaction-limits.md` (§2 patterns, §6 wiring, §7
  change-control); runners `.githooks/pre-push` and `mise compliance` (which
  `mise test` runs before pytest).
- **Contracts**: the scanner is a **test gate, not just a push hook**, and four
  invariants are asserted offline: (1) the working tree scans clean — a
  prohibited pattern in `src`/`scripts`/`tests`/`docs`/`.agents` or the root
  manifests fails `uv run pytest`; (2) the candidate set really covers those
  trees and excludes exactly the two documented self-exclusions (the checker
  itself, the policy doc); (3) a planted violation in every scanned family is
  reported with its path; (4) `PG_SKIP_CONSTRAINTS` exits 0 *with* the warning
  (never silent). The probe fixtures are assembled from fragments, because this
  file is itself scanned for the patterns it plants.
- **Change ⇒** `uv run pytest tests/test_constraints.py`, then `mise
  compliance`. Never weaken the scanner or the policy doc to pass a violation —
  §7 says a prohibited category is argued to Elder Game, never to this repo.
- This layer replaces **none**: it enforces the policy across every other layer.

### L10 — Service control (`scripts/services.py`)

- **Tests**: `test_services.py`
- **Source**: `scripts/services.py`; consumers `mise.toml` (`llm-start`/`llm-stop`/`chat-start`/
  `chat-stop`/`start-all`/`stop-all`/`status`/`debug-llm`), `scripts/agentic_tune.py` (stops/starts via
  `mise run llm-stop`/`llm-start`, and builds its `--llm-restart` server argv with
  `services.llm_argv` — the two contender rows carry their own model/flags, while the `ornith` row
  re-reads `[env]`, so no production literal lives in that registry)
- **Contracts**: the LLM argv carries `LLM_MODEL` + `LLM_FLAGS` from `[env]` (never a literal),
  `--reasoning off` is appended when `LLM_REASONING=off`; chat spawns `uv run --with gradio python
  scripts/rag_chat.py` with `PG_RAG_CHAT_NO_BROWSER=1`; the port-owner fallback parses `netstat -ano`
  (all families — `-p TCP` hides every IPv6 listener) / `ss -ltnpH` / `lsof -t -i TCP:<port>
  -sTCP:LISTEN` (listener-scoped), so a client socket is never picked; the port probe covers both
  loopbacks (`127.0.0.1` + `::1`), matching the owner scan's family coverage; a pidfile pid is only
  signalled while its live image still matches the service's `process` **and** its recorded start
  token (read at spawn; unknown on either side is not a veto) still matches the live process —
  liveness is not identity, so a recycled pid must not be killed; `stop` reports a held port it cannot
  attribute and exits 1 (keeping the pidfile) instead of a false "Not running"; stop tree-kills
  (`taskkill /T /F` on Windows; on POSIX `killpg` **only** for a pid that came from a token-matched
  pidfile entry *and* leads its own group — a pid derived from the port owner, `pid <= 1`, or the
  caller's own group is signalled alone with a warning — SIGTERM then SIGKILL), so a foreign group
  is never killpg'd; a kill that could not be sent at all (an unattributable `pid <= 1` on Windows, a
  permission-denied signal, a `taskkill` that failed to run) skips the grace window and keeps the
  pidfile — reported as that
  failure, never as "surviving children"; a spawn that dies immediately leaves no pidfile;
  status probes the per-service health URL (`:8080/health` for the LLM, `:7860/` for chat) and treats
  HTTP 503 as **loading** (up), so `status` exits 0 on a healthy boot and 1 only when a service is
  genuinely down.
- **Change ⇒** `uv run pytest tests/test_services.py -q`, then a live `mise start` / `mise status` /
  `mise down` smoke (the manager owns the real :8080/:7860 processes).

### L11 — Docs drift gate (`scripts/check_docs.py`)

- **Tests**: `test_check_docs.py`
- **Source**: `scripts/check_docs.py` (`mise drift`, alias `dc`).
- **Contracts**: every `tests/test_*.py` is named in this map, owned by exactly one layer's
  `**Tests**` block, and every test path this map names exists; every backticked
  `scripts|src|tests|docs|.mise/tasks`-prefixed path in AGENTS.md and the skills resolves over
  `.py`, `.sh`, `.ps1`, `.mjs`, `.md`, `.json` and `.toml` — with `-` in the name class, since the
  audit's deleted names are hyphenated, and a path a SKILL.md addresses as `scripts/…` resolving next
  to that SKILL.md too (a skill bundles its own scripts) — while a *missing* source is drift, never a
  silent skip; `DOCUMENTS_VERSION` is quoted as a symbol (never a literal) in AGENTS.md / this map /
  RULES.md / the pg-rag skill; the golden counts, `SHORT_TIER_IDS` and `GOLDEN_DIR` the evaluation
  skill/gate quote exist; the documented `golden_check.py` flags are real; every skill named by
  `CANONICAL_SKILLS` **or** by the tree (a `SKILL.md` directory) is present and carries frontmatter —
  the union is deliberate, since a tree-only list cannot report a canonical skill deleted outright
  and a canonical-only list cannot report a newly landed skill; each `[env]` model single-source
  consumer (`scripts/services.py`, `scripts/agentic_tune.py`, AGENTS.md) is scanned for a **leaked
  literal value** of `LLM_MODEL`/`LLM_FLAGS` — the enforced rule is *no literal value*, not
  "references the variable", so a consumer that names neither variable still passes;
  every `ruff.toml` suppression (`lint.ignore` + `lint.per-file-ignores`) names a code
  `lint.select` carries, resolved the way ruff resolves it — inside the code's linter family, so
  `B` covers `B008` but **not** `BLE001` (a raw string prefix accepted exactly the dead
  suppressions the check exists for), with the `lint.ignore` arm dormant until that key returns
  to `ruff.toml`; the gates' runner files
  (`scripts/check_ts.mjs`, `package.json`, `package-lock.json`) are `git ls-files`-tracked, because a
  clone without them has no TypeScript gate at all and its tests merely *skip* — and a git that cannot
  answer (rc ≠ 0) is its own drift, never "untracked"; AGENTS.md's
  "`[tasks]` set is exactly:" list equals `mise.toml`'s task set, set for set; and the two docs
  quoting the suite counts must quote the live `N collected / M deselected / K default` from a real
  `pytest --collect-only` — parsed from any summary shape pytest prints (with a deselect count or
  without), with a *failed* collection reported as its own failure rather than as a stale count (this
  map states the convention, not a count, so it is deliberately not in the set). **This check has an
  offline unit test** (`test_check_docs.py`), which
  provisions a tmp tree and asserts the provisioned one is clean and each drift is reported — the
  pointer scan, the ownership map (including a `*`/`+` bullet, a fenced example of the convention and
  a file named twice in one block), the lint-suppression arm, the untracked-runner
  and unanswerable-git arms, the task list and the count quote are all covered there — while the
  model-literal *leak* (as opposed to a missing consumer file) is asserted only under `mise drift`;
- **Change ⇒** `uv run pytest tests/test_check_docs.py`, then `mise drift` (must print `0 drift`).

### L12 — Review tooling (`scripts/review_facts.py`)

- **Tests**: `test_review_facts.py`
- **Source**: `scripts/review_facts.py` (`uv run python scripts/review_facts.py`, `--json` for machines).
- **Contracts**: the sheet is **read-only** — it inspects, writes nothing git-visible (bytecode and
  the ruff cache only), and starts no service — and it
  carries exactly one section per fact class: `counts` (via the drift gate's own collector, so the sheet
  and `mise drift` cannot disagree; `None` when the gate could not collect, rendered as
  `(unavailable)` rather than as the number zero), `gates` (the three repo-wide **Python** gates —
  ruff / drift / compliance — each as `rc <code> — <summary>`, where a non-zero rc is a *fact* to
  report, not an error to raise, and the summary keeps the diagnosis as well as the trailer; the full
  suite and `mise lint-ts` are deliberately not rows), `tasks` (the `mise.toml`
  task names + aliases, tolerating mise's trivial string-task form), `tree` (HEAD, whether the working
  tree is frozen, and whether the runner files
  the gates shell out to are `git ls-files`-tracked — read from *every* `ls-files` line, because a
  "last output line" reader reported two of three files as untracked — with an unanswerable git
  reported as itself, never as "untracked"), `liveness` (how python and node
  each classify a released pid — the platform fact reviewers kept re-measuring, and the case where the
  two agree, not the case where they diverge), and `limits`
  (working-tree-only `data/` inputs a clone cannot see, with a *directory* counted as tracked when any
  file under it is — `git ls-files` never prints the directory). The markdown block and `--json` carry the same
  facts.
- **Change ⇒** `uv run pytest tests/test_review_facts.py`, then `uv run python scripts/review_facts.py`
  to see the sheet a review pass pastes into its shared context.

## Hard rules (no exceptions)

1. A test that **changes an assertion** is a **contract change** — state the
   new contract in the test and run the layer's sibling suite (L1–L12 above).
2. A test that **fails without a contract change** is a **regression in
   source** — fix the source, not the test.
3. Never hand-edit a build artifact to satisfy a test (the store's `documents`
   rows, `wiki_parsed.json`, `data/derived/*`, real `data/wiki/.meta.json`). Change
   the generator (RULES.md #2).
4. The retrieval path is the agentic tool loop and only that — a test or change
   that reintroduces a dense/RRF/reranker path is a new architecture, not a fix.
5. A tool-loop change is not done until **L3** passes; a golden change until
   `test_golden_check.py` collects it.

## Writing a good test (for NEW tests)

- One observable contract per test; a deterministic, isolated, full-suite-safe
  case. Test the contract, not plumbing, source text, or incidental defaults.
- **Module docstring**: one line stating the file's single contract (the
  layer it defends) — a fresh model reads that before the test bodies.
- Name tests as `test_<behavior that can regress>`; assert meaning, not
  implementation.
- Temp-dir isolation for anything touching `data/` (never real meta/docs).
