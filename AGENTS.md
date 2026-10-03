# Repository Guidelines

Oh My Pi agent guide for **pg-rag** — the unified repo for the Project
Gorgon knowledge base: corpus build, retrieval pipeline, agentic SQLite
store, and tooling in one place.
The marimo notebook lives in `notebooks/` (molab-mirror).

---

## Project Overview

Build a searchable, fact-grounded knowledge base from two sources:
- **CDN**: structured `data/cdn/*.json` tables (items, recipes, abilities, skills, quests, npcs, areas, effects).
- **Wiki**: raw `data/wiki/*.txt` page dumps (hashed filenames, titles via `data/wiki/.meta.json`).

Generate typed documents → embed → index in Chroma → retrieve (dense + BM25 → RRF fuse → rerank) → answer via LLM. Ships a CLI (`pgrag`), OpenWebUI pipe, Gradio chat, curation tooling, and golden/embed eval suites.

## Architecture & Data Flow

```
cdn/*.json ─┐
            ├─ loaders → GameDatabase(tables + wiki) ─┐
wiki/*.txt ─┘                                          ├─ documents/ (builder + wiki_builder + skill_profiles + summaries)
data/wiki/curated/*.json ──────────────────────────────┘
                        │  build.py: generate_documents() → documents.json (+ documents_version.json, stamps DOCUMENTS_VERSION)
                        ▼
              build_index.py → Chroma collection "project_gorgon" (incremental, hash-based)
                        │  EMBED_BATCH_SIZE=10000, validates EMBEDDING_DIM=384
                        ▼
Query → query_classifier → retriever (dense + BM25 → RRF fuse → reranker :8082)
                        │
                        ├─ entity queries → entity_retrieval (skill dossiers sorted by required level) + gap-fill
                        └─ pipeline.ask / ask_stream → prompt → LLM :8080 → answer
```

- **Freshness contract (avoid stale-document trap)**: `build-documents` stamps `data/derived/documents_version.json` with `DOCUMENTS_VERSION` (config.py; authority is the constant, not this doc). `build-index` only reads the persisted `documents.json` and refuses to embed if the stored version differs — it never regenerates. To converge a source in one command, use `mise sync-*` tasks (they run `build-documents` first). Bump `DOCUMENTS_VERSION` whenever document shape changes.
- **Agentic SQLite store + tool loop**: `src/pgrag/agentic/` builds a separate SQLite store (`data/sqlite_gorgon.db`, `mise sql-store`) holding the primary sources — hand-mapped CDN tables, raw wiki pages, live play-session data (`~/AppData/LocalLow/Elder Game/Project Gorgon`: ChatLogs, Player.log, Reports, Books), and glogger play-history (`src/pgrag/agentic/glogger.py` reads `%APPDATA%/glogger.Release/glogger.db` via a WAL-safe snapshot copy: stall sales, gift/favor deltas, kills/deaths, loot transactions, recipe completions, Words of Power, vendor gold — watermark-incremental in `data/sqlite_state.json` under `"glogger"`; glogger's own CDN mirrors are deliberately skipped, ours is fresher). `src/pgrag/agentic/loop.py` runs the LLM in a bounded tool loop (5 tools: sql_query, find_entities, get_page, corpus_search, player_state; native tool_calls + fenced ```tool text-protocol fallback; max 6 rounds; temp 0/seed 0) via `mise agentic-chat`. `corpus_search` reads the BM25 doc store — `--corpus tool` reads the Phase B variant (`data/tool_documents.json` + `data/tool_bm25.pkl`, built by `scripts/build_tool_corpus.py`). This never touches documents.json / Chroma / the golden pipeline.
Tuning sweeps live in `scripts/agentic_tune.py` (`mise agentic-tune`,
reports in `data/agentic_tune/` + README verdicts): measured outcomes are
**temp 0 confirmed** (no candidate beat it on any seed; goldens stay temp
0/seed 0), **reasoning budget 4096 kept**, **Ornith-1.5-9B kept**
(qwen-27b ties 6/6 at ~2.6x latency; gemma-26b regressed a case). Qwen MoE
templates reject a 2nd system message, so the loop sends the forced-final
notice as a user-role message.

## Key Directories

- `src/pgrag/` — importable package (`uv` installs editable).
  - `cli.py` — CLI: `download-cdn`, `download-wiki`, `build-documents`, `build-index`, `validate`.
  - `config.py` — paths, `EMBEDDING_DIM=384`, `CONTEXT_BUDGET=80000`, `DOCUMENTS_VERSION`, `TARGET_CATEGORIES`/`RECURSIVE_CATEGORIES`.
  - `build.py` — `generate_documents()` orchestration.
  - `loaders/` — `cdn_loader` (tables from CDN json), `wiki_loader` (wiki text + `.meta.json` title mapping, orphan cleanup), `database.GameDatabase` (in-memory `tables` + `wiki` bag).
  - `documents/` — `builder.py`, `wiki_builder.py` (mwparserfromhell → sections/chunks, `parent_id` links), `chunking.py`, `resolver.py` (internal code → display name), `skill_profiles.py`, `summaries.py` (gathering skill maps).
  - `embeddings/llama_embeddings.py` → :8081.
  - `vectorstore/` — `build_index.py`, `hashes.py`, `health_check.py`.
  - `rag/` — `retriever.py`, `reranker_client.py`, `bm25.py`, `query_classifier.py`, `query_plan.py`, `spelling.py`, `entity_retrieval.py`, `resolve.py`, `synthesis_detector.py`+`synthesis_generator.py`, `pipeline.py` (+ `ask_stream`), `prompts.py`, `llm.py`.
- `scripts/` — eval + service tooling (see Important Files).
- `tests/` — pytest suite, imports the installed `pgrag` package.
- `data/` (gitignored) — `cdn/`, `wiki/` (+`curated/`, `.meta.json`), `derived/` (`documents_version.json`), `documents.json`, `chroma/`, `golden/`, `retrieval_traces/`, eval records (`embed_eval_*.log`, `embed_vram.json`, `bakeoff_*.json`). Service logs live at project-root `logs/` (`embed.log`, `llm.log`, `rerank.log`, `chat.log`, and `webui.log` when run).
- `.omp/` — oh-my-pi config: `RULES.md`, `config.yml`, `WATCHDOG.md`.
- `.agents/skills/` — all agent skills, discoverable by any agent harness: `pg-rag`, `pg-data`, `retrieval`, `evaluation`, `testing` (pipeline/workflow skills) + `molab-notebook` (pairing on the molab-hosted marimo chat notebook). Skills live here only — commit changes here, never re-create `.omp/skills/` copies.
- `notebooks/molab-mirror/` — the marimo notebook that runs the PG-RAG chat on molab (marimo's hosted notebook service). Platform mechanics (sandbox lifecycle, GPU attach, torch repair, HF bucket, auto-start limits): `docs/MOLAB_OPS.md`. Pairing/protocol: the `molab-notebook` skill.

## Development Commands

Python ≥3.14, managed with `uv`. `mise tasks` lists everything (`uv run pgrag …`).

```sh
uv run pgrag download-wiki          # fetch wiki dumps
uv run pgrag download-cdn           # fetch CDN json
uv run pgrag build-documents        # regenerate documents.json (stamps version)
uv run pgrag build-index            # embed + index into Chroma
uv run pgrag validate              # full offline pipeline integrity check (sources, documents+freshness, wiki meta, index)
uv run pgrag build-index --source cdn|wiki|computed|curated   # partial rebuild of one source
mise sync-wiki / sync-cdn / sync   # build-documents + build-index in one shot (aliases syw/syc/sy)
mise generate-docs                 # bare idempotent documents rebuild (alias docs)
mise upload-docs                   # publish data/documents.json -> hf://buckets/Nubula/paddock (HF_TOKEN write; verifies remote size; alias up)
mise golden                        # golden eval (needs :8080 + :8081)
mise golden-short                  # quick tier (~3-5 min; alias gds)
mise golden-one -- fireball-ability   # rerun named golden case(s) fast (alias go; comma-separate ids)
mise golden-flaky / golden-flaky-list # focus on historically-troublesome golden cases / audit stats
mise sql-store (alias sq)        # build data/sqlite_gorgon.db: CDN + wiki + session + glogger sources for the agentic tool loop
mise agentic-chat (alias ac)     # agentic CLI chat over the SQLite store (tool loop; needs only LLM :8080; pre-chat session+glogger store refresh runs automatically, --no-sync skips)
mise agentic-eval (alias ae)     # agentic tool-loop eval over the golden fact set (short tier + store-only glogger cases; report data/agentic_eval_report.json)
# tuning sweeps: uv run python scripts/agentic_tune.py --eval-set cases --sweep-temperature 0,0.2,0.4 --sweep-seeds 0,1,2
mise chat                          # Gradio chat (primary UI; also started by `mise start` = start-all; needs embed + LLM up)
mise lint                        # ruff check src scripts tests (alias li) — run after editing code
mise fmt                         # ruff format + safe autofix (alias fo)
uv run pytest                      # offline test suite
uv run pytest tests/test_retrieval_unit.py tests/test_bm25.py tests/test_rerank*.py  # retrieval regression
mise drift                        # check docs/skills against the repo (aliases: dr)
```

**Build/refresh needs no servers**; only Q&A/eval (`golden`, `chat`, `scripts/retrieval.py`) do.

## Code Conventions & Common Patterns

- **Python/uv**: py≥3.14, type-annotated, package-relative imports (`from pgrag.config import …`). Edit the package in `src/pgrag/`, never hand-edit build artifacts (`documents.json`, `data/derived/wiki_parsed.json`, `bm25_index.pkl`).
- **Doc identity contract**: every doc carries `id` + `metadata.source` + `metadata.table`. Chroma `update()` merges metadata (add-only) — renames/removals require a full rebuild. Preserve these fields.
- **Retrieval architecture is fixed**: dense + BM25 → RRF fusion (`_hybrid_fuse`, `HYBRID_MULTIPLIER`) → reranker (`bge-reranker-v2-m3` :8082, lexical fallback if down). Don't replace a component without identifying the existing implementation first; verify with the retrieval regression tests.
- **Incremental index**: `build_index.py` computes embedding hashes to avoid re-embedding unchanged docs, batches at `EMBED_BATCH_SIZE=10000`, validates dims against `EMBEDDING_DIM`. **Never change embedding models silently** — embeddings are a fixed-dim contract with the Chroma collection.
- **Error handling**: server clients raise domain errors (e.g. `EmbeddingServerError`, LLM/rerank errors) with the URL in the message; offline tests assert these. Server reachability is checked but servers down → graceful lexical/fallback paths.
- **Wiki categories**: `TARGET_CATEGORIES` flat + `RECURSIVE_CATEGORIES` (Creatures d2, Items d1) — monsters/items only via recursion. Subcats bare (no `Category:` prefix). Uncategorized pages (racial-stat pages, badge hub) come from `WIKI_TITLE_EXTRAS` — category-driven discovery cannot see them. Wiki filenames `{safe_title}_<sha256-8>.txt`; display names come only from `.meta.json` (never filenames). Sync ends with `remove_orphan_files`.
- **Test isolation**: temp dirs for anything touching `data/` (real meta/documents are guarded — a past bug silently destroyed `data/wiki/.meta.json`). `tests/conftest.py` snapshots `data/cdn`/`data/wiki` and asserts immutability. Fixture-based store tests pass `session_dir=`/`glogger_db=` pointing at absent tmp paths — the `build_store` defaults are LIVE sources (real game dir, real glogger DB), so a fixture that omits them ingests live data and races it.
- **Store-only goldens**: golden JSONs with `"store": true` (glogger play-history facts) are excluded from the pipeline harnesses (golden_check/golden_rerun/pytest golden tiers) and run only via the agentic loop eval (`mise agentic-eval`), which appends them to the short tier.

## Important Files

- `src/pgrag/cli.py` — entry point; `config.py` — constants/paths; `build.py` — document orchestration; `rag/pipeline.py` — query path (deterministic temp=0/seed=0).
- `src/pgrag/agentic/glogger.py` — glogger play-history ingestion (WAL-safe snapshot copy → watermark-incremental mirror into the store; `DEFAULT_GLOGGER_DB` = `%APPDATA%/glogger.Release/glogger.db` is what moves if glogger relocates).
- `scripts/pg_rag.py` — OpenWebUI pipe, `PG_ROOT = os.environ.get("PG_RAG_ROOT", r"F:\ProjectGorgon\pg-rag")` (env override, Windows default) + `os.chdir()`, adds `PG_ROOT/src` to `sys.path` — the default path is what moves if the repo relocates. Valves: `TOP_K=40`, `USE_HYBRID=True`, `USE_RERANK=True`.
- `scripts/curator.py` + `curator_scheduler.py` — heuristic (non-LLM) curation: regex-detect fragmented knowledge (area_levels, skill_trainers, crafting_progressions), write template docs to `data/wiki/curated/`, scheduler persists state to `data/curator_state.json` and rebuilds doc/index on change. Deterministic by design — no LLM, so curated docs are stable anchors.
- `scripts/golden_check.py` — fact-presence golden eval → `data/golden/`; `scripts/golden_rerun.py` — quick named-case rerun + flaky focus (`mise golden-one`/`golden-flaky`; appends miss history to `data/golden/history.jsonl`); `scripts/embed_eval.py` (+`bakeoff_corpus.py`; VRAM helpers in `embed_vram_probe.py`) — embedding bake-offs.
- `docs/TEST_CONTRACTS.md` — layer→tests→contract map + regression-triage protocol (read before changing behavior/tests); `docs/REVIEW.md` — audit findings + improvement backlog.
- `scripts/check_services.py` — [OK]/[DOWN] probes for all services.
- `mise.toml` `[env]`: `WEBUI_DIR`, `LOGS_DIR` — update if paths move.

## Runtime/Tooling Preferences

- **Runtime**: Python ≥3.14 via `uv`; model binaries fetched with `hf` global CLI (cache `F:\AI\models\hub\`; GGUF via `-hf org/repo:quant`).
- **Local services** (running on Windows host):
  | svc | port | model / note |
  |-----|------|--------------|
  | Embeddings | 8081 | `EMBED_MODEL` (`[env]`) — bge-small f16, cls pooling, hard 512-token server cap (input chars clipped to `llama_embeddings.MAX_EMBED_CHARS` = 2000); needed for Q&A/eval |
  | LLM | 8080 | `LLM_MODEL` (`[env]`; `LLM_FLAGS` carries launch tuning, e.g. `--jinja`) — RAG Q&A; a running instance OOMs before start → `mise down` (alias of `stop-all`) first |
  | Reranker | 8082 | `RERANK_MODEL` (`[env]`) — bge-reranker cross-encoder; optional, lexical fallback |
  | Chat (Gradio) | 7860 | primary UI — part of `mise start` (alias of `start-all`); `mise chat` foreground; history in browser localStorage |
  | OpenWebUI | 3000 | optional (legacy) — `mise webui-start`/`webui-stop`; no longer in `start-all`/`stop-all` |
- **Single-source models**: refs + tuning flags live once in `mise.toml [env]` (`*_MODEL`/`*_FLAGS`); consumed by `.mise/tasks/*-start.ps1` (`$env:`), `mise debug-*` (`{{ env.* }}`), `scripts/vram_sweep.py` and `scripts/embed_eval.py` (tomllib). `mise drift` fails if any consumer hardcodes a model literal.
- `scripts/rag_chat.py` and `pg_rag.py` assume these services up.
- Tests import the installed `pgrag` package — after changing `src/pgrag/`, no reinstall needed (editable install).

## Testing & QA

- **Framework**: pytest via `uv run pytest`; 597 tests pass, 42 skipped (655 collected — skips are offline server-guards: golden tests), 16 slow deselected. All offline; temp-dir integration.
- **Golden eval** (`tests/test_golden_check.py`): parametrized over `data/golden/*.json`; `require_servers` fixture skips unless LLM :8080 + embed :8081 are up; retries once (2 attempts) to damp LLM nondeterminism; both attempts fail = regression. `GENERATION = {"temperature":0,"seed":0}` is wired into every `ask()`; observed 7/8/10 single-run variance was **LLM-side** (reasoning-ON thinking trajectory is not greedy-pinned even at temp 0 + seed), not a harness miss. For a byte-reproducible comparison, launch the server with `--reasoning off` (gemma AND qwen proven reproducible); reasoning-ON production runs remain single-sample noisy. See `docs/LLM_MODEL_WIRING.md` → "Card flags & determinism".
- **Retrieval regression set** (run when changing retrieval): `tests/test_bm25.py`, `tests/test_retrieval_unit.py`, `tests/test_rerank*.py`, `tests/test_retriever_spelling.py`. **`docs/TEST_CONTRACTS.md` is the authoritative layer→tests→contract map** — read L3 before any retrieval change: it flags shared-function facet coverage (`retrieve()` asserted across 5 files; `_hybrid_fuse` across `test_bm25.py`/`test_rerank.py`) and notes the stale-`DOCUMENTS_VERSION` refusal is directly tested in `test_build_index.py`.
- **Test edits are contract changes.** Editing what a test *asserts* is not a workaround — state the new contract in the test, run the layer's sibling suite (`docs/TEST_CONTRACTS.md`), and never hand-edit a build artifact (`documents.json`, `bm25_index.pkl`, `wiki_parsed.json`) to satisfy a test. If the behavior didn't legitimately change, the failure is a source regression: fix the source, not the test.
- **Lint gate** (`tests/test_lint.py`, layer L9 in `docs/TEST_CONTRACTS.md`): `uv run ruff check src scripts tests` must be clean — a failing check fails `uv run pytest`. Run `mise lint` after editing code (`mise fmt` applies the safe autofixes: import sort, unused imports, f-strings, formatting). Rules live solely in `ruff.toml`; the per-file-ignores there (scripts: broad excepts, unchecked subprocess; tests: broad catches) are deliberate. New rule ⇒ fix all findings in the same commit.
- **Key suites** (from `tests/`): `test_documents.py` (doc shape), `test_chunking.py`, `test_health_check.py` (index integrity incl. no-SQLite-crash on large collections), `test_llm.py` (SSE streaming parse), `test_download_wiki.py` (batching, redirects, orphan cleanup — patches `META_FILE`+`WIKI_DIR` to tmp), `test_query_classifier.py`/`test_query_plan.py`, `test_bm25_persist.py`, `test_rerank_fallback.py`, `test_hashes.py`, `test_embed_validation.py`.
- **Coverage expectation**: one test defends each observable contract; integration tests use temp dirs and mocked servers rather than live ones.