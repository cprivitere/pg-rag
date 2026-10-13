---
name: retrieval
description: Search-quality troubleshooting manual for pg-rag's agentic tool loop. Use when investigating why a query returns wrong/missing/refusing results, or before changing any retrieval stage.
---

# retrieval — tool-loop search troubleshooting

There is exactly **one** retrieval path: the agentic tool loop
(`pgrag.agentic.loop.run_loop`, `src/pgrag/agentic/loop.py`). The model answers
by issuing bounded rounds of tool calls against `data/sqlite_gorgon.db` and the
BM25 corpus, then writes the answer. The dense path (embeddings + Chroma → RRF
fusion → reranker), the query classifier, spelling correction, entity dossiers
and `_gap_fill` were **deleted** — do not look for them.

## The five tools (in `src/pgrag/agentic/tools.py`)

`AVAILABLE_TOOLS = "sql_query, find_entities, get_page, corpus_search,
player_state"`.

| tool | what it hits | use it for |
|---|---|---|
| `sql_query` | one read-only SELECT over the store | structured facts: recipes/levels/XP, item sources, calls/favor, stall sales. `LIMIT 200` injected; >2M-op scans aborted |
| `find_entities` | FTS5 over `entity_names` | resolving a name to its typed entity (item/recipe/ability/quest/npc/effect/skill), up to 20 hits |
| `get_page` | raw wiki page text by exact title (LIKE fallback lists candidates) | prose/mechanics — how a skill works, growing/farming, crafting workflows |
| `corpus_search` | BM25 over `data/documents.json` + `data/bm25_index.pkl` | generated-corpus recall when SQL/FTS don't fit — the lexical arm; `--corpus tool` swaps to `data/tool_documents.json` + `data/tool_bm25.pkl` |
| `player_state` | live-session sections (summary/skills/currencies/quests/favor/items/stall/abilities) | the player's own state; `section=abilities` needs `skill=`; `section=stall` holds the only *realized* prices (own stall) and appends the work-order payout and best-effort chat price mentions |

The loop is blocking and bounded (`DEFAULT_MAX_ROUNDS` rounds; each tool-result
payload capped at 12000 chars). Tool errors come back as strings for the model
to self-correct, never as exceptions.

**`corpus_search` shows only the first `_SEARCH_SNIPPET` chars of each hit, and
that snippet is the ONLY window onto a corpus doc** — `get_page` serves wiki
titles, so it returns a near-empty error for a corpus id like `skill_Pooping`
(the model does try, and burns rounds). Facts that sit past the cut are
unreachable no matter how good the query is: at the old 400-char budget
`skillprofile_Pooping_chunk_0` (871 chars) hid `Requires Beast` (offset 788) and
the gourmand food-quality note (451), and `skillprofile_Gardening_chunk_4`
(1089) hid its trainer list (703-830) — all of which failed goldens with no
corpus gap at all (raised to 1000 on 2026-10-11; 10 hits x 1000 + prefixes ≈
10.6K stays inside the payload cap). When a fact goes missing, check the offset
first: `python -c "import json;t=[d for d in json.load(open('data/documents.json')) if d['id']=='<id>'][0]['text'];print(t.find('<fact>'))"`.
A related trap: for skill questions the model often answers from the CDB
`skills`/`abilities` SQL tables (which carry no `BonusToSkill` rewards), so it
can claim "this skill grants no bonus levels" while the corpus profile doc holds
the full list — a RET/GEN miss that looks like a data gap but is tool choice.

## Debugging discipline

**First localize the failure to the GEN side or the RET side. Never modify
multiple stages at once.**

`run_loop` returns `documents` (exactly what the model was fed) and
`source`/`trace_rounds` (per tool round). `scripts/golden_check.py --diagnose`
uses these to label a miss:

- **GEN-side** — the needed fact is in the fed context (a tool returned it) but
  the answer omits it. Fix the prompt/tool contract or the tool's output shape,
  not retrieval.
- **RET-side** — no tool round ever surfaced the fact. The model queried the
  wrong thing, the fact is absent from the store/corpus, or a tool is missing.

Then:

1. **Is the fact in the data at all?** Query the store directly
   (`mise sql-store` then the sqlite file) and `grep data/documents.json`. If it
   is absent, the problem is upstream (loader/builder/store ingest), not the
   loop.
2. **Did the model call the right tool?** Inspect `trace_rounds` — a
   pricing question that ran only `sql_query` against `items` instead of
   `player_state section=stall` (realized sales + base value + chat mentions)
   is a routing miss driven by the schema summary.
3. **Corpus freshness** — is `corpus_search` reading stale text? See below.
4. **Answer generation** — context right, answer wrong → prompt/`_SYSTEM_TEMPLATE`
   (GEN-side). Offline tests monkeypatch `loop._post`, never a live LLM;
   `tests/test_agentic_loop.py` owns the round protocol.

## Corpus freshness

- `pgrag build-documents` stamps `data/derived/documents_version.json` with
  `DOCUMENTS_VERSION` (`src/pgrag/config.py`). `scripts/build_tool_corpus.py`
  refuses to run against a stale stamp — regenerate with `build-documents`
  (`mise generate-docs`) or a `mise sync-*` task, never by hand.
- `rag/bm25.py` serves `data/bm25_index.pkl` while `data/documents.json` is
  unchanged (mtime-keyed) and rebuilds otherwise. If BM25 results look like an
  older corpus, check the documents.json mtime / rebuild with `mise build-bm25`.
- Build artifacts (`documents.json`, `bm25_index.pkl`, `tool_documents.json`,
  `tool_bm25.pkl`, the store) are outputs — change the builder, never the file.

## Rule of thumb

- One stage at a time; re-run the layer's tests after each (`docs/TEST_CONTRACTS.md`
  L2/L3/L4).
- The surviving retrieval tests are `tests/test_bm25.py`,
  `tests/test_bm25_persist.py`, `tests/test_agentic_tools.py`,
  `tests/test_agentic_loop.py` — there is **no** dense/RRF/rerank regression set
  anymore.

## Evaluation loop (see `evaluation` skill)

For any retrieval change: pick representative golden cases → run before → make
one change → run after → compare pass/fail and read `--diagnose` GEN/RET labels.
`data/golden/*.json` + `scripts/golden_check.py` is the automated form.
