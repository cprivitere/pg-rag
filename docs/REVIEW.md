# REVIEW — pipeline, tests, and documentation audit (verified)

Date: 2026-08-21 (findings) · 2026-10-09 (retrieval cutover record).
Companion artifact: `docs/TEST_CONTRACTS.md` (contract map + triage protocol).

**Scope note.** This record predates the 2026-10-09 retrieval cutover and is
edited, not preserved verbatim: findings and backlog items whose subject was
deleted (the dense path, the embeddings bakeoff, the reranker, the query
classifier, the IR-metric harness, the pipeline test set) are removed rather
than left as history about code that no longer exists. The cutover and its open
follow-ups are recorded at the end.

## Correction (v1 → v2) — reading beats summarizing

The first version of this review shipped two headline findings sourced from
agent summaries that were not verified against the test files:

1. "`test_bm25.py` and `test_retrieval_unit.py` both own five identical BM25
   tests" — **false.** The five BM25 tests live only in `test_bm25.py`. An
   exact-name scan of all `def test_*` across the suite found exactly one name
   collision (`test_summary_shape`).
2. "The `DOCUMENTS_VERSION` refusal has no direct test" — **false at the time.**
   `test_build_index.py::test_documents_version_refuses_stale` asserted it. (That
   test was deleted with the dense path; the refusal now lives in
   `scripts/build_tool_corpus.py` and is no longer directly tested — see the
   cutover follow-ups.)

Both errors came in because the finding matched the expected narrative. That is
the failure mode the engagement exists to stop, and it validated the core
recommendation before any code was touched: **facts about this repo must be read
from source lines, not inferred from summaries that fit a prior.** No test was
deleted or altered on a false premise, and the brittle "assert
`DOCUMENTS_VERSION == 4`" test proposed in v1 was dropped.

## Ground-truth rule (survives every cutover)

Facts go into `docs/TEST_CONTRACTS.md` / this record only after being verified
by reading the exact source/test lines. A summary or peer judgment that "fits
the expected story" is not evidence — an unverified finding encoded into a
guardrail becomes a wrong guardrail that future runs trust.

## Findings that survive

### Tests

- **One true name collision** was fixed: `test_summary_shape` appeared in
  `test_summaries.py` (asserts `build_summary_documents` shape) and
  `test_gathering_summaries.py` (asserts `build_gathering_summaries` shape).
  Renamed to `test_cdn_summary_shape` / `test_gathering_summary_shape` and given
  per-file module docstrings naming each file's builder. Both files still exist;
  the collision fix stands.
- **Isolation and coverage remain strong**: session immutability guard
  (`tests/conftest.py`), tmp-dir patches for wiki/derived paths, mocked servers,
  keys-only shape asserts, deterministic data.
- **Module docstrings** were added across the suite so a fresh model reads each
  file's single contract before the test bodies. The surviving files still carry
  them.

### Documentation & comments

- `AGENTS.md`, `.omp/RULES.md`, `.omp/WATCHDOG.md`, the skills: substantive and
  consistent.
- **`scripts/` is operational documentation.** `golden_check.py` parses
  `{id, question, type, facts}` with any-variant-substring pass; `rag_chat.py`
  (port 7860, LLM :8080) matches.
- **Curator is a documented heuristic stub, not LLM curation** (chosen, not
  deferred): `curator.py` states its deterministic-template nature — stable,
  offline anchors. LLM curation is deliberately rejected (it would break the
  suite's determinism and offline-test ethos).

## Backlog that survives

- **Golden coverage** is intake-gated on a *measured* pipeline gap plus explicit
  user confirmation (workflow in the `evaluation` skill); prefer filling thin
  buckets over near-duplicates of passing cases.
- **Pricing surfaces**: other players' stall observations landed 2026-10-15
  (`stall_price_observations` is read by `player_state section=stall` and named
  in the loop's schema note; all 4 live rows are `price_unit 0` = price
  pending, so the block says so). Player work orders (see `TODO.md`) remain an
  unimplemented store surface — no ingest or parser exists.
- **Glogger-driven first-class access** (a glogger build with this as a tab)
  remains an idea, not work.

---

## Cutover record (2026-10-09) — one retrieval path

The repo consolidated onto a single retrieval path: the **agentic tool loop**
(`pgrag.agentic.loop.run_loop` driving `sql_query`/`find_entities`/`get_page`/
`corpus_search`/`player_state` over `data/sqlite_gorgon.db` + the BM25 corpus).
The dense path was deleted outright: `pgrag.rag.{pipeline,retriever,
reranker_client,entity_retrieval,resolve,synthesis_detector,synthesis_generator,
spelling,query_classifier,query_plan,retrieval_eval,prompts}`,
`pgrag.vectorstore.*`, `pgrag.embeddings.*`, `pgrag.validation`,
`pgrag.documents.prompts`, their scripts, the embed/rerank service tasks, the
molab mirror notebook, and their tests. Chroma is out of `pyproject.toml`.

The prose layer (`AGENTS.md`, `README.md`, `docs/*`, the skills, `.omp/*`,
`TODO.md`) was rewritten to match; `scripts/check_docs.py` (`mise drift`) is the
guardrail that keeps it truthful.

### Open follow-ups (honest, not celebratory)

- **Parity verification** — latest full run (2026-10-12, 61 cases, 2003 s, 0
  error records): **59 PASS, 2 FAIL, 4 missing facts** — the residual pair is
  `gardening-basics` + `gardening-trainers-and-fertilizer`, i.e. the loop
  tool-choice item in `TODO.md`, not a coverage or phrasing gap. The earlier
  baseline follows. Measured 2026-10-11, full `data/golden` set through
  `run_loop` (`mise golden`, 62 cases, 2121 s, 0 error records): **55 PASS,
  7 FAIL, 16 missing facts**. Every missing fact exists in both
  `data/documents.json` and the store's `wiki_pages`, so none is a coverage
  gap; 12 of the 16 facts were not in the context the model received
  (RET-side), 4 were present and omitted (GEN-side). Reported failures mix
  three things: golden variants that pin one of several valid answers (e.g.
  `first-aid-kit-recipe` expects Toolcrafting 55 while the corpus also holds the
  FirstAid-40 combo method, which the answer gave), qualitative-phrase facts
  (`sword-vs-unarmed-burst` answers with the damage numbers instead of the
  pinned phrasing), and sub-fact omission. Individual cases flip between runs
  (the same case PASSes in one 10-case run and FAILs in the next), which is the
  documented reasoning-ON single-sample variance; the deleted pipeline had a
  worse record on the same hard cases (`royal-jelly-gathering` missed 12/12).
  Until then, the model/flag leaderboard in `docs/LLM_MODEL_WIRING.md` is
  explicitly historical (measured on the old harness).
- **Known loop defects found by the gate and fixed** — duplicate
  `LLMServerError` (callers' `except` never fired), `get_page` dropping infobox
  params (location/level facts), prompt letting the model defer to a page,
  truncated answers (`finish_reason=length`) returned as final, HTTP errors
  escaping as raw `HTTPError` and killing a batch, and malformed **native**
  tool-call JSON (server responds `HTTP 500 Failed to parse tool call
  arguments`, seen on 1/62 cases) now retried once without native tools, which
  stay off for the rest of the run. Each has a regression test
  (`test_agentic_tools.py`, `test_agentic_loop.py`,
  `test_golden_check.py`).
- **Freshness guard coverage** — resolved: `tests/test_build_tool_corpus.py`
  asserts all four arms of the `DOCUMENTS_VERSION` refusal (current / stale /
  missing docs / missing stamp) against tmp paths.
- **Golden runtime** — measured: 62 cases in 2121 s (~35 min) on a warm local
  LLM; per-case cost is dominated by the reasoning model's rounds (a 1-2 round
  store case is tens of seconds, a 5-6 round wiki case minutes). `mise golden`
  is a background job; `mise golden-one`/`golden-short` are the fast paths.
- **Orphaned/stale artifacts** — resolved 2026-10-11: `evaluation/queries.jsonl`
  (tracked, no consumer after the IR harness died), `data/chroma/` (1.3 GB),
  `data/bakeoff_*.json`, `data/embed_vram.json`, `data/rerank_stats.json`,
  `logs/embed.log`, `logs/rerank.log` and stray `data/tmp_*` scratch were
  deleted. `data/retrieval_traces/` is NOT stale — `golden_check.py` (with
  `PGRAG_TRACE=1`) and `golden_rerun.py --trace` write loop traces there.
- **`notebooks/molab-agentic/README.md`** — resolved: the mirror-notebook
  references are gone (only notebook left) and the loop's new return keys are
  documented.
- **molab first-boot numbers** — resolved: `docs/MOLAB_OPS.md` records the
  sidecar's `TOOL_PARSER`/`NO_TOOL_PARSER` reporting, the ~3–4 min first boot
  (weights 4 s / compile ~70 s / CUDA-graph capture ~90 s, ~40 s warm), MTP-3
  decode at 60.5/72.5 tok/s, and the live tool-loop run at 24576 context
  (**3 rounds, 15.8 s**, answer grounded in the stall ledger).

### Follow-on (2026-10-11)

- **Python 3.15.** `requires-python`, `.python-version`, `mise.toml [tools]` and
  the docs moved to 3.15; the lock re-resolved (32 packages, chromadb-free) and
  the venv was recreated on 3.15.0. Verified: 315 offline tests green, ruff
  clean, drift 0, compliance OK, and the loop answers through the LLM on 3.15
  (golden `bacon-for-joeh` + `stall-price-basics` PASS, 61 s).
- **Sandbox-parse regression (found and fixed).** Consolidating `LLMServerError`
  made `loop.py` import `pgrag.rag.llm`, which used PEP 758 `except A, B:` —
  valid on ≥3.15 but a SyntaxError on the molab 3.13 kernel, i.e. the published
  notebook would have failed to import the loop. All eight such handlers in the
  shipped `src/`+`scripts/` were parenthesized and
  `tests/test_lint.py::test_shipped_source_stays_python_313_parseable` now gates
  the invariant.
- **Cleanup.** `data/chroma` (1.3 GB), `bakeoff_*.json`, `embed_vram.json`,
  `rerank_stats.json`, `evaluation/queries.jsonl`, `logs/{embed,rerank}.log` and
  stray `data/tmp_*` scratch deleted; `mise upload-store` re-published the store
  snapshot + source tarball to the private bucket.
- **Golden-set hygiene + consolidation (2026-10-11).** 8 cases deleted as
  redundant (set 62 → 54): `blacksmithing-leveling-25-30` (shape survives in
  `cheesemaking-leveling`, which also checks the arithmetic; that case split
  into `cheesemaking-leveling-xp` + `cheesemaking-leveling-recipes` on
  2026-10-15),
  `fireball-vs-fire-breath` (identical facts to `-damage`),
  `riding-trainer-bands` (same shape as `animal-handling-trainer-bands`),
  `mycology-skill`, `mushroom-farming-gathering`, `grow-field-mushrooms`,
  `mushroom-farming-substrates` (mushroom coverage narrowed to two cases,
  keeping the substrate mechanic), `tailoring-practice-leveling`,
  `mastery-spark-orbs-price`. 9 cases rewritten so the question asks every
  dimension its facts check (the flip class): `ratkin-root-tender-location`,
  `first-aid-kit-recipe` (now requires **both** crafting routes),
  `gazluk-keep-dungeon-level` (level fact added — it checked none before),
  `dungcrafting`, `gardening`, `sword-vs-unarmed-burst`,
  `meditation-synergy-skills`, `animal-handling-ability-audit`,
  `recipes-using-basic-spider-silk`, `healing-potion-omega` (ingredients added),
  `moonstone-item` (crafting use added). `SHORT_TIER_IDS` rebuilt around the
  surviving cheap representatives. Rule recorded in the evaluation skill.
- **Fact-density splits (2026-10-11).** The 4 cases that still failed the
  post-hygiene full run (13 missing facts, 8 GEN-side / 5 RET-side) were
  fact-dense long answers, not phrasing problems: `dungcrafting` and `gardening`
  each required ~10 facts scattered across several docs, and the store audit
  required 5 names. Each was split into two focused cases, one per source doc /
  audit dimension — `dungcrafting` → `dungcrafting-basics`
  (skill_Pooping description + parent + food-quality note) +
  `dungcrafting-poop-ability-and-rewards` (skillprofile_Pooping ability, Beast
  requirement, Cow/Pig rewards, Graffiti quest); `gardening` →
  `gardening-basics` (description, non-combat, Foraging/Rabbit, Spade Assault) +
  `gardening-trainers-and-fertilizer` (three trainers + the from-Animal-Feces
  recipe, one doc); `animal-handling-ability-audit` →
  `animal-handling-known-abilities` (TameRat + the Unknown-bucket CharmRat) +
  `animal-handling-family-tames` (Tame Bear / Tame Big Cat / Tame Bee in the
  audit's KNOWN family list); `royal-jelly-gathering` →
  `royal-jelly-what-is-it` (item_1337) + `royal-jelly-obtaining` (Midge's barter
  table row `Phoenix Egg x 1 | Royal Jelly x 6` + quest sources). Set 54 → 58
  files, same wall time. Findings while splitting: `gardening`'s `giant mantises`
  fact was dropped (no Gardening-grounded source — it was an incidental-string
  assertion), and the old audit note claiming TameBear/TameBee/TameBigCat are
  NOT known is wrong: the live sheet's Animal Handling ability list contains all
  three (they are shrine/riddle-gated in game, which `abilities.level_req` does
  not show); the cases now assert what `player_state abilities` actually prints.
  Each split case was verified twice before the LLM run: a hand-written perfect
  answer passes the matcher, and every fact variant was asserted present in its
  named source doc (or the live tool output).
- **`corpus_search` snippet bug (2026-10-11, fixed).** Verifying the splits
  exposed a real reachability bug in the only retrieval path: `_SEARCH_SNIPPET`
  showed only the first 400 chars of each hit, and a corpus doc has no other
  reader (`get_page` serves wiki titles, returning a near-empty error for ids
  like `skill_Pooping`), so every fact past the cut was unreachable. Measured
  offsets: `skillprofile_Pooping_chunk_0` (871 chars) hid `Requires Beast` at 788
  and the gourmand note at 451; `skillprofile_Gardening_chunk_4` (1089) hid its
  trainer list at 703-830 — exactly the facts the split cases were missing.
  Raised to 1000 (10 hits ≈ 10.6K, inside the loop's 12000-char payload cap),
  with a regression test
  (`tests/test_agentic_tools.py::test_corpus_search_snippet_reaches_doc_tail`,
  the first offline coverage of `corpus_search`); at 400 the test fails, at 1000
  it passes. Effect: both `dungcrafting-*` cases went 6 rounds/FAIL → 3-4
  rounds/PASS in the same session.
- **Residual two (2026-10-11).** `gardening-basics` and
  `gardening-trainers-and-fertilizer` still fail most runs, on a different fact
  subset each time, because the model answers skill questions from the CDB
  `skills`/`abilities` tables and the wiki level-cap trainer page instead of the
  corpus profile doc — it once asserted "Gardening grants no bonus levels to any
  other skill". Their facts are provably retrievable (a natural trainer query
  returns ten `wiki_Gardening_table_0_row_*` hits naming Floxie/Tavilak/Therese),
  so the remaining fix is loop tool-choice guidance, not eval hygiene. Both ids
  are in `data/golden/FLAKY.txt`.
- **Gate policy (resolved 2026-10-11).** Pre-hygiene full-set parity was 55/62,
  with residual failures split between golden hygiene (variant-pinned answers,
  phrasing) and reasoning-ON single-sample variance. Both halves are now
  addressed: the hygiene class was refactored, the density class split (above),
  and the reasoning-off dev gate is **implemented** —
  `scripts/services.py` appends `--reasoning off` when `LLM_REASONING=off`
  is set (it prints the mode so the task output records it), documented in
  `docs/LLM_MODEL_WIRING.md` ("Dev gate vs acceptance gate") and in the
  evaluation skill. Assembly verified (script parses; the command line gains the
  flag only in that mode); the live restart was not exercised because the
  running server serves the chat UI. Post-split full run: 50/54 → see the next
  run for the 58-file number.

---

## Follow-up fix pass (2026-10-17) — the `479b6bc` task/script audit

Reviewed `479b6bc` ("audit mise tasks + scripts") — then the three-commit range
(`479b6bc` + `a8fcf27` + `2af6bc5`) in a second, PR-style round whose findings
are folded in below. Everything the reviews found is fixed; the carry-overs named
in the first pass are addressed in the second.

- **Corpus-build lock redesigned** (the review's only P1 pair). The pre-hook
  (`.omp/hooks/pre/00-repo-lock.ts`) is now **probe-only**; the *writers* own
  `data/.omp-build.lock` through the new `src/pgrag/buildlock.py` —
  `pgrag build-documents` (`src/pgrag/cli.py`), the BM25 rebuild/persist in
  `src/pgrag/rag/bm25.py`, and `scripts/build_tool_corpus.py`. Two reasons: a
  hook's lifetime is one tool call while a backgrounded build outlives it
  (observed live — the old lock was released at the ~60 s auto-background while
  the build kept running), and the matcher missed *documented* spellings
  (`mise run --force syf`, `mise run <task> ::: syf`, glued `-m pgrag.rag.bm25`)
  which the writer-side lock now covers regardless of invocation. Layer **L11**
  in `docs/TEST_CONTRACTS.md`; `tests/test_buildlock.py` +
  `tests/test_build_tool_corpus.py` pin it (cross-process refusal verified live).
- **`scripts/services.py` hardened**: strict-UTF-8 child output read with
  `errors="replace"` (a non-UTF-8 process name previously killed `stop`/`start`
  with `AttributeError` — reproduced), POSIX `killpg` gated on pidfile
  provenance (a port-derived pid, `pid <= 1`, or the caller's own group is
  signalled alone with a warning), atomic pidfile write, non-positive pid cells
  rejected, `LOGS_DIR` honored via `os.environ`.
- **Docs/config made truthful**: `mise.toml` no longer pins pwsh globally (only
  `logs`/`tail-llm`/`golden-type` do, so the service tasks really are runnable
  on POSIX), the `[env]` consumer lists include `scripts/agentic_tune.py`,
  AGENTS/TEST_CONTRACTS/testing-skill names and counts were corrected
  (**503 collected / 78 deselected / 425 default** after the L11/L12 tests
  landed), the L4 Change ⇒ command runs
  its new `test_publish_store.py`, and the orphan
  `tests/fixtures/retrieval_cases.json` (a dense-path leftover) was deleted.

**Carry-overs resolved (2026-10-17, second pass).**

1. `scripts/check_docs.py` now resolves backticked
   `scripts|src|tests|docs|.mise/tasks` pointers over **`.py`, `.sh` and
   `.ps1`** — with `-` in the name class, because the deleted names are
   hyphenated (`execute-code.ps1`, `dxgi_vram.ps1`) — and resolves a skill's own
   `scripts/…` reference next to its `SKILL.md` (which is how the molab skill
   names `execute-code.ps1`). Split into testable helpers (`_doc_refs` /
   `_missing_doc_refs` / `_all_missing_doc_refs`); the expected-skills list is
   now derived from the tree instead of a five-name literal that silently
   skipped `molab-notebook`. New layer **L12** (`tests/test_check_docs.py`) pins
   the widened capture, a vanished pointer being reported, skill-local
   resolution, and the real AGENTS.md + skills blob staying clean. Verified
   `mise drift` → 22 OK / 0 drift with the wider scan. Note the first round's
   claim that the widening "let 12 deletions pass" was too broad: only the four
   `scripts/*` ones were ever in the scanned prefix space.
2. `.agents/skills/molab-notebook/scripts/execute-code.ps1` pointed at
   `discover-servers.ps1`, which never existed in the skill; the URL/token flow
   is its sibling `molab-connect.ps1` (`-Pasteline "<block>" -Save`, then
   `-Info` for the url + kernel probe). Both the header comment and the Usage
   text now name it, and its long-advertised `-Session` parameter now exists
   (it was in the Usage text and in every branch of the body, but not in the
   param block, so `-Session` hard-errored). The `scripts/molab_vllm_launch.sh`
   the molab skill references **does** exist (checked) — it was never part of
   this backlog.

**Second-round findings on the lock + writers (all fixed, all pinned).**

- **`BuildLocked` no longer escapes the tool loop.** `corpus_search` catches it
  and returns an error string: `run_loop` calls that tool for its SEED, so an
  escaping exception killed the whole answer instead of letting the model fall
  back to `sql_query`/`find_entities` (`tests/test_agentic_tools.py`).
- **Dead-pid locks are recoverable on win32.** `os.kill(dead_pid, 0)` raises
  `OSError(22 / winerror 87)` there, not `ProcessLookupError`, so the old
  `except OSError: assume alive` left a crashed writer's lock in place for the
  full 3 h TTL. Measured, mapped to "dead" (EINVAL), and pinned with a real
  impossible pid (the old test used pid 0, which short-circuited above `os.kill`
  and masked it).
- **The steal is identity-checked**, and both unlinks tolerate an OS refusal:
  a lagging stealer can no longer delete a lock a peer just re-created, and a
  win32 `WinError 32` (a peer holding the file open) on the *release* path no
  longer crashes a build that finished successfully.
- **`--docs` publishes the `DOCUMENTS_VERSION` stamp** and validates the corpus
  (JSON list + stamp match) before the first byte: the molab sandbox builds its
  tool corpus with the tarball's own `build_tool_corpus.py`, which refuses to run
  without the stamp — the corpus alone left a fresh sandbox unable to reach the
  tool loop. The notebook now fetches both via `.part` + rename, so a truncated
  download cannot poison its cache.
- **BM25**: an explicit `python -m pgrag.rag.bm25` build fails fast on a peer's
  lock (`lock_wait=0`) instead of sitting out the 30 s in-request wait, and the
  pickle is written to a temp file + renamed (the cache-hit read happens before
  the lock, so in-place writes could hand a reader a torn pickle).
- **`services.py`**: POSIX permission errors in the kill path, the Windows
  `pid <= 1` guard, bounded child reads (a wedged probe can no longer hang
  `stop`), ASCII-only pid cells, dead 503 branch removed, and the
  `LLM_REASONING` dev gate is case-insensitive like the PowerShell it replaced.
