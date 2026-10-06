# Ideas to try

- Burn down the flaky golden queue (`mise golden-flaky-list`): `animal-handling-ability-audit`
  100% (1 record, store-only -> `mise agentic-eval`), `ratkin-root-tender-location` 75%,
  `strom-farblood-favor` 75%, `astounding-ink-acquisition` 67%,
  `meditation-synergy-skills` 57%. Triage each with
  `uv run python scripts/golden_rerun.py --id <id> --diagnose`: GEN-SIDE (fact in
  context, LLM dropped it) -> corpus/prompt-shape fix; RET-SIDE -> retrieval. Fix the
  source, never the assertion.

- Golden coverage: comparison is the thinnest bucket (6 of 61). Intake for new goldens
  is live-session questions; the workflow lives in `.agents/skills/evaluation/SKILL.md`.
  Ground every fact with `grep data/documents.json` before adding it.

- Track pricing of items in player stalls and player work orders
  - Own stall: done — `stall_events` ledger + `player_state section=stall`.
  - Other players' stalls: glogger's Personal-build `stall_price_observations`
    (v68 market intel — "prices observed at OTHER players' stalls", 0 sentinel =
    price pending) is mirrored into the store but read by nothing, and both the tool
    output (`src/pgrag/agentic/tools.py:573`) and the loop's schema note
    (`src/pgrag/agentic/loop.py:240`) still tell the model "this store has no other
    players' stalls". Update those notes and surface the table (now: 3 rows, all
    `price_unit=0`).
  - Player work orders (signboard orders you post and price): no store surface at all —
    no `work_order` table, ingest or parser anywhere in `src/`/`scripts/`; only CDN
    NPC work-order turn-ins and `WORKORDER_COIN_REWARD_MOD` exist. Needs a new source
    (glogger / game-log / session parsing).

- Sync scripts for game state
  - `mise agentic-chat` already refreshes session + glogger sources before the loop
    (`scripts/agentic_chat.py::_preflight_sync`, `--no-sync` skips).
  - Remaining: `mise chat` (Gradio) and `mise agentic-eval` start cold; decide whether
    a shared staleness check replaces the daemon idea.

- Instead of a separate chat window web thing, package this in such a way that I can
  make a custom build of glogger that has this as a tab inside of it
  - first class access to glogger's data

- Figure out a way to publish the sqlite data privately (or do the same trick we do
  with the private version of the full corpus) and do the inference/tool loops on a
  molab notebook
  - `docs/MOLAB_OPS.md` covers only the public `documents.json` bucket today, and
    `notebooks/molab-mirror/notebook.py` ships no sqlite/tool-loop code.
