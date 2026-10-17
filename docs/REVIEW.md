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
  `tests/test_build_tool_corpus.py` pin it in-process (the cross-process
  refusal was observed by hand, not by the suite).
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
  (re-pinned after the L11/L12 tests landed; the third pass below carries the
  current numbers), the L4 Change ⇒ command runs
  its new `test_publish_store.py`, and the orphan
  `tests/fixtures/retrieval_cases.json` (a dense-path leftover) was deleted.

**Carry-overs resolved (2026-10-17, second pass).**

1. `scripts/check_docs.py` now resolves backticked
   `scripts|src|tests|docs|.mise/tasks` pointers over **`.py`, `.sh` and
   `.ps1`** — with `-` in the name class, because hyphenated names are exactly
   what the prefix space carries (the deleted `dxgi_vram.ps1`, the surviving
   `molab-connect.ps1`) — and resolves a skill's own
   `scripts/…` reference next to its `SKILL.md` (which is how the molab skill
   names `execute-code.ps1`). Split into testable helpers (`_doc_refs` /
   `_missing_doc_refs` / `_all_missing_doc_refs`); the expected-skills list is
   now derived from the tree instead of a five-name literal that silently
   skipped `molab-notebook`. New layer **L12** (`tests/test_check_docs.py`) pins
   the widened capture, a vanished pointer being reported, skill-local
   resolution, and the real AGENTS.md + skills blob staying clean. Verified
   `mise drift` → 22 OK / 0 drift with the wider scan. Note the first round's
   claim that the widening "let 12 deletions pass" was too broad in the other
   direction: of the 22 files `479b6bc` deleted, the `.py`-only pattern already
   matched the 10 `scripts/*.py` ones, and the widening was what covered the
   remaining 12 — the four `scripts/*` `.ps1`/`.sh` and the eight
   `.mise/tasks/*.ps1` (the `.mise/tasks` prefix is precisely what it added).
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

**Third pass (2026-10-17) — the 61-file PR review, every finding fixed.**

Sixteen reviewers covered the range `479b6bc..d048ce3` slice by slice; every
finding below is fixed and pinned, and the suite is green on the tree this record
was written against (**584 collected / 78 deselected / 506 default**, one
POSIX-only port-owner case skipping on Windows; the pass-5 follow-up at the end
moved these from 568/78/490) with `mise lint`, `mise drift` (0 drift) and
`mise compliance` clean.

- **The molab stamp never reached the builder (the review's P1).** `corpus_docs`
  fetched `data/derived/documents_version.json` next to the corpus, but
  `build_tool_corpus.py` read `pgrag.config.DOCUMENTS_VERSION_FILE`, which is
  anchored at the *imported package root* — in the sandbox that is
  `pg-rag-src/data/derived/…`, so the refusal aborted every in-sandbox build
  (reproduced: exit 1, `check=True` → the `pgrag_src` cell raised and the tool
  loop stayed unreachable). The stamp is now addressed **from the CWD** like
  `SRC`/`OUT`/`PKL` (identical in the repo, where every task runs from the repo
  root), pinned by a subprocess test that builds in a CWD-rooted tmp data tree
  *and* a discriminating one that proves the CWD stamp is the file consulted.
  The cell also stages both files and promotes them as a pair, so a missing
  stamp can no longer leave a fresh 185 MB `documents.json` behind; the docs
  attribute the build to `pgrag_src` (it was never `corpus_docs`).
- **A corrupt lock could wedge every writer.** `read_owner` caught only
  `OSError`, so a non-UTF-8 record raised `UnicodeDecodeError` out of
  `corpus_lock` (reproduced) — and `corpus_search` catches only the lock error,
  so it killed the answer too. Non-UTF-8 bytes are now judged corrupt and stolen
  like any other bad record, and a lock path that cannot be created at all
  raises the new `BuildLockUnavailable` (`BuildLockError` base) instead of
  claiming contention with a remedy that cannot work — the tool catches the base
  class and returns an error string.
- **`mise drift` had lost the canonical-skill guard.** The skills list became
  tree-derived, which made the `missing skill` branch unreachable: deleting
  `.agents/skills/pg-rag/` reported zero drift. The list is now
  `CANONICAL_SKILLS` ∪ tree-derived (both directions documented), pinned by a
  test and by an in-memory proof that a deleted canonical skill still reports.
- **`--model current` silently dropped a swept reasoning budget** (the docstring
  introduced in this branch claimed the opposite, then contradicted itself).
  `current` and `ornith` now both resolve the live `[env]` pair, so the direct
  spawn path really applies `--reasoning-budget`; a restart that does not reach
  the requested state aborts the run instead of persisting mislabeled rows, and
  the post-sweep restore re-asserts the production pair explicitly. The health
  probe builds its URL from `LLM_PORT` instead of a literal.
- **The pre-hook could block or mis-probe.** It ignored the bash call's own
  `cwd`, so a build in another checkout was blocked by the session repo's lock
  (and the reverse lost the warning) — reproduced both ways in-process. The mise
  flag group was exponentially backtracking (measured 21 s for 44 flag tokens at
  a 30 s fail-closed timeout) and is now a bounded token skip: 0.016 ms at 4000
  flags, with 26-command detection equivalence against the old pattern.
- **`stop` waited out the grace window and dropped the pidfile it never acted
  on** (`taskkill`'s return code was discarded): `_kill_tree` now returns a
  verdict, the poll runs only after a real signal, and a refused kill keeps the
  pidfile and says nothing was signalled. The `start` spawn contract
  (env merge / `cwd` / `start_new_session` / `creationflags`) is pinned for
  background *and* foreground, and `debug-llm`'s Ctrl-C exits cleanly.
- **`curator_scheduler.py` called a dead subcommand** (`pgrag.cli build-index`,
  argparse exit 2 → the state file was never saved, so the curator re-ran every
  invocation). It now runs the two derived corpora that exist
  (`scripts.build_tool_corpus`, then `pgrag.rag.bm25`) in the same gated chain,
  and the test asserts the whole ordered chain with no dead argv.
- **Publish/docs/counts**: the `--docs` stamp parse is guarded (a hand-edited
  stamp aborts actionably instead of raising `JSONDecodeError`); `AGENTS.md`'s
  model-consumer trio now names the set `mise drift` actually enforces; L12
  describes the enforced rule (a leaked literal *value*) and the coverage
  (the pointer scan, the ownership map, the wiring drifts and the count quote are
  unit-tested — see `docs/TEST_CONTRACTS.md` L12 — while the model-literal leak
  and the suite counts are asserted only under `mise drift`); the never-edit
  parse cache is named by its live path (`data/derived/wiki_parsed.json`) in
  RULES/testing/pg-data/TEST_CONTRACTS and `tests/conftest.py`; `pyproject.toml`'s
  golden counts (62/8/54) match reality; `docs/LLM_MODEL_WIRING.md` names how to
  re-measure VRAM now that the probe scripts are deleted, and labels its MiB
  figures historical; the golden `notes` point at `mise golden-short` too; a
  stale `.tmp` scratch dir is gitignored.
- **Verified end to end**: `mise build-tool-corpus` against the real corpus
  (204,232 docs kept / 65,491 dropped, 6 s, lock released) proves the CWD stamp
  path and the cross-instance lock still work in the repo, not just in tmp.

## Leftovers for the next pass (found by the pass-5 review, deliberately not fixed)

Ordered by value, not severity. Each was verified by its reviewer; none is
blocking, and every one of them is in code the reviewer *executed* or traced, so
they are evidence-backed rather than suspicions.

1. **The molab corpus cache is existence-only** (`notebooks/molab-agentic/notebook.py`): a re-published
   corpus is never picked up, so a live sandbox builds from the old pair and then dies with "run
   `mise generate-docs`" — a task that does not exist there, with no stated remedy. Fix: probe the
   remote stamp on a cache hit (the store cell already probes its manifest) and re-download on
   mismatch.
2. **The tool-corpus freshness read races `build-documents`** (`scripts/build_tool_corpus.py`): the
   check runs *before* the lock by design, so it can observe new `documents.json` beside the old
   stamp and refuse spuriously. Also: its new `except OSError` in-build path is untested, the version
   numbers in the refusal message are unpinned, and the CWD-anchor test is vacuous on a fresh clone
   (`data/documents.json` and the stamp are gitignored — unlike `data/golden/*.json`, which is tracked).
3. **The BM25 lock covers the whole rebuild** (`src/pgrag/rag/bm25.py`): minutes of in-memory indexing
   are held exclusively with no re-check inside, so a peer that finished while we waited has its work
   redone; and nothing tests that the cache-hit path takes *no* lock.
4. **Service-manager hardening** (`scripts/services.py`): an unprovenance port owner is tree-killed
   (`taskkill /T /F`) on win32 while POSIX signals one pid; the pidfile `os.replace` can raise
   `WinError 5`; `_wait_dead` reads `EPERM` as "gone"; one test still creates `<repo>/logs`.
5. **Publish hardening** (`scripts/publish_store.py`): `_uri`/`_upload` default `bucket=BUCKET` binds
   the import-time constant while the guards read the runtime one; a crash between the two `--docs`
   uploads leaves a parsable stale pair the notebook cannot detect.
6. **`execute_tool`'s errors-as-data is still partial** (`src/pgrag/agentic/tools.py`): only
   `BuildLockError` is stringified; `FileNotFoundError`/`JSONDecodeError` from the same two call sites
   still escape the seed at `loop.py:779`.
7. **Curator scheduler** (`scripts/curator_scheduler.py`): its docstring's "every step is a live
   command" contract is not asserted (the test compares a hand-copied argv list), the rc≠0 →
   no-`save_state` path is uncovered, and its derived-corpus order differs from every `mise sync-*`
   task without saying why.
8. **Docstring debt about the deleted dense path**: `src/pgrag/documents/chunking.py` (module docstring
   and the `bge-small`/`build_index` sentences are present-tense; one clause is garbled),
   `tests/test_chunking.py:~321`, `tests/test_leveling.py:~206`, and `tests/conftest.py`'s "kept out of
   the source dir" claim (false for `data/wiki/curated`, and a stale `data/wiki/.parsed.json` sits
   inside the guarded dir).
9. **Pre-hook miss list**: `git -c alias.s=!mise syf S` and `git rebase --exec 'mise syf'` run a writer
   and pass the probe (documented class — the writer still locks); saying so in the header closes it.
10. **Two `mise.toml` nits**: `golden-type` is pwsh-pinned while the file claims the task set is
    POSIX-usable without PowerShell, and `golden-flaky-list`'s description renders a literal `%%`.



## Pass-5 follow-up (2026-10-17) — fixed findings of the frozen-tree review

The fifth pass reviewed the frozen tag `review-pass5` (`918f510`) against
`review-base-5ddd05b` — 18 files, one facts sheet, seven read-only slices, both
lenses. Verdict: **ship-with-fixes** (0 critical, 0 high, 8 fix-now mediums).
Every finding below is fixed and pinned; the tree is green at
**584 collected / 78 deselected / 506 default** with `mise lint`, `mise drift`
(0 drift) and `mise compliance` clean.

- **The facts sheet was wrong about four of its own facts.**
  `limits()` called the *tracked* `data/golden` fixture set "clone-invisible"
  (`git ls-files` never prints a directory; `_is_tracked` now accepts any tracked
  file under one). `liveness()` described the two probes backwards — `buildlock`
  is the weak `os.kill` side *by policy*, the pre-hook is the libuv side, and they
  deliberately diverge (measured: an exited child reads `alive` to `os.kill`,
  `ESRCH` to node). A bare `text=True` turned one non-UTF-8 byte into a `TypeError`
  that escaped every guard (now `encoding=`/`errors="replace"`, as `services.py`
  already did). And a red gate's row reported only its trailer, naming no finding.
- **The suite no longer runs the gates.**
  `test_review_facts.py::test_every_section_renders` called `rf.collect()`
  unstubbed, so the *offline* suite shelled out to ruff + drift + compliance and
  spawned a second `pytest --collect-only` inside the first (measured 4.0 s, 9
  spawns; the two share `.pytest_cache`). The module's `counts` and gate rows are
  stubbed, and its remaining facts are unit-tested.
- **The rest of the sheet's facts are honest**: a git that cannot answer is no
  longer reported as "untracked"/"clone-invisible" (`None` is its own verdict),
  `counts=None` renders as `(unavailable)`, mise's trivial string-task form no
  longer raises, and the suite and `mise lint-ts` are documented as *not* rows.
- **§8's predicate is ruff's, not a string prefix.** A selector resolves inside
  its linter, so `B` (flake8-bugbear) never covers `BLE001` — verified with
  `ruff check --select B` on a blind `except`, which lints clean, while
  `--select BLE` reports it. The old predicate judged every such family
  "selected", i.e. it accepted exactly the dead suppressions the check exists for
  (the "six of nine were dead" count is now what it computes).
- **§11 parses every summary pytest prints and treats a failed collection as its
  own verdict**: a legitimate 0-deselected (or all-deselected) config was a false
  "cannot count the test suite", and a broken collection's partial count was
  accepted and then blamed on the docs.
- **The layer map's parser follows the convention, not the markup**: all three
  bullet markers open a block, fenced examples are skipped, and a file named twice
  in one block is one owner. Each of those was a wrong verdict.
- **The pre-hook's new `MISE_EXE` had no leading `\b`**, so any word *ending* in
  `mise` before a corpus word reached the probe (`uv run python -c "print('promise
  docs')"`, and newly `compromise.exe syf`) — a wrong block while a peer builds,
  the one outcome the probe promises never to produce. All eleven writer spellings
  still probe; the four false positives do not.
- **`BuildLockUnavailable` says what happened.** Its message covered only "cannot
  create" while the class is now also raised for an ownerless record whose claim
  the filesystem refuses — where the file *exists* and removing it is the remedy.
  It now says "cannot acquire", and the inner text names that remedy.
- **`buildlock`'s docstring named a test that does not exist**
  (`test_win32_liveness_residuals`); `mise drift` drops `::`-selectors by design,
  so nothing else could see it. `tests/test_buildlock.py` now resolves every
  `tests/…::name` pointer in the module, and the liveness test's mechanism comment
  was corrected to the measured residual. The vanished-claim branch is pinned
  through the pause it skips (a refused steal whose file is gone must not burn the
  retry budget), and the peer fake returns a real record instead of a sentinel.
- **`agentic_tune` labels the budget the server served.** A *default*
  `--reasoning-budget` does not rewrite the live `[env]` flags, so with `[env]`
  carrying 8192 the row said `b4096` — the same mislabelling the `--llm-restart`
  verdict exists to prevent. `_served_budget` reads the served value (and falls
  back to the requested one when no `[env]` pair exists).
- **Docs made truthful**: the L8 map and AGENTS.md no longer enumerate the
  suppressions this range deleted; L12/L13 match the code they describe (the family
  rule, the dormant `lint.ignore` arm, the three docs in the count-quote set, the
  gate-summary format, "read-only" meaning *nothing git-visible*); REVIEW's own
  coverage clause, its `data/` gitignore parenthetical and its "this protocol in
  prompt form" claim were corrected; `/frozen-review` documents a fallback for an
  empty `$1` (this pass ran with an empty target) and its timings match
  measurement.

The ten items in "Leftovers for the next pass" above are untouched — they were
declared out of scope for this pass and remain ordinary work.

## Review protocol — how a pass is run now

Three review passes ran in a row on this PR, each fixing the previous one's
findings. The reason it took three is structural, and each part of the protocol
below removes one of those costs.

1. **Freeze first.** Commit the work in progress and tag it
   (`git tag review-base-<sha>`); reviewers get that SHA — never a dirty working
   tree, never `origin/HEAD...main`. Every fix is a new commit and a new tag.
   The reviewer-side half of the same instruction lives as a project slash command —
   `/frozen-review <tag|sha> <base-tag> [focus]` (`.omp/commands/frozen-review.md`) —
   which hard-stops on an already-dirty tree. The freeze/tag step *above* stays the
   operator's, and the command does not carry it (nor §4's two lenses): running the
   command on a dirty tree is a report, not a review.
   *Evidence*: the third pass's two high findings (a failed restart exiting `0`
   and leaving the production server down, `--sweep-reasoning` persisting
   mislabeled rows) and its untracked TS-gate runner all lived in the layer that
   was being *written* while that pass ran, so no reviewer had seen it by
   construction.
2. **Compute the facts once.** `uv run python scripts/review_facts.py` (L13) and
   paste the sheet into the shared context of every brief, with "do not re-derive
   the rows it already carries (ruff / drift / compliance), and no slice runs the
   suite". The sheet carries the suite *counts* (a collection), the three gate
   verdicts, the task set, whether the tree is frozen, the platform's liveness
   answer and the working-tree-only inputs. The full suite and `mise lint-ts` are
   deliberately **not** rows, so the one runner measures those once and reports
   them alongside the sheet. *Evidence*: four reviewers re-derived the same
   numbers, and three of them reported a fact as a finding (a stale count, an
   untracked file, a drift status).
3. **Fan out by risk, not by file count.** ~8 agents in three tiers: changed
   *logic* (deep, and allowed to execute code paths — both highs of the third
   pass came from a reviewer running the path rather than reading it),
   wiring/config (medium), docs/data (one batched agent, or just `mise drift`).
   Cap concurrency: sixteen agents on one host were parked mid-flight, and a
   broadcast revived only one of them, so the other fifteen each needed their own
   message. One runner executes the shared commands.
4. **Two lenses, not five passes.** Pass A: correctness against the frozen diff.
   Pass B: cross-file consistency and claim-vs-assertion — the class that only
   appears when files are compared (the pre-hook versus `read_owner`, `cli.py`
   versus `bm25.py` lock directories, a docstring versus the test meant to hold
   it). Keep the deep briefs; pay for depth by cutting breadth.
5. **Turn each recurring class into a gate.** Every one of these had been found by
   hand at least once: a stale suite count, dead lint suppressions, an untracked
   gate runner, a task list that disagreed with `mise.toml`, a test file owned by
   no layer, an unguarded `read_text` in the gate itself, a lock on a directory
   the writer does not write. They are L12 assertions now, so no later pass pays
   for them again — and the compliance scanner pays for itself the same way: it
   caught this protocol's own first change (`OpenProcess` in a liveness probe)
   before a human did.
