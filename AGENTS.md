# Repository Guidelines

Oh My Pi agent guide for **pg-rag** — the unified repo for the Project
Gorgon knowledge base: corpus build (documents + BM25 + tool corpus), the
agentic SQLite store, and the single retrieval path — the agentic tool loop —
in one place.
The marimo notebook lives in `notebooks/molab-agentic/` (the tool loop over a
private store snapshot); the corpus-chat **mirror notebook was deleted** with
the dense-RAG cutover.

---

## Project Overview

Two sources feed a searchable, fact-grounded knowledge base:
- **CDN**: structured `data/cdn/*.json` tables (items, recipes, abilities,
  skills, quests, npcs, areas, effects).
- **Wiki**: raw `data/wiki/*.txt` page dumps (hashed filenames, titles via
  `data/wiki/.meta.json`).

`generate_documents()` → `data/documents.json` (+ a `DOCUMENTS_VERSION` stamp)
→ a persisted BM25 index (`data/bm25_index.pkl`) and a trimmed tool-corpus
variant (`data/tool_documents.json` + `data/tool_bm25.pkl`). A separate SQLite
store (`data/sqlite_gorgon.db`) holds the same CDN/wiki data plus live session
and glogger play history. **All retrieval is the agentic tool loop**
(`pgrag.agentic.loop.run_loop`, `src/pgrag/agentic/loop.py`) driving five tools
over that store + the BM25 corpus. Ships a CLI (`pgrag`), the Gradio chat,
agentic CLI chat, curation tooling, and the golden eval.

## Architecture & Data Flow

```
cdn/*.json ─┐
            ├─ loaders → GameDatabase(tables + wiki) ─┐
wiki/*.txt ─┘                                          ├─ documents/ (builder + wiki_builder + skill_profiles + summaries)
data/wiki/curated/*.json ──────────────────────────────┘
                        │  build.py: generate_documents() → data/documents.json
                        │           (+ data/derived/documents_version.json, stamps DOCUMENTS_VERSION)
                        ▼
      rag/bm25.py              → data/bm25_index.pkl        (persisted lexical index)
      scripts/build_tool_corpus.py → data/tool_documents.json + data/tool_bm25.pkl  (corpus_search --corpus tool)

      scripts/build_sqlstore.py → data/sqlite_gorgon.db  (CDN + wiki + live session + glogger)

Question → agentic.loop.run_loop (blocking, bounded rounds; temp 0 / seed 0 by default)
            ├─ sql_query      read-only SELECT over the store (LIMIT 200, 2M-op budget)
            ├─ find_entities  FTS5 over entity_names
            ├─ get_page       full wiki page text
            ├─ corpus_search  BM25 over data/documents.json (or the tool corpus)
            └─ player_state   live-session sections (summary/skills/stall/abilities/…)
                        ▼
              prompt + tool results → LLM :8080 → grounded answer
```

The five tool names are the exact `AVAILABLE_TOOLS` string in
`src/pgrag/agentic/tools.py`: `sql_query, find_entities, get_page,
corpus_search, player_state`.

- **DELETED in the cutover** — do not name these as live: the whole dense path
  (`pgrag.rag.{pipeline,retriever,reranker_client,entity_retrieval,resolve,
  synthesis_detector,synthesis_generator,spelling,query_classifier,query_plan,
  retrieval_eval,prompts}`, `pgrag.vectorstore.*`, `pgrag.embeddings.*`,
  `pgrag.validation`, `pgrag.documents.prompts`), the scripts
  `retrieval.py`/`rag.py`/`retrieval_eval.py`/`bakeoff_corpus.py`/
  `embed_eval.py`/`embed_vram_probe.py`/`vram_sweep.py`, the
  `.mise/tasks/{embed,rerank}-{start,stop}.ps1` tasks, `notebooks/molab-mirror/`,
  and their tests. There is no Chroma collection, no dense retrieval, no
  reranker, no embedding server, no RRF fusion, no entity dossier / `_gap_fill`,
  no query classifier, and no IR-metric harness. Chroma is out of
  `pyproject.toml`; the venv no longer ships `chromadb`/`onnxruntime`.
  `EMBEDDING_DIM` and `CONTEXT_BUDGET` no longer exist.

- **Freshness contract (avoid the stale-document trap)**: `build-documents`
  stamps `data/derived/documents_version.json` with `DOCUMENTS_VERSION`
  (`src/pgrag/config.py`; authority is the constant, not this doc).
  `scripts/build_tool_corpus.py` (and the `sync-*` tasks that call it)
  **refuses** to run against a stale stamp — the ported successor of the old
  `build-index` refusal. `pgrag/rag/bm25.py` serves its persisted index while
  `data/documents.json` is unchanged (mtime-keyed) and rebuilds otherwise.
  Bump `DOCUMENTS_VERSION` whenever document shape changes; regenerate with
  `build-documents` (`mise generate-docs`) or a `mise sync-*` task.

- **Agentic SQLite store + tool loop**: `src/pgrag/agentic/` builds a separate
  SQLite store (`data/sqlite_gorgon.db`, `mise sql-store`) holding the primary
  sources — hand-mapped CDN tables, raw wiki pages, live play-session data
  (`~/AppData/LocalLow/Elder Game/Project Gorgon`: ChatLogs, Player.log,
  Reports, Books), and glogger play-history (`src/pgrag/agentic/glogger.py`
  reads `%APPDATA%/glogger.Personal/glogger.db` (the fork-local daily driver;
  `glogger.Release` is the fallback) via a WAL-safe snapshot copy:
  stall sales, gift/favor deltas, kills/deaths, loot transactions, recipe
  completions, Words of Power, vendor gold — watermark-incremental in
  `data/sqlite_state.json` under `"glogger"`; glogger's own CDN mirrors are
  deliberately skipped, ours is fresher). `src/pgrag/agentic/loop.py` runs the
  LLM in a bounded tool loop (`DEFAULT_MAX_ROUNDS` rounds; each tool-result
  payload capped at 12000 chars; a final no-tools answer is forced once the
  budget is exhausted). `run_loop` returns the same 5-key shape as the deleted
  `pipeline.ask` — `{answer, documents, sources, rounds, trace_rounds}` — where
  `documents` is exactly what the model was fed and `sources` is one entry per
  tool round.
- **Tuning sweeps** live in `scripts/agentic_tune.py` (`mise agentic-tune`,
  reports in `data/agentic_tune/`): measured outcomes are **temp 0 confirmed**
  (no candidate beat it on any seed; goldens stay temp 0/seed 0), **reasoning
  budget 4096 kept**, **Ornith-1.5-9B kept** (qwen-27b ties 6/6 at ~2.6x
  latency; gemma-26b regressed a case). Qwen MoE templates reject a 2nd system
  message, so the loop sends the forced-final notice as a user-role message.

## Key Directories

- `src/pgrag/` — importable package (`uv` installs editable).
  - `cli.py` — CLI: `download-cdn`, `download-wiki`, `build-documents`
    (exactly those three).
  - `config.py` — paths, `DOCUMENTS_VERSION`.
  - `build.py` — `generate_documents()` orchestration.
  - `loaders/` — `download_wiki` (category-driven fetch; owns
    `TARGET_CATEGORIES`/`RECURSIVE_CATEGORIES`/`WIKI_TITLE_EXTRAS`, batching,
    retry, atomic `.meta.json`), `cdn_loader` (tables from CDN json),
    `wiki_loader` (wiki text + `.meta.json` title mapping, orphan cleanup),
    `database.GameDatabase` (in-memory `tables` + `wiki` bag).
  - `documents/` — `builder.py`, `wiki_builder.py` (mwparserfromhell →
    sections/chunks, `parent_id` links), `chunking.py`, `tokenizer.py` (the
    token counter the chunk budgets measure with), `resolver.py` (internal
    code → display name), `skill_profiles.py`, `summaries.py` (gathering skill
    maps), `creature_zones.py`, `combat_xp.py`.
  - `rag/bm25.py` — the lexical index (build + persistence +
    `load_bm25_index`); `rag/llm.py` — the LLM client (SSE parse, server-error
    types).
  - `agentic/` — `store.py` + `session.py` + `glogger.py` (SQLite store
    build), `tools.py` (the five tools), `loop.py` (the tool loop),
    `eval.py` (golden fact-presence eval + tier membership).
- `scripts/` — eval + service tooling (see Important Files).
- `tests/` — pytest suite, imports the installed `pgrag` package.
- `data/` (gitignored) — `cdn/`, `wiki/` (+ `curated/`, `.meta.json`),
  `derived/` (`documents_version.json`, `wiki_parsed.json`), `documents.json`,
  `bm25_index.pkl`, `tool_documents.json`, `tool_bm25.pkl`,
  `sqlite_gorgon.db` (+ `sqlite_state.json`), `golden/`, `agentic_tune/`,
  `eval/`, and `retrieval_traces/` (loop traces: `PGRAG_TRACE=1` on
  `golden_check.py`, `--trace` on `golden_rerun.py`). Service logs live at project-root
  `logs/` (`llm.log` + `llm-stdio.log` from the launcher, `chat.log` +
  `chat-error.log`). The dense path's
  artifacts (`chroma/`, `bakeoff_*.json`, `embed_vram.json`,
  `rerank_stats.json`, `evaluation/queries.jsonl`) were deleted with the
  cutover — nothing reads them. `retrieval_traces/` is live (loop traces).
- `.omp/` — oh-my-pi config: `RULES.md`, `config.yml`, `WATCHDOG.md` and
  `hooks/pre/00-repo-lock.ts`. The review protocol used to live here as a
  project slash command (`commands/frozen.md`, `/frozen <tag|sha> [base tag or
  -] [focus...]`); it is retired — a pass now runs from the shared skill, see
  **Code review loop**.
- `.agents/skills/` — all agent skills, discoverable by any agent harness:
  `pg-rag`, `pg-data`, `retrieval`, `evaluation`, `testing` (project skills) +
  `molab-notebook` (pairing on the molab-hosted marimo notebook). Skills live
  here only — commit changes here, never re-create `.omp/skills/` copies.
  The one skill this repo does not own but does use is the user-level
  `frozen-review` (`~/.agents/skills/frozen-review/SKILL.md`, the agents-standard
  root, shared with the sibling glogger repo) — this repo's review call.
- `notebooks/molab-agentic/` — the **tool-loop-only** molab notebook:
  `pgrag.agentic.loop.run_loop` against a snapshot of `data/sqlite_gorgon.db`
  published to the private `hf://buckets/Nubula/paddock-private` by
  `mise upload-store` (store + manifest + `src`/`scripts` tarball). No torch in
  its kernel (inference is the vLLM sidecar, which the notebook launches from
  the extracted tarball when `:8000` is down), so no env repair / session
  restart / in-process fallback. PEP 723 deps: `marimo[mcp]`,
  `huggingface-hub`, `requests` — the kernel is Python 3.13, so everything the
  tarball ships (`src/`, `scripts/`) must stay 3.13-parseable: no PEP 758
  `except A, B:` (valid here on ≥3.15, a SyntaxError there). Gated by
  `tests/test_lint.py::test_shipped_source_stays_python_313_parseable` —
  write `except (A, B):`. Platform mechanics:
  `docs/MOLAB_OPS.md`. Pairing/protocol: the `molab-notebook` skill.
- `docs/` — `TEST_CONTRACTS.md` (the authoritative layer→tests→contract map),
  `REVIEW.md` (audit findings + backlog), `LLM_MODEL_WIRING.md` (per-model
  launch flags + determinism evidence), `MOLAB_OPS.md` (molab platform
  mechanics), `research/` (policy docs).

## Development Commands

Python ≥3.15, managed with `uv`. `mise tasks` lists everything. The
`mise.toml` `[tasks]` set is exactly: `test`, `sync-fast`, `sync-full`,
`generate-docs`, `build-bm25`, `build-tool-corpus`, `sync-wiki`, `sql-store`,
`upload-store`, `upload-docs`, `agentic-chat`, `agentic-tune`, `golden`,
`golden-short`, `golden-one`, `golden-type`, `golden-flaky`,
`golden-flaky-list`, `debug-llm`, `chat`, `llm-start`, `llm-stop`,
`chat-start`, `chat-stop`, `start-all`, `stop-all`, `status`, `drift`,
`compliance`, `logs`, `tail-llm`, `lint-ts`, `ts-deps`, `lint`, `format`. Service tasks
(`llm-start`, `llm-stop`, `chat-start`, `chat-stop`) run
`scripts/services.py`; the `.mise/tasks/` directory is gone.

```sh
uv run pgrag download-wiki          # fetch wiki dumps
uv run pgrag download-cdn           # fetch CDN json
uv run pgrag build-documents        # regenerate documents.json (stamps version)
mise sync-fast / sync-full          # CDN (+wiki) → documents → BM25 → tool corpus (aliases sy/syf)
mise sync-wiki                      # converge wiki to current source, then documents + BM25 + tool corpus (alias syw)
mise generate-docs                  # bare idempotent documents rebuild (alias docs)
mise build-bm25 / build-tool-corpus # rebuild the lexical index / tool-corpus variant (b25/btc)
mise sql-store (alias sq)           # build data/sqlite_gorgon.db: CDN + wiki + session + glogger sources
mise upload-docs (alias ud)         # publish data/documents.json to the PUBLIC corpus bucket for the molab notebook
mise agentic-chat (alias ac)        # agentic CLI chat over the store (tool loop; needs only LLM :8080; pre-chat session+glogger refresh runs automatically, --no-sync skips)
mise golden                         # full golden eval through the loop (LLM only; LONG — batch it in the background)
mise golden-short                   # the 8-case fast tier (alias gds)
mise golden-one fireball-ability    # rerun named golden case(s) fast (alias go; comma-separate ids)
mise golden-type recipe             # golden subset by type (entity|recipe|comparison|general; alias gt)
mise golden-flaky / golden-flaky-list # focus on historically-troublesome cases / audit stats
mise chat                           # Gradio chat (primary UI; started by `mise start`; refreshes the store first; needs only LLM up)
mise lint                           # ruff check src scripts tests (alias li) — run after editing code
mise lint-ts                        # TypeScript parse gate for .omp/hooks/** (alias tsl; needs `mise ts-deps` once)
mise format                         # ruff format + safe autofix (alias fo)
uv run pytest                       # offline test suite
mise drift                          # check docs/skills against the repo (alias dc)
mise compliance                     # TOS/CoC hard-line scan (alias comp) — also step 1 of mise test
```

**Build/refresh needs no servers**; only Q&A/eval (`golden*`, `chat`,
`agentic-chat`) need the LLM at :8080.

**Concurrency gate (two layers).** The corpus *writers* own an atomic
`O_EXCL` lock at `data/.omp-build.lock` for the duration of a build —
`pgrag build-documents` (`src/pgrag/cli.py`), the BM25 persist/rebuild
(`src/pgrag/rag/bm25.py`) and `scripts/build_tool_corpus.py`, all through
`pgrag.buildlock.corpus_lock` — so two omp instances on this repo never rewrite
the shared corpus at once, whatever the invocation spelling (mise task, alias,
`mise run --force …`, `… ::: syf`, direct CLI or direct script; a held lock
fails the second builder loudly). The pre-command hook
(`.omp/hooks/pre/00-repo-lock.ts`) is the *early warning* in front of that: it
probes the same file and blocks a destructive corpus command (`build-*`,
`generate-docs`, `mise sync-*` and their aliases) while a live owner holds it —
it never takes the lock itself, because a hook's tool call returns long before a
build finishes. `mise sql-store` (private additive store, not corpus state) and
the publish tasks are deliberately unguarded, and a pure read that only *names*
a build task (`grep -rn build-documents .`) is never blocked. Never bypass the
lock by hand-editing it; to clear a stuck one, delete `data/.omp-build.lock`.

## Code review loop

**`/skill:frozen-review` is this repo's review call** — a user-level skill
(`~/.agents/skills/frozen-review/SKILL.md`, the agents-standard root, shared with
the sibling glogger repo) that owns the whole loop: the freeze invariant, the
target grammar, facts-once, the three intake categories, the report contract and
the closing rule. It takes its target from an argument when given one (a tag
freezes the range that starts after it, a SHA that single commit, `worktree` the
working tree) and otherwise derives it from this repo's pass markers, so a fix
batch is reviewed by invoking it again with no argument: the delta since the last
`review-pass*` tag, the working tree when the tree is dirty, and nothing at all
when the marker already sits on HEAD. Skills are discovered at startup
(`omp://skills.md`) — there is no file watcher — so a freshly written skill
answers `Unknown skill` until a restart. The project command that used to carry
this protocol (`.omp/commands/frozen.md`) is retired; `docs/REVIEW.md` →
"Review protocol" keeps the pass history and the evidence behind each rule.

- **Freeze the target, and never mix revisions.** The diff handed to a reviewer
  and the files it reads must be the same revision. A checkout that is merely
  ahead of the frozen tag is answered with `git worktree add ../review-<tag>
  <tag>`, which puts every reviewer (and every path they execute) at that SHA
  with the main checkout untouched — never review `origin/HEAD...main` unless it
  resolves to the same commit.
- **Measure the facts once, in the parent, and paste them into every brief.** The
  row is `uv run python scripts/review_facts.py` (~4 s warm; `--no-gates` for
  ~1.6 s, `--json` for machines). The sheet carries the suite *counts*, the
  `ruff` / `mise drift` / `mise compliance` verdicts, the task set, the freeze
  state and the platform's liveness answer — deliberately **not** the suite
  verdict or `mise lint-ts`, which the one runner measures once on the frozen SHA
  (`uv run pytest` and `node scripts/check_ts.mjs`) and reports alongside the
  sheet. A red row is a fact to report once, not a per-file finding, and no slice
  re-derives a row.
- **Intake is three categories.** (1) behaviour nobody intended, (2) a claim the
  code or its docs make that the code does not honour, (3) a gate whose
  documentation is stronger than its check. Anything else is out of scope: say so
  in the request. `docs/REVIEW.md` → "Leftovers for the next pass" is the
  **accepted backlog** — never re-raise those items, and do not widen scope to
  untouched files except where one makes a claim about a file that changed.
- **Report to the fixed contract.** Severity (critical / high / medium / low /
  nit) · `file:line` · the defect · evidence (an exact quote or command output) ·
  the fix · `verified` or `[INFERENCE]`; then one verdict — **ship** /
  **ship-with-fixes** / **blocked** — an explicit fix-now vs backlog split
  (fix-now = a defect in code this range changed), and what could not be verified
  and why.
- **Close-out needs the suite green on the frozen SHA.** A pass closes the review
  when it returns 0 critical, 0 high and 0 fix-now mediums in the changed code
  **and** the runner's suite verdict is green on that revision — the numeric rule
  alone would close a pass whose suite never ran on the frozen revision. There is
  no CI here, so a red suite is a **blocked** pass, not a finding. Everything else
  is backlog, i.e. ordinary work; at most one further verification-only pass is
  allowed (verify the fixes, no new scope).
- **Fan out by risk tier, at most 8 concurrent agents.** Changed *logic* (deep,
  and allowed to run code paths — both highs of the third pass came from an agent
  executing the path rather than reading it), wiring/config (`mise.toml`, hooks,
  gates), docs/data (one batched agent, or leave it to `mise drift`). Eight is a
  round's budget, not a host ceiling: beyond it the host parks agents mid-flight,
  and a parked agent must be revived one message at a time. The parent is the only
  runner.
- **The pass writes its own marker** at the revision it reviewed: annotated
  `review-pass<N>`, its message the reviewed revision's own (so the tag reads as
  the work it froze), `N` taken by `sort -n` *after* stripping the prefix — never
  `sort` on the tag names, which is lexicographic and puts `review-pass10` before
  `review-pass5`. It is local bookkeeping and is never pushed; the next pass
  derives its base from it, never from `<target>^`, which resolves the previous
  *fix* tag and silently shrinks a verification pass to its last commit.

## Code Conventions & Common Patterns

- **Python/uv**: py≥3.15, type-annotated, package-relative imports
  (`from pgrag.config import …`). Edit the package in `src/pgrag/`, never
  hand-edit build artifacts (`documents.json`, `data/derived/wiki_parsed.json`,
  `bm25_index.pkl`, `tool_documents.json`, `tool_bm25.pkl`).
- **Doc identity contract**: every doc carries `id` + `metadata.source` +
  `metadata.table`. Preserve these fields — the store and the BM25 index are
  keyed off them.
- **One retrieval path.** The agentic tool loop is the only retrieval
  architecture. There is no dense path, no vector store, no reranker, and no
  query classifier to "restore"; a proposal that reintroduces one is a new
  architecture, not a fix. Inspect `src/pgrag/agentic/tools.py` and `loop.py`
  before proposing retrieval changes — the loop is blocking/bounded, and only
  the model's own tool rounds re-query.
- **Error handling**: tool errors are returned to the model as strings (data to
  self-correct), never raised out of `execute_tool`. The LLM client
  (`rag/llm.py`) raises domain errors with the URL in the message; offline
  tests assert these.
- **Wiki categories**: `TARGET_CATEGORIES` flat + `RECURSIVE_CATEGORIES`
  (Creatures d2, Items d1) — monsters/items only via recursion. Subcats bare
  (no `Category:` prefix). Uncategorized pages (racial-stat pages, badge hub)
  come from `WIKI_TITLE_EXTRAS` — category-driven discovery cannot see them.
  `Dungeons` is flat: all 52 dungeon pages are direct members, so its region
  subcats (`Dungeons by Area` → `Serbule Dungeons`, …) add nothing.
  Wiki filenames `{safe_title}_<sha256-8>.txt`; display names come only from
  `.meta.json` (never filenames). Sync ends with `remove_orphan_files`.
- **Test isolation**: temp dirs for anything touching `data/` (real
  meta/documents are guarded — a past bug silently destroyed
  `data/wiki/.meta.json`). `tests/conftest.py` snapshots `data/cdn`/`data/wiki`
  and asserts immutability. Fixture-based store tests pass
  `session_dir=`/`glogger_db=` pointing at absent tmp paths — the `build_store`
  defaults are LIVE sources (real game dir, real glogger DB), so a fixture that
  omits them ingests live data and races it.
- **Store-only goldens**: golden JSONs with `"store": true` (glogger
  play-history facts) are excluded from the corpus-side goldens and run through
  the tool loop like every other case — the loop reads the store, so there is
  no separate store-only harness anymore.

## Important Files

- `src/pgrag/cli.py` — entry point; `config.py` — constants/paths; `build.py` —
  document orchestration.
- `src/pgrag/agentic/glogger.py` — glogger play-history ingestion (WAL-safe
  snapshot copy → watermark-incremental mirror into the store;
  `DEFAULT_GLOGGER_DB` = `%APPDATA%/glogger.Personal/glogger.db` (`RELEASE_GLOGGER_DB`
  is the fallback) is what moves if
  glogger relocates).
- `scripts/rag_chat.py` — Gradio chat over the tool loop (port 7860).
- `scripts/curator.py` + `curator_scheduler.py` — heuristic (non-LLM) curation:
  regex-detect fragmented knowledge (area_levels, skill_trainers,
  crafting_progressions), write template docs to `data/wiki/curated/`,
  scheduler persists state to `data/curator_state.json` and rebuilds docs on
  change. Deterministic by design — no LLM, so curated docs are stable anchors.
- `scripts/golden_check.py` — fact-presence golden eval through the loop →
  `data/agentic_eval_report.json`; `scripts/golden_rerun.py` — quick named-case
  rerun + flaky focus (`mise golden-one`/`golden-flaky`; appends miss history to
  `data/golden/history.jsonl`); `scripts/agentic_tune.py` — tuning sweeps.
- `scripts/build_sqlstore.py` — SQLite store build; `scripts/build_tool_corpus.py`
  — tool-corpus variant; `scripts/publish_store.py` — private-bucket publish
  (`--docs` = `mise upload-docs`, the public corpus bucket);
  `scripts/agentic_chat.py` — agentic CLI chat (+ pre-chat sync).
- `scripts/review_facts.py` — the read-only facts sheet a review pass pastes into
  every reviewer brief (suite counts, gate verdicts, task set, freeze state,
  platform liveness, clone-invisible inputs); the protocol that consumes it is
  `docs/REVIEW.md` → "Review protocol".
- `docs/TEST_CONTRACTS.md` — layer→tests→contract map + regression-triage
  protocol (read before changing behavior/tests); `docs/REVIEW.md` — audit
  findings + improvement backlog.
- `scripts/services.py` — start/stop/status for the LLM + Gradio chat
  (cross-platform: stdlib only, `os.name`-gated, `ss`/`lsof`/`killpg` on POSIX.
  The `.mise/tasks/*.ps1` service scripts it replaced are gone — but `logs`,
  `tail-llm` and `golden-type` are still PowerShell run blocks and need pwsh).
- `mise.toml` `[env]`: `LOGS_DIR` (read by `scripts/services.py` via
  `os.environ`, falling back to `<root>/logs`), `LLM_MODEL`, `LLM_FLAGS` —
  update if paths move.

## Runtime/Tooling Preferences

- **Runtime**: Python ≥3.15 via `uv`; model binaries fetched with `hf` global
  CLI (cache `F:\AI\models\hub\`; GGUF via `-hf org/repo:quant`). Node (mise
  `[tools]`, with `npm`) exists for one thing: the `.omp/hooks` TypeScript gate
  (`mise lint-ts`, parser from `mise ts-deps`) — the repo ships no JavaScript.
- **Local services** (running on Windows host):
  | svc | port | model / note |
  |-----|------|--------------|
  | LLM | 8080 | `LLM_MODEL` (`[env]`; `LLM_FLAGS` carries launch tuning) — the one hard dependency; the tool loop answers through it. A running instance OOMs before start → `mise down` (alias of `stop-all`) first |
  | Chat (Gradio) | 7860 | primary UI — part of `mise start` (alias of `start-all`); `mise chat` foreground; loop-driven, refreshes the store at startup, needs LLM :8080 |

  Service control is `scripts/services.py`: `mise llm-start`/`llm-stop`,
  `chat-start`/`chat-stop`, `status`, `debug-llm`. The manager itself is
  cross-platform (the deleted `.mise/tasks/*.ps1` launchers were Windows-only)
  and every service task is a plain `uv run python …`, so the task set runs on
  POSIX too; only `logs`/`tail-llm`/`golden-type` pin `pwsh`.

  Embed (:8081) and reranker (:8082) services are **retired** — start tasks
  and env keys were deleted with the dense path.
- **Single-source models**: refs + tuning flags live once in `mise.toml [env]`
  (`LLM_MODEL`/`LLM_FLAGS`); consumed by `scripts/services.py` and
  `scripts/agentic_tune.py` (`os.environ` — the launch path and the `ornith`
  sweep row / `--llm-restart` argv). `scripts/check_docs.py` reads `[env]`
  (tomllib) and fails if any of `scripts/services.py`, `scripts/agentic_tune.py`
  or this file hardcodes a model literal value — that trio is the consumer set
  `mise drift` enforces (the gate itself is not a consumer). Tuning rationale:
  `docs/LLM_MODEL_WIRING.md`.
- Tests import the installed `pgrag` package — after changing `src/pgrag/`, no
  reinstall needed (editable install).

## Testing & QA

- **Framework**: pytest via `uv run pytest`; the counts this doc quotes are
  **584 collected / 78 deselected / 506 default** (asserted by `mise drift`
  against a real collection) — the 78 being 16 data-heavy tests + the 62
  parametrized golden cases, so the default offline run leaves **506** (one POSIX-only port-owner case skips on Windows) — all
  offline (temp-dir integration, mocked servers). The golden gate is the only
  suite that needs any server.
- **Golden eval** (`tests/test_golden_check.py`): parametrized over
  `data/golden/*.json`; the `require_llm` fixture skips unless the LLM
  (:8080) is up — **LLM only**, nothing else. Each case runs through
  `pgrag.agentic.eval.run_case` → `run_loop`; retries once (2 attempts) to damp
  reasoning-ON nondeterminism; both attempts fail = regression.
  `EVAL_GENERATION = {"temperature": 0, "seed": 0}` is wired into every run; reasoning-ON
  decoding is not greedy-pinned even at temp 0 + seed (observed 7/8/10
  single-run variance was **LLM-side**, not a harness miss), so the retry-once
  is the harness's defense. For a byte-reproducible comparison, launch the
  server with `--reasoning off`. See `docs/LLM_MODEL_WIRING.md` → "Card flags &
  determinism".
- **Golden CLI**: `mise golden` (all 62 files through the loop, exit 1 on
  missing facts), `mise golden-short` (`--tier short`, 8 cases),
  `mise golden-one <id>` (named cases), `mise golden-type <type...>`. `--tier`,
  `--ids`, `--type`, `--max-facts`, `--diagnose`, `--export`, `--corpus`,
  `--max-rounds`, `--store`, `--report` are the `golden_check.py` flags.
  Per-case cost is dominated by the reasoning model's rounds — a store-only
  case with 1-2 rounds answers in tens of seconds, but a 5-6 round wiki case
  takes minutes, so a full 62-case run is a long background job, not a
  foreground watch. `--diagnose` labels a miss GEN-side (fact was in the fed
  context, the model dropped it) vs RET-side (never retrieved) from the
  `documents`/`trace_rounds` the loop returns.
- **Store-only cases** still run through the same loop (`--store` points at
  `data/sqlite_gorgon.db`); there is no separate store-only harness.
- **No retrieval-regression set.** The L3/L4 suites of the *pre-cutover* map
  (`test_retrieval_unit.py`, `test_rerank*.py`, `test_retriever_spelling.py`,
  `test_where_filter.py`, entity/query-plan tests) were deleted with the dense
  path — `L3`/`L4` in today's map are the tool loop and the SQLite store, both
  live. What remains for retrieval is the agentic layer: `test_agentic_loop.py`,
  `test_agentic_tools.py`, `test_bm25.py`, `test_bm25_persist.py`.
  `docs/TEST_CONTRACTS.md` is the authoritative map — read the L-agentic and
  BM25 layers before touching retrieval.
- **Test edits are contract changes.** Editing what a test *asserts* is not a
  workaround — state the new contract in the test, run the layer's sibling
  suite (`docs/TEST_CONTRACTS.md`), and never hand-edit a build artifact
  (`documents.json`, `bm25_index.pkl`, `tool_documents.json`,
  `tool_bm25.pkl`, `wiki_parsed.json`) to satisfy a test. If the behavior
  didn't legitimately change, the failure is a source regression: fix the
  source, not the test.
- **Lint gate** (`tests/test_lint.py`, layer L8 in `docs/TEST_CONTRACTS.md`):
  `uv run ruff check src scripts tests` must be clean — a failing check fails
  `uv run pytest`. Run `mise lint` after editing code (`mise format` applies the
  safe autofixes: import sort, unused imports, f-strings, formatting). Rules
  live solely in `ruff.toml`; the per-file-ignores there (scripts: unused
  unpack/vars; tests: unused unpack) are deliberate. New rule
  ⇒ fix all findings in the same commit.
- **TypeScript gate** (same `tests/test_lint.py` / L8 layer): every JS/TS file
  the repo ships must parse — the pre-command hook (`.omp/hooks/**`) and the
  gate's own `scripts/*.mjs`. `mise lint-ts` (`node scripts/check_ts.mjs`) exits
  1 on any diagnostic; the one expected *note* is `TS2307` for the
  `@oh-my-pi/pi-coding-agent` import, whose types ship only inside the compiled
  `omp` binary, so the hook's own API use cannot be typed here. `mise install`
  provides node (`[tools]`, pinned like python/ruff) and `mise ts-deps`
  (`npm install`) the parser devDependency
  (`node_modules/` is gitignored; without it the layer's two TS tests skip
  loudly instead of failing). The hook is the cross-instance lock's early
  warning, so a `.ts` edit that stops it loading is a real regression — nothing
  else in the suite reads TypeScript.
- **Compliance gate** (`tests/test_constraints.py`, layer L9 in
  `docs/TEST_CONTRACTS.md`; policy doc `docs/research/pg-rag-game-interaction-limits.md`):
  `scripts/check_constraints.py` must scan
  the working tree clean, cover every documented tree (including its two
  self-exclusions), report a planted violation in each family, and warn loudly
  when skipped. `mise compliance` (alias `comp`) runs it standalone, `mise test`
  runs it before pytest, and `.githooks/pre-push` runs it on a provisioned
  checkout (`core.hooksPath=.githooks` is local config — a fresh clone re-sets
  it). There is no CI.
- **Key suites** (from `tests/`): `test_documents.py` (doc shape),
  `test_chunking.py`, `test_bm25.py`/`test_bm25_persist.py` (lexical index +
  persistence), `test_llm.py`/`test_connectivity.py` (LLM client SSE + server
  errors), `test_agentic_tools.py`/`test_agentic_loop.py` (the five tools + the
  round protocol), `test_sqlstore_build.py`/`test_session_ingest.py`/
  `test_glogger_ingest.py` (store ingestion), `test_golden_check.py` (the
  golden gate), `test_services.py`/`test_publish_store.py` (service control +
  the two publish paths), `test_download_wiki.py` (batching, redirects, orphan
  cleanup — patches `META_FILE`+`WIKI_DIR` to tmp).
- **Coverage expectation**: one test defends each observable contract;
  integration tests use temp dirs and mocked servers rather than live ones.
