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

- Publish the sqlite store privately and run the tool loop on a molab notebook
  - Done: `mise upload-store` (`scripts/publish_store.py`) publishes a `VACUUM INTO`
    snapshot + manifest (sizes, sha256, row counts) + a reproducible `src`/`scripts`
    tarball of the worktree to the **private** `hf://buckets/Nubula/paddock-private`;
    it refuses the public corpus bucket and asserts `bucket_info(...).private` before
    the first byte. The notebook gained `store_access` / `store_snapshot` / `pgrag_src`
    / `sidecar_tools_probe` cells plus a `corpus chat` / `tool loop` mode selector, and
    `loop._post` reads `PGRAG_LLM_URL` / `PGRAG_LLM_MODEL` / `PGRAG_LLM_NATIVE_TOOLS`
    at call time; the sidecar now boots with `--max-model-len 24576` and a
    discovered tool-call parser. See `docs/MOLAB_OPS.md` → "Private store bucket".
  - Remaining: the first fresh-sandbox run-all — record the parser the sidecar
    reports (`TOOL_PARSER …`) and the tool-loop round count/latency there. The path
    itself is verified locally (published tarball + snapshot run under Python 3.13,
    2 tool rounds), but the sandbox numbers are still unmeasured.
