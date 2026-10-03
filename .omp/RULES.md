# RAG Builder Rules

Hard requirements for pg-rag sessions. Sticky: re-attached near the current turn, so they apply even after the opening context scrolls away.

1. **Never rebuild the entire corpus unless explicitly requested.** Prefer incremental paths: `pgrag build-index --source cdn|wiki|computed|curated` for partial rebuilds; full `mise sync` only when the user asks for a full refresh.
2. **Never modify generated documents manually.** `data/documents.json`, `data/wiki/.parsed.json`, `data/bm25_index.pkl` are build outputs — change builders/loaders, never the artifacts.
3. **Preserve source IDs and metadata.** Doc `id`, `metadata.source`, `metadata.table` are the identity contract. Chroma `update()` merges metadata (add-only); renames/removals require a full rebuild.
4. **Never replace an existing retrieval component without first identifying the existing implementation.** Dense + BM25 → RRF fusion → reranker is existing architecture, not a suggestion.
5. **Before proposing architecture changes, inspect the relevant implementation.** The pipeline is one-shot; only `_gap_fill` re-retrieves. Agentic/tool-calling retrieval is NOT built — never assume it exists.
6. **When changing retrieval, run the retrieval regression tests** — `tests/test_bm25.py`, `tests/test_retrieval_unit.py`, `tests/test_rerank*.py`, `tests/test_retriever_spelling.py`.
7. **When changing document generation, inspect representative generated documents** after `pgrag build-documents`.
8. **When changing the index, distinguish layers:** source data → generated documents → embeddings → index state. Run `pgrag validate` after index work. `build-index` only embeds the persisted `data/documents.json` and refuses a stale generator version (see `DOCUMENTS_VERSION`); regenerate via `build-documents` (`mise generate-docs`) or a `mise sync-*` task, never assume `build-index` re-parses source.
9. **Prefer incremental rebuilds during development.**
10. **Never silently change embedding models.** Embeddings are a fixed-dim contract with the Chroma collection; changing the model means a full re-embed.
11. **Wiki names come from `data/wiki/.meta.json`.** Filenames are `{safe_title}_<sha256-8>.txt` — never derive display names from filenames, and never let tests write the real meta file (tmp dirs only).
12. **A test edit that changes what it asserts is a CONTRACT CHANGE, not a workaround.** State the new contract in the test, run the layer's sibling suite, and never weaken an assertion to dodge a real regression (see `docs/TEST_CONTRACTS.md`). No test edit "to make it pass" without proving the source is fine. Watch shared-function facet coverage: `retrieve()` is asserted across `test_bm25.py`, `test_retrieval_unit.py`, `test_where_filter.py`, `test_retrieval_trace.py`, `test_retrieval_eval.py`; `_hybrid_fuse` across `test_bm25.py`/`test_rerank.py` (see `docs/TEST_CONTRACTS.md` L3).
14. **Cross-instance build lock.** `.omp/hooks/pre/00-repo-lock.ts` guards destructive corpus commands (`build-*`, `generate-docs`, `mise sync-*`) with an `O_EXCL` lock at `data/.omp-build.lock`. Never bypass it by hand-editing the lock; to clear a stuck lock, delete `data/.omp-build.lock`.