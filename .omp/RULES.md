# RAG Builder Rules

Hard requirements for pg-rag sessions. Sticky: re-attached near the current turn, so they apply even after the opening context scrolls away.

1. **Never rebuild the entire corpus unless explicitly requested.** Prefer the incremental paths: `mise build-bm25` / `mise build-tool-corpus` for the derived indexes; full `mise sync-full` only when the user asks for a full refresh.
2. **Never modify generated documents manually.** `data/documents.json`, `data/derived/wiki_parsed.json`, `data/bm25_index.pkl`, `data/tool_documents.json`, `data/tool_bm25.pkl` are build outputs — change builders/loaders, never the artifacts.
3. **Preserve source IDs and metadata.** Doc `id`, `metadata.source`, `metadata.table` are the identity contract. Do not rename or drop them; the BM25 corpus and the SQLite store are keyed off them.
4. **Never replace the retrieval path without first identifying the existing implementation.** The agentic tool loop (`pgrag.agentic.loop.run_loop` driving `sql_query`/`find_entities`/`get_page`/`corpus_search`/`player_state`) is the only retrieval architecture. A dense/vector-store, RRF, or reranker proposal is a NEW architecture, not a fix — there is no existing component to "upgrade".
5. **Before proposing architecture changes, inspect the relevant implementation.** The loop is blocking and bounded (`DEFAULT_MAX_ROUNDS` rounds, then a forced no-tools answer); only the model's own tool rounds re-query, and each tool-result payload is capped. Read `src/pgrag/agentic/tools.py` and `loop.py` first.
6. **When changing retrieval, run the surviving retrieval tests** — `tests/test_bm25.py`, `tests/test_bm25_persist.py`, `tests/test_agentic_tools.py`, `tests/test_agentic_loop.py`. The old dense/RRF/rerank regression set was deleted with the dense path.
7. **When changing document generation, inspect representative generated documents** after `pgrag build-documents`.
8. **When changing the corpus/index, distinguish layers:** source data → generated documents → BM25 index → tool corpus. `scripts/build_tool_corpus.py` refuses a stale `DOCUMENTS_VERSION` stamp; `rag/bm25.py` serves its persisted index while `documents.json` is unchanged and rebuilds otherwise. Regenerate via `build-documents` (`mise generate-docs`) or a `mise sync-*` task — never assume `build-bm25`/`build-tool-corpus` re-parse source.
9. **Prefer incremental rebuilds during development.**
10. **Bump `DOCUMENTS_VERSION` (`src/pgrag/config.py`) whenever document shape changes**, then re-stamp via `build-documents` — a shape change that skips the bump ships facts rendered by an older generator and can silently build a derived corpus from the wrong shape.
11. **Wiki names come from `data/wiki/.meta.json`.** Filenames are `{safe_title}_<sha256-8>.txt` — never derive display names from filenames, and never let tests write the real meta file (tmp dirs only).
12. **A test edit that changes what it asserts is a CONTRACT CHANGE, not a workaround.** State the new contract in the test, run the layer's sibling suite, and never weaken an assertion to dodge a real regression (see `docs/TEST_CONTRACTS.md`). No test edit "to make it pass" without proving the source is fine. Watch shared-function facet coverage: `run_loop` is asserted across `test_agentic_loop.py`, `test_agentic_tune.py`, and `test_golden_check.py`; `execute_tool` across `test_agentic_tools.py`/`test_agentic_loop.py` (see `docs/TEST_CONTRACTS.md` L3).
13. **Cross-instance build lock — the writers own it, the hook only probes.**
    The corpus writers hold an atomic `O_EXCL` lock at `data/.omp-build.lock`
    for the whole build: `pgrag build-documents` (`src/pgrag/cli.py`), the BM25
    persist/rebuild (`src/pgrag/rag/bm25.py`) and `scripts/build_tool_corpus.py`
    (via `pgrag.buildlock.corpus_lock`). That covers every spelling —
    `mise sync-*`/aliases, `mise run --force …`, `… ::: syf`, the direct CLI, the
    direct scripts — and a second builder fails loudly instead of interleaving.
    `.omp/hooks/pre/00-repo-lock.ts` is only the early warning: it probes the
    same file and blocks destructive corpus commands while a live owner holds it
    (never taking the lock itself — a hook's tool call ends long before a build
    does). `mise sql-store`/`sq` is *not* guarded (private additive store, not
    corpus state) and neither are publishes (`upload-docs`/`upload-store`). A
    pure read that merely *names* a task (`grep -rn build-bm25 .`,
    `git commit -m 'fix build-documents stamp'`, `mise tasks | grep build-bm25`)
    is never blocked, but the reader list is a heuristic: an unusual spelling
    (e.g. `uv run python -c "..."` that names a build script, or
    `mise which docs`) may still be probed, and a command that redirects output
    or chains a writer (`grep -rn build-bm25 . > out.txt`,
    `grep x && mise syf`) always is — a pipeline of pure readers is not. The
    writers' own lock is the real guarantee.
    Never bypass the lock by hand-editing it; to clear a stuck one, delete
    `data/.omp-build.lock`.
