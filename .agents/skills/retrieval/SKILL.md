---
name: retrieval
description: Search-quality troubleshooting manual for pg-rag's agentic tool loop. Use when investigating why a query returns wrong/missing/refusing results, or before changing any retrieval stage.
---

# retrieval — tool-loop search troubleshooting

There is exactly **one** retrieval path: the agentic tool loop
(`pgrag.agentic.loop.run_loop`, `src/pgrag/agentic/loop.py`). The model answers
by issuing bounded rounds of tool calls against `data/sqlite_gorgon.db`
(including the FTS-indexed generated corpus), then writes the answer. The dense path (embeddings + Chroma → RRF
fusion → reranker), the query classifier, spelling correction, entity dossiers
and `_gap_fill` were **deleted** — do not look for them.

## The six tools (in `src/pgrag/agentic/tools.py`)

`AVAILABLE_TOOLS = "sql_query, find_entities, get_page, corpus_search,
corpus_get_doc, player_state"`.

| tool | what it hits | use it for |
|---|---|---|
| `sql_query` | one read-only SELECT over the store | structured facts: recipes/levels/XP, item sources, calls/favor, stall sales. `LIMIT 200` injected; >2M-op scans aborted |
| `find_entities` | FTS5 over `entity_names` | resolving a name to its typed entity (item/recipe/ability/quest/npc/effect/skill), up to 20 hits |
| `get_page` | raw wiki page text by exact title (LIKE fallback lists candidates) | prose/mechanics — how a skill works, growing/farming, crafting workflows. Exempt from the 12000-char result cap |
| `corpus_search` | FTS5 `bm25()` over the store's `documents` | generated-corpus recall when SQL/FTS don't fit — the lexical arm; `--corpus tool` filters to non-SQL-table docs. Each hit cut at `_SEARCH_SNIPPET` (2000) chars |
| `corpus_get_doc` | parent + chunks for one corpus id | when a `corpus_search` hit ends mid-section — chunk id (or parent id) returns the whole parent instead of more search rounds |
| `player_state` | live-session sections (summary/skills/currencies/quests/favor/items/stall/abilities) | the player's own state; `section=abilities` needs `skill=`; `section=stall` holds the only *realized* prices (own stall — `stall_events`) and appends the item's `source:` lines, CDN base value, OTHER PLAYERS' STALLS observations (`stall_price_observations`), the work-order payout and best-effort chat price mentions |

The loop is blocking and bounded (`DEFAULT_MAX_ROUNDS` rounds; every tool-result
payload except `get_page` capped at 12000 chars). Tool errors come back as strings for the model
to self-correct, never as exceptions.

**`corpus_search` shows only the first `_SEARCH_SNIPPET` (2000) chars of each hit; `corpus_get_doc(id)`
reassembles the whole parent when a hit ends mid-section** — so a fact past the cut costs one more
tool call, not a lost golden. (`get_page` serves wiki
titles, so it returns a near-empty error for a corpus id like `skill_Pooping`
(the model does try, and burns rounds) — reach for `corpus_get_doc` instead.) At the old 400-char budget
`skillprofile_Pooping_chunk_0` (871 chars) hid `Requires Beast` (offset 788) and
the gourmand food-quality note (451), and `skillprofile_Gardening_chunk_4`
(1089) hid its trainer list (703-830) — all of which failed goldens with no
corpus gap at all (raised to 2000 after the 1000-char step on 2026-10-11; 10 hits x 2000 + prefixes ≈
20.6K caps via the loop's row-aware `_cap` on multi-hit rounds). When a fact goes missing, check the offset
first: `python -c "import sqlite3;t=sqlite3.connect('data/sqlite_gorgon.db').execute(\"SELECT text FROM documents WHERE id='<id>'\").fetchone()[0];print(t.find('<fact>'))"`.
A related trap: for skill questions the model often answers from the CDB
`skills`/`abilities` SQL tables (which carry no `BonusToSkill` rewards), so it
can claim "this skill grants no bonus levels" while the corpus profile doc holds
the full list — a RET/GEN miss that looks like a data gap but is tool choice.

**Query-side stopwords.** `corpus_search` drops function words
(`tools._QUERY_STOPWORDS`) before matching; the FTS index
keeps every token. This matters for the loop's seed, which passes the **raw question**:
before the filter, a long natural-language question scored docs stuffed with
`the`/`does`/`it` above the doc holding the answer (measured 2026-10-15 — the
`dungcrafting-poop-ability-and-rewards` question ranked a Citan forum post and
`effect_effect_7028` above `skillprofile_Pooping_chunk_0`, so the profile's
`Requires Beast` line never entered context and the case flapped RET-side; with
the filter the profile is seed hit #5). If a long-question case misses while its
keywords work as a short query, suspect the query shape, not the data.
It is **not free**: the drop removes the credit long prose docs used to earn
from function words, so a few seeds cover FEWER facts than before (measured
over the then-61 golden questions: 13 fact-group hits lost vs 15 gained on the
k=12 seed; worst `meditation-synergy-skills` 15 → 8). Those cases still pass
because the model's own tool calls carry the facts — the **full gate** is the
arbiter, not the seed. `off`/`only`/`under` are deliberately NOT filtered:
hyphenated corpus vocabulary tokenizes to them (Off-Hand, Off-Guard,
'(Fae Only)', 'Caves Under Serbule') and dropping them inverts those queries.

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
   (`mise sql-store` then the sqlite file) and `SELECT text FROM documents WHERE id='<id>'`. If it
   is absent, the problem is upstream (loader/builder/store ingest), not the
   loop.
2. **Did the model call the right tool?** Inspect `trace_rounds` — a
   pricing question that ran only `sql_query` against `items` instead of
   `player_state section=stall` (realized sales + base value + `source:` lines +
   other-stall observations + chat mentions) is a routing miss driven by the
   schema summary.
3. **Corpus freshness** — is `corpus_search` reading stale text? See below.
4. **Answer generation** — context right, answer wrong → prompt/`_SYSTEM_TEMPLATE`
   (GEN-side). Offline tests monkeypatch `loop._post`, never a live LLM;
   `tests/test_agentic_loop.py` owns the round protocol.

## Corpus freshness

- The store build runs the document builders in-process and stamps every
  `documents` row with `DOCUMENTS_VERSION` (`src/pgrag/config.py`); there is
  no intermediate artifact and no staleness refusal. Regenerate with
  `mise sql-store` (or a `mise sync-*` task), never by hand.
- Build outputs (the store's `documents` rows) are outputs — change the builder, never the rows.

## Rule of thumb

- One stage at a time; re-run the layer's tests after each (`docs/TEST_CONTRACTS.md`
  L2/L3).
- The retrieval tests are `tests/test_agentic_tools.py`,
  `tests/test_agentic_loop.py`, `tests/test_agentic_tune.py` — there is **no** dense/RRF/rerank regression set
  anymore (the BM25 pickle suite went with the corpus→store move).

## Evaluation loop (see `evaluation` skill)

For any retrieval change: pick representative golden cases → run before → make
one change → run after → compare pass/fail and read `--diagnose` GEN/RET labels.
`data/golden/*.json` + `scripts/golden_check.py` is the automated form.
