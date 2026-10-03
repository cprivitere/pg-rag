# Agentic loop tuning sweeps

Run reports from `scripts/agentic_tune.py` (one JSON per sweep run, keyed by
run-id = eval-set + model + temperature + seed + reasoning-budget + max-rounds).
Tabulate: `uv run python scripts/agentic_tune.py --compare`.

## Environment

- Model: Ornith-1.5-9B (Q4_K_M, `--reasoning-budget 4096`) unless noted.
- Eval sets: `cases` = data/eval/tune_cases.json (6 cases); `golden-short`
  = the 8 golden parity cases (always temp 0 / seed 0).
- Baseline lineage: the loop gained schema-note guidance (abilities PvE
  damage via `json_extract`, chat_events indexed counts, leveling math via
  xptables) and `corpus_search` seed k 10 -> 12 BEFORE the sweeps; the
  t0/b4096 rows below include those fixes (which took the cases baseline
  from 4/6 to 6/6).

## Temperature sweep (cases, seeds 0/1/2)

| temp | pass (s0/s1/s2) | facts missing |
|---|---|---|
| 0.0 (baseline) | 6/6 | 0 |
| 0.2 | 5/6 / 5/6 / 5/6 | 1 each |
| 0.4 | 5/6 / 5/6 / 6/6 | 1/1/0 |
| 0.7 | 5/6 / 5/6 / 4/6 | 1/1/2 |

**Decision rule** (pre-committed): adopt T only if pass-rate(T) > temp-0 on
2 of 3 seeds AND no case regresses.

**Verdict: temp 0 confirmed.** No candidate beat the baseline on any seed;
0.2 and 0.7 were worse on all three. H1 (mild temp breaks tool-loop
deadlocks) is refuted on this set — the loop's retry path + schema notes
already prevent deadlock; sampling variation only adds wrong numbers. The
golden parity set stays temp 0/seed 0 (contract).

## Reasoning-budget sweep (cases, temp 0, seed 0)

| budget | pass | facts missing | wall |
|---|---|---|---|
| 4096 (current) | 6/6 | 0 | 100s |
| 1024 | 4/6 | 2 | 207s |
| 256 | 5/6 | 1 | 146s |

**Verdict: keep 4096.** The empty-content failure mode observed during
Phase A (budget starvation) was already mitigated loop-side (doubled-token
digest fallback on the forced-final turn); shrinking the budget just
truncates the model's own reasoning mid-tool-use (chat-activity degraded
in both lower-budget runs).

## Model comparison (cases, temp 0, seed 0)

| model | pass | facts missing | wall | notes |
|---|---|---|---|---|
| Ornith-1.5-9B (current) | 6/6 | 0 | 100s | baseline |
| gemma-4-26B-A4B-qat | 5/6 | 2 | 78s | missed npc-gift-preferences (answered "Clubs and Loot", not the gift NAMES); faster per round |

Harness note: the first gemma run (bg_760/762) silently reused the running
Ornith server — `mise run llm-start` re-applies mise.toml `[env]`, so
inherited `LLM_MODEL` overrides are discarded. The harness now spawns
`llama-server` directly (`_spawn_server`) and waits for the fresh model to
bind; the log line `load_model: loading model 'unsloth/gemma-4-26B...'`
confirms the reported run served gemma. The invalid first gemma report was
deleted.

| qwen-3.8-27B | 6/6 | 0 | 264s | needs the loop's forced-final notice as user-role (qwen MoE templates reject a 2nd system message); ~2.6x slower than Ornith |

**Model verdict: keep Ornith-1.5-9B as production.** Qwen-3.8-27B ties 6/6
but at ~2.6x the latency and (Q2 quant) 65-layer density — no accuracy gain
on the parity-relevant set; gemma-26b regressed a case. The template
constraint found (2nd system message rejected by qwen MoE templates) is
already fixed loop-side, so future model swaps are unblocked.

## Golden-short post-sweep runs (reruns of the parity set, temp 0/seed 0)

Two `mise golden-short` runs after the sweeps failed the same case
(grow-field-mushrooms, fact "5 hours") — root cause: the reranker (:8082)
was DOWN during those runs, degrading retrieval to the lexical fallback
(the fact lives deep inside the Mushroom Farming wiki page chunk). With
:8082 up the case passes immediately (`golden_rerun --id
grow-field-mushrooms` → PASS). Takeaway: golden parity comparisons require
ALL services up (LLM + embed + reranker), matching the mise golden
contract; a down reranker is an eval-environment change, not a regression.
