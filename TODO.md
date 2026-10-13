# Ideas to try

- Burn down the flaky golden queue (`mise golden-flaky-list`, audit with
  `uv run python scripts/golden_rerun.py --selection list`): current stats —
  `ratkin-root-tender-location` 75%, `strom-farblood-favor` 75%,
  `astounding-ink-acquisition` 67%, `animal-handling-trainer-bands` 50%,
  `poison-belt-best` 50%; plus the manual trio in `data/golden/FLAKY.txt`
  (`gardening-basics`, `gardening-trainers-and-fertilizer`,
  `astounding-ink-acquisition`). The audit still prints deleted ids
  (`animal-handling-ability-audit`, `grow-field-mushrooms`,
  `royal-jelly-gathering`, `mycology-skill`, `riding-trainer-bands`) because
  `history.jsonl` is append-only and `--selection list` does not filter to
  existing golden files — small tool hygiene fix: skip ids with no
  `data/golden/<id>.json`. Triage each with
  `uv run python scripts/golden_rerun.py --id <id> --diagnose`: GEN-SIDE (fact in
  context, LLM dropped it) -> prompt-shape / loop-guidance fix; RET-SIDE ->
  retrieval. Fix the source, never the assertion.

- **Loop tool-choice guidance — the systemic fix behind the remaining flaky
  trio** (`gardening-basics`, `gardening-trainers-and-fertilizer`,
  `astounding-ink-acquisition`). Their facts are reachable — verified
  2026-10-11: `corpus_search "astounding ink vendor"` ranks
  `source_items_item_4105` (Vendor from Spot / Hirochi …) **#1**, and a natural
  Gardening-trainer query returns ten `wiki_Gardening_table_0_row_*` hits naming
  Floxie/Tavilak/Therese — but the model (a) answers skill questions from the
  CDB `skills`/`abilities` tables, which carry no `BonusToSkill` column (it once
  asserted "Gardening grants no bonus levels to any other skill"), instead of
  the corpus skill profile, and (b) runs ONE broad search for acquisition
  questions, gets only the recipe-*uses* tables, and declares "I can't point you
  to a specific source" (`astounding ink`) without ever querying `sources` or
  searching `<item> vendor`. Fix lives in `src/pgrag/agentic/loop.py`: a rule in
  `_SYSTEM_TEMPLATE` — "for where-do-I-get-X: query the `sources` table and
  search '<item> vendor' / '<item> source' before concluding there is no source;
  for skill rewards / trainers / abilities read the corpus skill profile (the
  CDB `skills` table has no BonusToSkill column)" — plus a matching line in
  `_SCHEMA_SUMMARY`'s skills + `sources` notes. Acceptance: `mise golden-short`,
  then the trio (`mise golden-one
  gardening-basics,gardening-trainers-and-fertilizer,astounding-ink-acquisition`),
  then a full `mise golden` (61 cases), in both gate modes (add
  `LLM_REASONING=off` for the deterministic dev signal). Do NOT weaken the trio's
  facts — they are correct and source-grounded; the goldens were the wrong
  suspect. (The `corpus_search` 400→1000 snippet fix already landed 2026-10-11:
  `_SEARCH_SNIPPET` in `src/pgrag/agentic/tools.py` + the
  `test_corpus_search_snippet_reaches_doc_tail` regression test — don't redo it.)

- Golden coverage: comparison is the thinnest bucket (5 of 58). Intake for new
  goldens is live-session questions; the workflow lives in
  `.agents/skills/evaluation/SKILL.md`. Ground every fact with
  `grep data/documents.json` before adding it, and remember the fact-density
  rule: ~10 facts in one answer is flaky no matter how the question is worded —
  split by source doc instead (see the 2026-10-11 splits).

- Track pricing of items in player stalls and player work orders
  - Other players' stalls: glogger's Personal-build `stall_price_observations`
    (v68 market intel — "prices observed at OTHER players' stalls", 0 sentinel =
    price pending) is mirrored into the store but read by nothing, and both the
    tool output (`src/pgrag/agentic/tools.py`, the stall branch) and the loop's
    schema note (`src/pgrag/agentic/loop.py`, the `stall_events` bullets) still
    tell the model "this store has no other players' stalls". Update those notes
    and surface the table (now: 3 rows, all `price_unit=0`).
  - Player work orders (signboard orders you post and price): no store surface at
    all — no `work_order` table, ingest or parser anywhere in `src/`/`scripts/`;
    only CDN NPC work-order turn-ins and `WORKORDER_COIN_REWARD_MOD` exist. Needs
    a new source (glogger / game-log / session parsing).

- Instead of a separate chat window web thing, package this in such a way that I
  can make a custom build of glogger that has this as a tab inside of it
  - first class access to glogger's data
