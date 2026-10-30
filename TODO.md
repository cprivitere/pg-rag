# Ideas to try

- Flaky golden queue: CLEAR as of 2026-10-15. `--selection list` tops out at
  44% (`meditation-synergy-skills`, every miss from 2026-10-06), and the flaky
  selection (recent-10 >50% + manual `data/golden/FLAKY.txt`) resolves to `[]`
  — the last manual entry (`dungcrafting-poop-ability-and-rewards`) was deleted
  on recovery (trail kept in the file). Two root causes closed it:
  1. **The seed had no stopword filter.** `run_loop` seeds with the RAW
     question (`corpus_search question k=12`) and the FTS query scored every
     token, so a long natural-language question ranked docs stuffed with
     'the'/'does'/'it' above the doc holding the answer (measured: the
     dungcrafting question's top-12 was a Citan forum post +
     `effect_effect_7028`). Query-side stopwords (`_QUERY_STOPWORDS`; the FTS index
     is untouched, so no rebuild needed) put
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
  `pricing-fat-woe-cutting-circuit`) → 58/61 with the stopword change alone (1
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
  `src/pgrag/agentic/tools.py::_QUERY_STOPWORDS` and `.agents/skills/retrieval/SKILL.md`.
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
- **DONE 2026-10-15 — `cheesemaking-leveling` split into ladder + recipe cases;
  `gardeningrelated_items` measured and left alone (user OK'd the gate change).**
  Single-attempt samples (`--attempts 1` — the honest single-sample rate; the
  gate's retry-once is untouched): parent `cheesemaking-leveling` **1/4**
  (misses `first-time`; `6990`+`11710`; `sharp cheddar`+`first-time`+`repeat`),
  consistent with its 6-FAIL/6-PASS log history. The children:
  `cheesemaking-leveling-xp` **3/4** (its one miss was `11710`+`4720` — the
  `6990` arithmetic probe itself was answered) and
  `cheesemaking-leveling-recipes` **4/4** (re-measured 4/4 single-attempt after
  the review split its `96` XP number into a group of its own) — both at or
  above the accepted ≥3/4, both better than the parent's 1/4.
  Remedy rationale: the fact-density rule's precondition ("facts span many docs
  **AND** ~10 facts in one answer") was NOT met — all 8 groups lived in ONE doc
  family (`leveling_Cheesemaking` chunks 1/4/5/8). What WAS met: the
  **question-shape violation** (the question never asked for the two cumulative
  totals its facts demanded) and three bare `level 25`/`level 17` variants that
  let a level-only mention satisfy three groups — exactly the measured `11710`
  miss shape (`logs/golden-gate-after-review-fixes2.txt`). Those three variants
  are DROPPED, each having made its group satisfiable without the graded
  number. Split basis is the doctrine's fallback clause "per audit dimension":
  XP ladder/arithmetic (3 groups) vs recipe/strategy text (6 groups — the legacy
  `sharp cheddar` group was split so its `96` XP number is graded on its own,
  naming the recipe having satisfied it for free; parent 8 groups → 3 + 6).
  `SHORT_TIER_IDS` now carries `cheesemaking-leveling-xp` (the tier stays 8);
  the set went 61 → 62 files.
  `gardeningrelated_items` measured **4/4** single-attempt (11/12 logged full
  runs; the lone miss was a 1-round `guano` list omission, and
  `history.jsonl` carries no miss record for it — all 4 logged rows are passes)
  → **no change**: 4 groups in one
  table (`items`, matched on `keywords LIKE '%GardeningRelated%'`) is one
  dimension, so the density rule's precondition is absent again and a split
  would be an invented remedy. Watch the two new children; they run in the
  normal gate, not `FLAKY.txt`.

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
  `skillprofile_Pooping_chunk_0` never entered context. The FTS query builder now
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

- Golden coverage: comparison is the thinnest bucket (5 of 62). Intake for new
  goldens is live-session questions; the workflow lives in
  `.agents/skills/evaluation/SKILL.md`. Ground every fact with
  `SELECT text FROM documents WHERE id='<id>'` over `data/sqlite_gorgon.db` before adding it, and remember the fact-density
  rule: ~10 facts in one answer is flaky no matter how the question is worded —
  split by source doc instead (see the 2026-10-11 splits).

- Track pricing of items in player stalls and player work orders
  - **DONE 2026-10-15 — Other players' stalls are a live store surface.**
    glogger's `stall_price_observations` (market intel,
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
- Ingredient-name routing (open, 2026-10-30): v26 recipe docs carry a compact
  `Item codes (ingredients: …; results: …).` line (`DOCUMENTS_VERSION = 26`,
  `build_recipe_documents`), but multi-item questions route to family-level
  corpus hits + `recipes.raw` blobs — no per-recipe doc is ever fetched, so the
  codes resolve to names in the store yet the answer still quotes raw codes
  (measured 3 loop runs on the mask/hat/apple pricing question: 5/5 prices,
  0/3 ingredient-name runs). Prompt guidance was tried and reverted (no
  measured effect — the run never opened the ingredients table). Candidate:
  retrieval routing that surfaces per-recipe docs for multi-item recipe
  questions (seed ranking prefers family rows today). Prove with a loop run on
  the original wording before committing to the approach.

- Instead of a separate chat window web thing, package this in such a way that I
  can make a custom build of glogger that has this as a tab inside of it
  - first class access to glogger's data

- **BLOCKED on glogger (user) — pg-rag reads snapshots from glogger, game-folder `Reports/` ingest deleted.** glogger already imports every in-game export once into its own tables (`character_snapshots.raw_json` IS the Character export, `character_item_snapshots.raw_json` IS the items export; watcher + 60s poll, so nothing made while pg-rag is down is missed). pg-rag re-ingests the same flat files into `char_reports`/`player_items` AND mirrors glogger's derived tables — the raw JSON lives in three places. Switch session ingest to read `raw_json` from glogger's DB (Release-first, same fallback order as now), keep the 2+1 trim as cache policy, prove parity before deleting the game-folder path. **Blocker: glogger's `character_skill_levels` carries level/bonus/XP but NOT the `Skills[<skill>].Abilities` arrays — the abilities audit reads them from raw JSON, and the sibling repo must import them first (new per-skill ability-list table or extend `character_skill_levels`).**

- **DONE 2026-10-19 — review backlog closed** (retired molab/BM25/buildlock entries died with their code):
  - `scripts/services.py`: `_wait_dead` treats `PermissionError` as alive (keeps polling to the grace deadline, matching the `_kill_tree` POSIX arms); `_write_pid` retries once on `OSError` (transient `WinError 5`) then raises `RuntimeError` instead of running pidless; Windows `_kill_tree` drops `/T` for port-owner pids (single kill + warn, parity with POSIX never-killpg-foreign) — `test_kill_tree_windows_refuses_a_system_pid_and_names_a_port_owner` updated (`ours=False` asserts `taskkill /PID 4242 /F`).
  - `execute_tool` error-as-data gap closed (`src/pgrag/agentic/tools.py`): `_corpus_search` wraps `_conn` (`sqlite3.Error`/`OSError` → `"corpus_search error: …"`) and widens the query guard `OperationalError` → `Error`, copying `_sql_query`; a missing `documents_fts`/locked store now returns a string instead of aborting `run_loop` at the seed (`loop.py:780`). `_get_page`'s unguarded executes marked out-of-scope inline.
  - Curator scheduler (`scripts/curator_scheduler.py`): `rc≠0` no-`save_state` paths pinned by two new tests (curator-fail skips store + state; store-fail skips state), and the module docstring + rebuild comment state the ordering this scheduler owns (curator → store → state) vs `mise sync-*` (source-download(s) → store, never curator).
  - Docstring debt: `documents/chunking.py` docstring + `MAX_EMBED_CHARS` comment rewritten past-tense (chunking policy for FTS5 retrieval, budgets unchanged); `test_chunking.py`/`test_leveling.py`/`conftest.py` prose corrected (conftest now states derived artifacts are excluded from the snapshot by design).
  - `mise.toml`: `golden-flaky-list` renders `recent-miss %` (single `%`). `golden-type` stays pwsh-pinned (documented exception).
