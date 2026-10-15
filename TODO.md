# Ideas to try

- Burn down the flaky golden queue (`mise golden-flaky-list`, audit with
  `uv run python scripts/golden_rerun.py --selection list`): current audit
  (2026-10-14; recent-10 share, so reruns shift it) —
  `ratkin-root-tender-location` 60%, `strom-farblood-favor` 60%,
  `animal-handling-trainer-bands` 50%, `meditation-synergy-skills` 50%,
  `poison-belt-best` 50%, `dungcrafting-poop-ability-and-rewards` 40%,
  `gardening-basics` 10%; `data/golden/FLAKY.txt` now
  holds only `dungcrafting-poop-ability-and-rewards`. The former manual trio
  (`gardening-basics`, `gardening-trainers-and-fertilizer`,
  `astounding-ink-acquisition`) RECOVERED 2026-10-13 via the loop guidance
  below and was deleted from `data/golden/FLAKY.txt`; all three pass the
  deterministic gate (`LLM_REASONING=off`) and the production gate (trio alone,
  and inside a full `mise golden` run), though `gardening-basics` still shows
  one miss in its last 10 records (2026-10-14T01:53). Also landed: both
  `--selection list` and `--selection flaky` drop ids with no golden file
  (`scripts/golden_rerun.py::audit_rows`/`flaky_ids`,
  `tests/test_golden_rerun.py`), so the deleted
  `animal-handling-ability-audit`, `grow-field-mushrooms`,
  `royal-jelly-gathering`, `mycology-skill`, `riding-trainer-bands` no longer
  print (or read) as flaky. New standing entry:
  `dungcrafting-poop-ability-and-rewards` (loses "Requires Beast" in production
  runs with the guidance in place — 0/12 solo attempts vs 4/4 at the pre-change
  prompt; context-sensitive inside a full `mise golden` run (FAIL, FAIL, PASS
  across three runs) while the deterministic gate is the reverse; see below).
  Triage each with
  `uv run python scripts/golden_rerun.py --id <id> --diagnose`: GEN-SIDE (fact in
  context, LLM dropped it) -> prompt-shape / loop-guidance fix; RET-SIDE ->
  retrieval. Fix the source, never the assertion.

- **DONE 2026-10-13 — Loop tool-choice guidance (the systemic fix behind the
  flaky trio) landed** in `src/pgrag/agentic/loop.py` (`_SCHEMA_SUMMARY`, three
  query-recipe notes; no `_SYSTEM_TEMPLATE` rule in the shipped shape — see the
  tradeoff below): (1) skill trainer LISTS come from
  `npcs.services` (`json_extract(s.value,'$.Type')='Training'` +
  `json_each(...Skills)`, key = skill name without spaces), because the wiki
  level-cap page lists only a subset; (2) 'where do I get X' runs the `sources`
  table for `item_key='item_'||code` across ALL entry types plus
  `corpus_search '<item> vendor'` before concluding there is no source —
  recipe/uses docs are not acquisition, and a bare item-name search returns
  only the recipe-uses tables; (3) skill synergy reads the corpus skill profile
  (`corpus_search 'BonusToSkill <skill>'` → `skillprofile_<Skill>_chunk_0`,
  which also carries Description / Type / Abilities / Trainers / Quests), never
  the plain `skills` columns (they carry no `BonusToSkill` — the model used to
  assert "Gardening grants no bonus levels to any other skill"). Verified
  2026-10-13: the trio passes the deterministic gate (`LLM_REASONING=off`:
  trio + `mise golden-short`) and the production gate (trio alone, and inside a
  full `mise golden` run) — see TODO's flaky-queue entry.
  **Open tradeoff (follow-up):** under production flags the guidance destabilises
  `dungcrafting-poop-ability-and-rewards` when that case runs ALONE (0/12
  attempts vs 4/4 at the pre-change prompt; it RET-side loses "Requires Beast",
  which lives only in `skillprofile_Pooping_chunk_0`), while the same case
  *passes* deterministically with the guidance and *fails* at the pre-change
  prompt. Inside a full 61-case run it is context-sensitive (FAIL, FAIL, PASS
  across three runs). Root cause is the suggested profile query:
  `BonusToSkill Dungcrafting` does not return `skillprofile_Pooping_chunk_0`
  at all (measured k=10/k=20, full and tool corpora) — Dungcrafting's profile
  is thin (2 Rewards lines), so the bare `skill_Pooping` doc wins on token
  echo and the requirement line is never in context. Querying the profile
  directly works (`corpus_search 'skill profile Dungcrafting'` →
  `skillprofile_Pooping_chunk_0` #1), and loop.py's guidance now says to
  re-query whenever the profile is missing from the hits. A single-id
  reasoning-ON run (2026-10-14) PASSED with the corrected guidance (1/1), so
  the 0/12 above belongs to the pre-fix wording. An earlier
  `_SYSTEM_TEMPLATE` rule + transcription directive ("answer WHERE/WHAT-grants
  questions from a targeted query", "transcribe every entry of the list") was
  dropped: it never changed this case's outcome and the trio passes without it.
  Tracked in `data/golden/FLAKY.txt`.

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
