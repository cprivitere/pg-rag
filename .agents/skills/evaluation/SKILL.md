---
name: evaluation
description: The pg-rag golden evaluation harness — every case answered through the agentic tool loop, fact-presence scored, with GEN-side vs RET-side diagnosis, tuning sweeps, and before/after workflow. Use before and after any retrieval, tool, or document-generation change that should be measured.
---

# evaluation — golden eval through the tool loop

The point of this repo's evaluation is making agent changes measurable instead
of "this seems better." Every case is answered by the **agentic tool loop**
(`pgrag.agentic.loop.run_loop`); scoring, tier membership, normalization and
retry live in `src/pgrag/agentic/eval.py`. The one-shot pre-LLM IR-metric suite
(Recall@k/MRR/NDCG) and the embed-model bakeoff were **deleted** with the dense
path — there is no per-stage ranking harness anymore.

## Golden set — `data/golden/*.json`

Each file:

```json
{
  "id": "recipes-using-spider-silk",
  "question": "What recipes can I make with Spider Silk at Nature Appreciation 25?",
  "type": "recipe",
  "facts": [
    ["Spider Silk Tunic", "Spider Silk Robe"],
    ["Nature Appreciation", "25"]
  ]
}
```

- `facts` is a list of variant groups: the answer PASSES if ANY variant in each
  group appears (normalized substring) in the LLM answer. Add a variant, never
  delete one, when the corpus wording legitimately differs.
- `xfail: true` marks a known-gap probe — a persistent miss is expected, but a
  **pass (XPASS) is a failure** forcing you to unflag it.
- `store: true` marks a case whose facts live in the SQLite store (glogger play
  history). The loop reads the store, so it runs through the same path — there
  is no separate store-only harness anymore.
- 62 files exist today (22 entity / 28 general / 7 recipe / 5 comparison),
  spanning recipes-by-ingredient, level-gated crafting, item acquisition/drops,
  ability lookups, comparisons, quest requirements, wiki lore, wiki how-to
  assembly, and player-state audits. 10 store-only cases (player facts,
  `"store": true`). Future additions should favor the thin buckets (comparison
  is 5/62 today).
- **Question-shape rule (learned the hard way, 2026-10-11):** every dimension a
  case's facts check must be asked for by its question. A question that asks
  "where" while its facts also demand loot level and creature type fails a
  correct answer, intermittently (the model answers what was asked). Refactored
  under this rule: `ratkin-root-tender-location`, `gazluk-keep-dungeon-level`,
  `sword-vs-unarmed-burst`, `meditation-synergy-skills`, plus the 8 cases split
  below.
- **Fact-density rule (2026-10-11):** a case whose facts span many docs AND
  require ~10 facts in one answer is flaky no matter how the question is worded —
  the model drops one. Split into focused sub-questions, one per source doc (or
  per audit dimension). Sampled: 10-fact `dungcrafting`/`gardening` failed on
  4-6 facts each while every focused case passes. The split → replacement map:
  `dungcrafting` → `dungcrafting-basics` + `dungcrafting-poop-ability-and-rewards`;
  `gardening` → `gardening-basics` + `gardening-trainers-and-fertilizer` (its
  `giant mantises` fact was DROPPED — no Gardening-grounded source);
  `animal-handling-ability-audit` → `animal-handling-known-abilities` +
  `animal-handling-family-tames`; `royal-jelly-gathering` →
  `royal-jelly-what-is-it` + `royal-jelly-obtaining`;
  `cheesemaking-leveling` → `cheesemaking-leveling-xp` +
  `cheesemaking-leveling-recipes` (2026-10-15: 8 groups in ONE doc family and a
  question that never asked for the two cumulative totals its facts demanded; it
  failed 6 of 12 logged full runs, `6990` missing in 5 of the 6 (all but
  `golden-full-offmode.log`). The set went 61 → 62
  files.
  The 2026-10-11 batch went 54 → 58 files at roughly the same wall time (the
  focused cases are 1-2 rounds each).
- **Residual misses are tool choice, not goldens (2026-10-11).** After the split
  and the `corpus_search` snippet fix (400 → 1000 chars; see the retrieval
  skill), `dungcrafting-*`, `royal-jelly-*` and both audit cases pass in both
  gate modes. `gardening-basics` and `gardening-trainers-and-fertilizer` still
  fail ~every run, each time on a different fact subset: the model answers skill
  questions from the CDB `skills`/`abilities` SQL tables and the wiki level-cap
  trainer page rather than the corpus profile doc, and can assert the opposite
  of the truth ("Gardening grants no bonus levels to any other skill"). Their
  facts are provably reachable (`corpus_search "Gardening trainers Floxie
  Tavilak Kelim"` returns ten `wiki_Gardening_table_0_row_*` hits naming all
  three), so do **not** "fix" them by weakening the facts. Both ids are listed
  in `data/golden/FLAKY.txt`; the fix belongs in the loop's tool-choice
  guidance, not the eval.

## Running it (LLM only)

Every mode needs **only the LLM at :8080** — no embedder, no reranker.

**Two gate modes, same commands** (details: `docs/LLM_MODEL_WIRING.md`):

- **Dev gate (deterministic):** `mise llm-stop`, then `LLM_REASONING=off mise
  llm-start`, then `mise golden-short` / `mise golden-one <id>`. With thinking
  off + temp 0 + seed 0 the run is byte-reproducible, so a FAIL is a real
  regression signal. Not the production config.
- **Acceptance gate (production):** default flags (reasoning ON) + `mise golden`
  over the full set. Single-sample noise is expected; a FAIL is a triage item
  (`--diagnose` labels it GEN-side vs RET-side), not proof.

- `mise golden` — all 62 files through the loop, exit 1 listing missing facts
  on failure. `--tier {short,all}`, `--ids`, `--type
  {entity,recipe,comparison,general}`, `--max-facts N`, `--corpus {full,tool}`,
  `--max-rounds`, `--store`, `--report`, `--diagnose`, `--export [PATH]` are the
  `scripts/golden_check.py` flags.
- `mise golden-short` (`--tier short`) — the 8-case representative tier
  (`SHORT_TIER_IDS` in `pgrag.agentic.eval`); the fast path for a change you
  just made.
- `mise golden-one <id[,id2]>` — rerun named cases (`scripts/golden_rerun.py`;
  comma-separated).
- `mise golden-type <type...>` — run one type's subset (repeatable).
- `mise golden-flaky` / `mise golden-flaky-list` — focus on ids whose recent-10
  miss-share exceeds 50% (plus manual `data/golden/FLAKY.txt`), or audit every
  id's share. Every run appends a `{id, missing, missing_facts, query_type,
  attempt_n, ts}` record to `data/golden/history.jsonl` (an accumulating ledger,
  not a build output).

### Timing (read this before you wait on a run)

Per-case cost is dominated by the reasoning model's rounds (reasoning-ON,
budget 4096): a store-only case with 1–2 rounds answers in tens of seconds, but
a 5–6 round wiki case can take minutes per case. **A full 62-case run is a long
background job — batch it, don't watch it in a foreground terminal.** Use
`mise golden-one` while iterating and `--tier short` for a smoke check.

### Scoring details

- Generation is pinned to `EVAL_GENERATION = {"temperature": 0, "seed": 0}`.
  Reasoning-ON decoding is NOT greedy-pinned even then, so the harness **retries
  once** (2 attempts; the better attempt wins) to damp LLM-side noise — both
  attempts failing is a regression.
- `tests/test_golden_check.py` parametrizes over `data/golden/*.json` at
  collection time → new goldens are AUTO-collected as offline-skipped tests
  (skip unless the LLM is up); `-m short` selects the tier, the default
  `not slow` deselects the long tier. No test code per golden.

### Diagnosis: GEN-side vs RET-side

`--diagnose` uses `run_loop`'s returned `documents` (exactly what the model was
fed) and `trace_rounds` (per tool round) to label each FAIL:

- **GEN-side** — the missing fact was in the fed context; the model dropped it.
  Fix the prompt / tool contract, not retrieval.
- **RET-side** — no tool round ever surfaced it. The model queried wrong, or the
  fact is absent from the store/corpus. Route first, diagnose second.
- `--export [PATH]` writes, per case, the exact fed context + question/type/facts
  to a JSONL bundle for oracle replay on a stronger reader (bare writes
  `data/golden/golden_context_bundle.jsonl`).

## Live-session goldens (answer-then-validate workflow)

Standing session workflow: the user asks a gameplay question → research the
answer against the live store + corpus → give the answer. **A new golden is the
exception, not the default.** Pin one ONLY when the loop answers materially
worse than you can, and only AFTER the user confirms. A question the loop
already handles adds no signal; every golden is a permanent acceptance gate.

1. **Research** against the store (`data/sqlite_gorgon.db`) and corpus. Sync
   first when the user just played: `ingest_glogger`/`ingest_session` are
   incremental (or run `mise agentic-chat`, whose preflight syncs).
2. **Answer the user.** Ground every claim in a store table or corpus doc; note
   any fact you derived by hand (a JOIN, arithmetic, name-mapping) — that is
   exactly the part the model may fumble.
3. **Measure before proposing a golden.** Ask the same question back through the
   loop (`mise agentic-chat`) and compare against your oracle answer. A golden is
   warranted ONLY when that run is lower quality than it should be: a fact
   missing, a wrong route, or an answer that needed manual SQL gymnastics the
   model will not repeat. If the loop answers as well as you did, stop — no
   golden.
4. **Ask the user before writing a golden.** Even when step 3 shows a gap, state
   the measured gap plus the proposed id and facts, and get an explicit yes.
5. **Derive fact groups** from what you verified (grep the store /
   `documents.json` first — an un-grounded fact is a permanently-failing test).
6. **Route the golden:** facts from the player's own state (sheet abilities,
   stall ledger, kills, favor) get `"store": true`; CDN/wiki facts stay plain.
7. **Validate before committing:** `mise golden-one <id>` (or `--diagnose`). A
   FAIL means the model can't reproduce your answer — diagnose and fix the gap
   (tool, prompt note, doc builder) before committing. A golden that fails on
   commit is a known-gap trap, not a convenience.
8. **Commit** the golden together with whatever fix it forced.

### Traps learned the hard way (each cost a debug session)

- **`abilities.level_req` is not a known-gate.** Shrine/riddle unlocks
  (TameBear/TameCat) carry `level_req=1` yet may be unknown. The known list lives
  ONLY in `char_reports` sheet `Skills[<skill>].Abilities`; non-skill abilities
  (CharmRat) are bucketed under the pseudo-skill `"Unknown"` — audit both.
  `player_state section=abilities` does this diff.
- **`game_state_skills` is fresher than the sheet dump for LEVELS** (log-sourced
  vs export-time); the sheet is the only source for ability LISTS. Cross-check
  both; state which timestamp each claim comes from.
- **A store-only fact can never appear in the corpus.** Diagnosing a store-only
  case via `corpus_search` alone gives a guaranteed RET-side false signal —
  route first, diagnose second.
- **Name mismatches across sources:** wiki "Bear Anatomy" ↔ CDN/game "Bear and
  Bugbear Anatomy"; sheet keys have no spaces (`AnimalHandling`). Normalize
  before diffing.
- **A personal-data answer usually implies a schema/tool gap.** The AH audit
  session produced `player_state section=abilities`; the stall session produced
  the stall classifier. If you needed manual SQL gymnastics to answer a question
  the model will get again, that gymnastics belongs in a tool or doc-builder.
- **Pricing has four layers; name them or the model answers the wrong one.**
  `items.value` is the CDN base value (the vendor floor); a **work order** is
  the guaranteed floor for items that are a Collect objective (fixed councils ÷
  required count — e.g. 5 Phoenix Eggs → 120,833 = 24,167 each, Industry 60
  scroll); `stall_events` holds realized prices — but only from the player's OWN
  stall; `stall_price_observations` holds what the player saw at *other*
  players' stalls (typed/OCR captures + auto-detected purchases, `price_unit 0`
  = price pending — dated observations, never own sales and never a market
  average); and `chat_events` holds
  other players' *asking* prices, which the tool attempts best-effort
  (price-token filter over the social channels, labeled `CHAT PRICE MENTIONS …
  NOT realized sales`). A "pricing" question answered only from `items.value`
  is a routing miss: `player_state section=stall` lists each realized sale
  individually (averages erase the spread) and prints the item's CDN base value
  in every branch (sales / asking / zero-sale — plus the work-order payout and
  chat mentions when they exist), so a zero-stall item falls back to base value
  + `sources`→`npcs`/`quests` — or to a dated chat mention — instead of
  dead-ending (golden `stall-price-slabs-sands-gizmos`).
- **Synergy is directional, and the CDN stores it one way only.** A skill's
  `Rewards[level].BonusToSkill = X` means that skill grants a bonus level TO
  `X` (outgoing). "What gives synergy to Meditation?" needs the *incoming*
  direction — the skill profile carries a computed
  `Synergy Levels (bonus levels received from other skills)` section (inverse
  over every skill's `Rewards`).
- **An answered question is not automatically a golden.** Auto-pinning every
  session question bloats the set with cases the loop already passes. Intake is
  gated on a *measured* gap plus explicit user confirmation.

## Tuning sweeps

`mise agentic-tune` (`scripts/agentic_tune.py`) sweeps (model, temperature, seed,
reasoning budget, max-rounds) into `data/agentic_tune/<run-id>.json`
(skip-if-exists, `--force` re-runs, `--compare` tabulates). Measured outcome:
**temp 0 confirmed** (no candidate beat it on any seed — goldens stay temp
0/seed 0), **reasoning budget 4096 kept**, **Ornith-1.5-9B kept**. The tuning
case set lives in `data/eval/tune_cases.json` (never `data/golden/`).

## Before/after workflow

1. Pick a focused golden set (or write a new golden for the failing case).
2. Run it → record baseline (`--report` / `data/agentic_eval_report.json`).
3. Make the change (one stage).
4. Re-run — compare pass/fail AND read the `--diagnose` GEN/RET labels.
5. If the "fix" only moved a problem downstream, go back to stage localization
   (`retrieval` skill).

## Legacy: pre-LLM IR suite (DELETED)

The `eval-retrieval` / `eval-offline` commands, the pre-LLM IR suite
(retrieval_eval in scripts and src, plus its test), and the embed bakeoff
(embed_eval / bakeoff_corpus) were deleted with the dense path — none of those
files exist, including `evaluation/queries.jsonl` (deleted 2026-10-11 with the
rest of the orphaned artifacts). Do not rebuild an IR harness: the loop's own
gate (`mise golden` + `--diagnose`) is the measurement surface.
