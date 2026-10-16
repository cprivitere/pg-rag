# Ideas to try

- Flaky golden queue: CLEAR as of 2026-10-15. `--selection list` tops out at
  44% (`meditation-synergy-skills`, every miss from 2026-10-06), and the flaky
  selection (recent-10 >50% + manual `data/golden/FLAKY.txt`) resolves to `[]`
  — the last manual entry (`dungcrafting-poop-ability-and-rewards`) was deleted
  on recovery (trail kept in the file). Two root causes closed it:
  1. **The seed had no stopword filter.** `run_loop` seeds with the RAW
     question (`corpus_search question k=12`) and `BM25.search` scored every
     token, so a long natural-language question ranked docs stuffed with
     'the'/'does'/'it' above the doc holding the answer (measured: the
     dungcrafting question's top-12 was a Citan forum post +
     `effect_effect_7028`). Query-side stopwords (`_QUERY_STOPWORDS`; the index
     is untouched, so no rebuild and the pickle stays valid) put
     `skillprofile_Pooping_chunk_0` at seed #5 + `skill_Pooping` #3 there, and
     raised `creature_Ratkin_Root-Tender` 7 → 1 (ratkin) and
     `wiki_Strom Farblood_Favor` 3 → 1 (strom) — those two were already seeded,
     the filter promotes them. `moonstone-item` flipped FAIL→PASS on this
     change alone.
  2. **Guidance notes** (`_SCHEMA_SUMMARY`): 'which recipes use X'
     resolves `itemuses.recipes_that_use` ids to recipe NAMES (Moonstone →
     White Dye); `player_state section=stall` prints the item's `source:` lines
     and a price answer must quote them (Woe Coin → 'source: Barter via Sven
     the Bleeder'); a CATEGORY/plural ask ('all metal slabs') is ONE
     `section=stall query='metal slab'` call, not one per variant, and
     `items.value` is never evidence of "no stall data" (the single full-run
     miss of 2026-10-15, `stall-price-slabs-sands-gizmos`, answered "Metal
     Slabs — base value only, no stall data" while that one call returns 63
     slab sales; 1/3 solo passes before the note → 3/3 after); which trainer
     teaches which LEVEL BAND is the skill's own page (`get_page('<Skill>')`),
     quoting EVERY 'Levels A-B are unlocked by …' line (81-90 has two). Plus a
     loop robustness fix: the malformed-tool-call 500 is re-asked on the forced
     final (no-tools) turn instead of erroring the case out
     (`animal-handling-known-abilities` was scored 0 by it).
  Measured: full `mise golden` 59/61 before (fails `moonstone-item` +
  `pricing-fat-woe-cutting-circuit`) → 58/61 with the BM25 change alone (1
  harness ERROR) → **61/61** (1595 s, reasoning-ON) with everything.
  Review-driven corrections to the stopword filter (2026-10-15, second pair of
  eyes on the diff): `off`/`only`/`under` are corpus vocabulary, not function
  words (Off-Hand, Off-Guard, '(Fae Only)', 'Caves Under Serbule' — dropping
  them ranked a Main-Hand bead row #1 for an Off-Hand question), so they are
  no longer filtered; and the seed is NOT a free win — the drop costs long
  prose docs their function-word credit, so a strict fact-in-seed check over
  the 61 questions measures 13 fact-group hits lost vs 15 gained (worst
  `meditation-synergy-skills` 15 → 8), which the model's own tool calls cover
  (the full gate is the arbiter, not the seed). Both recorded in
  `src/pgrag/rag/bm25.py` and `.agents/skills/retrieval/SKILL.md`.
  Variance-prone but NOT queue entries: `pricing-fat-woe-cutting-circuit`
  (9 fact groups, one store-only audit; its notes already record 0/3–1/4 pass
  on the Sven/ownership facts) — the ownership near-miss was a variant gap
  (added 'your stall'/'your own listings'), the Sven miss is what the stall
  `source:` note targets; if it flaps again the remedy is the fact-density
  split (evaluation skill), which needs an explicit OK because it changes the
  gate. `animal-handling-trainer-bands` (8 fact groups, one wiki page) is the
  same shape — watch it, don't touch it while it passes.
  Later runs on 2026-10-15 (four more full gates: 61/61, 60/61, 57/61, 59/61 —
  the aggregate is noise-dominated at one sample, so read cases, not runs)
  surfaced three more, all diagnosed rather than guessed:
  `stall-price-slabs-sands-gizmos` (the run's single miss, 1/3 solo passes: it
  answered "Metal Slabs — base value only, no stall data" while ONE
  `player_state section=stall (query='metal slab')` call returns 63 slab sales)
  → category-ask note, 3/3 after. `vendor-top-earner-npc` (0/3): the model
  invented a `npc_name` column, the query errored, it fell back to
  `SELECT * FROM game_state_npc_vendor LIMIT 5` and answered from that
  arbitrary sample (Cheb); every earlier PASS was the stale `621` fact matching
  Roshun the Traitor's 351,621 incidentally → vendor-JOIN note + the lifetime
  number re-grounded (Flia 647,287; the authoring value 621,389 drifted),
  3/3 after. `strom-farblood-favor` (2 consecutive full-run misses on "lead to
  gold", solo PASS): NOT the stopword filter — `quest_quest_29162` ("Lead to
  Gold") ranks >40 both before and after the change, so the chain has to be
  fetched; watch it.
  Gate reliability: 2 of 6 full runs scored a case ERROR on the server's
  malformed-tool-call 500 (`animal-handling-known-abilities`,
  `ranalon-den-quests`); the loop now re-asks once and, if the model repeats the
  call, forces the grounded final answer instead of raising.
  Residual (single-run noise, NOT diagnosed failures — the derived flaky list
  still computes to `[]`): the last gate's misses moved to
  `cheesemaking-leveling` (2 of 5 runs, `6990` XP-range + `first-time`) and
  `gardeningrelated_items` (1 run, `guano`). Both are multi-number answers over
  reachable data — the XP-range scalar-difference recipe is already spelled out
  in the guidance — so the doctrine-correct remedy is the fact-density split,
  which needs the user's OK.

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
  **Closed 2026-10-15** — the "open tradeoff" below (dungcrafting losing
  "Requires Beast" under production flags) had a single root cause: the loop's
  SEED scored the raw question without dropping function words, so
  `skillprofile_Pooping_chunk_0` never entered context. `BM25.search` now
  filters query-side stopwords and the profile is seed hit #5; the case passed
  a solo rerun, a targeted rerun and both full `mise golden` runs on
  2026-10-15, and the entry is deleted from `data/golden/FLAKY.txt`. Original
  measurement, kept for the record: under production flags the guidance
  destabilised `dungcrafting-poop-ability-and-rewards` when that case ran ALONE
  (0/12 attempts vs 4/4 at the pre-change prompt; it RET-side lost "Requires
  Beast", which lives only in `skillprofile_Pooping_chunk_0`), while the same
  case *passed* deterministically with the guidance and *failed* at the
  pre-change prompt; inside a full 61-case run it was context-sensitive (FAIL,
  FAIL, PASS across three runs). The suggested profile query
  `BonusToSkill Dungcrafting` does not return `skillprofile_Pooping_chunk_0`
  (measured k=10/k=20, full and tool corpora) — Dungcrafting's profile is thin
  (2 Rewards lines), so the bare `skill_Pooping` doc wins on token echo.
  Querying the profile directly works (`corpus_search 'skill profile
  Dungcrafting'` → `skillprofile_Pooping_chunk_0` #1), and loop.py's guidance
  still says to re-query whenever the profile is missing from the hits. An
  earlier `_SYSTEM_TEMPLATE` rule + transcription directive ("answer
  WHERE/WHAT-grants questions from a targeted query", "transcribe every entry
  of the list") was dropped: it never changed this case's outcome and the trio
  passes without it.

- Golden coverage: comparison is the thinnest bucket (5 of 61). Intake for new
  goldens is live-session questions; the workflow lives in
  `.agents/skills/evaluation/SKILL.md`. Ground every fact with
  `grep data/documents.json` before adding it, and remember the fact-density
  rule: ~10 facts in one answer is flaky no matter how the question is worded —
  split by source doc instead (see the 2026-10-11 splits).

- Track pricing of items in player stalls and player work orders
  - **DONE 2026-10-15 — Other players' stalls are a live store surface.**
    glogger's Personal-build `stall_price_observations` (v68 market intel,
    `price_unit 0` = price pending) was mirrored but read by nothing, and both
    the tool banner and the loop's schema note told the model "this store has no
    other players' stalls" — a false claim. Now: `player_state section=stall`
    appends an `OTHER PLAYERS' STALLS` block in both branches (sales and
    zero-sale), each row dated with stall label / owner / source and
    `price not recorded (pending)` when glogger has no price; the loop note and
    `_TOOL_CONTRACT` name the table and forbid presenting those rows as your own
    sale or a market average (`scripts` need no change — ingest already mirrors
    it). Contract: `tests/test_agentic_tools.py::
    test_player_state_stall_other_players_observations`. Live rows are 4 (all
    `price_unit 0`, `owner_name` NULL, `stall_label` 'Unidentified Stall'), so
    the block currently reports observations without prices — real prices need
    glogger captures, not a code change. A golden is NOT warranted yet: two
    direct `run_loop` probes (2026-10-15) answered as well as a hand-written
    oracle — the broad "what have I observed other players' stalls selling?"
    went straight to `stall_price_observations` and reported all 4 rows with the
    pending-price sentinel, owners and stall labels, and the targeted
    "what's Great Phlogiston worth?" answered from the tool block (own sales by
    price, the dated observation, chat mentions as asking prices). Revisit when
    a PRICED observation exists — the fact set is thin until then.
  - Player work orders (signboard orders you post and price): no store surface at
    all — no `work_order` table, ingest or parser anywhere in `src/`/`scripts/`;
    only CDN NPC work-order turn-ins and `WORKORDER_COIN_REWARD_MOD` exist. Needs
    a new source (glogger / game-log / session parsing).

- Instead of a separate chat window web thing, package this in such a way that I
  can make a custom build of glogger that has this as a tab inside of it
  - first class access to glogger's data
