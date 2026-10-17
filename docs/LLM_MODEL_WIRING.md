# LLM Model Wiring Reference

Source of truth for how to launch the pg-rag LLM. The `LLM_MODEL` /
`LLM_FLAGS` pair lives in `mise.toml [env]` and is consumed by
`scripts/services.py` (`llama-server -hf $LLM_MODEL $LLM_FLAGS
--host 0.0.0.0 --port 8080 -np 1 --log-file logs/llm.log`, read via
`os.environ` and shared by `mise llm-start`, `mise debug-llm` and
`scripts/agentic_tune.py --llm-restart`), and by `scripts/check_docs.py`
(tomllib). `mise drift` fails if any consumer hardcodes a model literal.

A `mise.toml` entry always sets the **current** model; swap by editing the two
vars there, then `mise llm-stop` → `mise llm-start` (stop tree-kills, verified
to actually free :8080).

**Eval path.** Every goldens run now goes through the **agentic tool loop**
(`mise golden`, `scripts/golden_check.py` → `pgrag.agentic.eval.run_case` →
`run_loop`), not the deleted one-shot dense pipeline. Generation is pinned to
`EVAL_GENERATION = {"temperature": 0, "seed": 0}` in `pgrag.agentic.eval`. All
"facts missing" numbers below are historical single-draw measurements on the
older pipeline harness at set sizes that have since grown to 62; they are kept
as comparative evidence for model/flag choices, not as current golden verdicts.

## Common flags (all models)

- `-ngl 999 -fa on` — full offload, flash attention.
- `-c 65536` (64k) on the production winner, `-c 32768` on several historical
  rows — real fed context is a few k to ~20k tokens; with
  reasoning@4096 + ≤8k answer the worst case ≈ 32k, so 64k keeps headroom on
  the production model while 32k was safe for the contender rows.
- `-ctk q8_0 -ctv q8_0` — Q8 KV. **Never Q4 on the V axis** (the fact-extraction
  axis golden measures). Keep unless VRAM forbids.
- `--no-mmproj` (double-dash; this build rejects `-no-mmproj`) — drop the vision
  projector for text-only RAG (~0.16 GiB). Add to any model bundled with an
  `mmproj` whose RAG path never sends images.

## Ornith-1.5-9B — current production

Dense ~9B reasoning model (built on Qwen3.5 + Gemma4; the GGUF's embedded chat
template renders correctly, **no** `--jinja`) with a vision projector bundled
in repo (dropped via `--no-mmproj`). No MTP head (dense — Qwen3.5-9B-MTP's
spec-decode does NOT apply); native thinking via `--reasoning-budget 4096`.

The two lines below are a **copy for reading** of the `mise.toml [env]` pair —
`LLM_MODEL`/`LLM_FLAGS` there are the authority; if this block and `[env]`
disagree, `[env]` wins.

```toml
LLM_MODEL = "ornith-ai/Ornith-1.5-9B-GGUF:Q4_K_M"
LLM_FLAGS = "--no-mmproj -ngl 999 -fa on -c 65536 -ctk q8_0 -ctv q8_0 --reasoning-budget 4096"
```

- Weights 5.78 GiB (Q4_K_M); measured **6,580 MiB** (~6.6 GB — lightest of all
  contenders). 64k context because reasoning@4096 + ≤~20k ctx + ≤8k ans ≈ ≤32k,
  and the densest content edge (~2.4 chars/token) can approach 32k.
- Its record run reported **0 facts missing on the 38-case set** of the time
  (presence-only, single noisy reasoning-ON draw — verbosity games the metric;
  hallucination is not penalized). Deterministic re-baseline was captured on the
  contenders below, not re-run on this model; treat the clean sweep as a
  best-effort single sample.

## Qwen3.5-9B-MTP

Mamba2-hybrid, 4 KV heads → tiny cache; MTP head bundled in the main GGUF
(`nextn_predict_layers=1`), so `--spec-type draft-mtp` works single-file.

```toml
LLM_MODEL = "unsloth/Qwen3.5-9B-MTP-GGUF:UD-Q4_K_XL"
LLM_FLAGS = "--spec-type draft-mtp --spec-draft-n-max 6 -ngl 999 -fa on -c 65536 -ctk q8_0 -ctv q8_0 --reasoning-budget 4096"
```

- Native thinking (`--reasoning-budget 4096`), MTP spec-decode n6.
- VRAM: **8187 MiB @32k** (lighter than the 8B granite). KV @32k Q8 = 1.03 GiB.
- Golden: **31 PASS / 3 FAIL, 8 facts missing** (historical set).

## Granite-4.1-3B / 8B (superseded)

```toml
# 8B (UD-Q4_K_XL):
LLM_MODEL = "unsloth/granite-4.1-8b-GGUF:UD-Q4_K_XL"
LLM_FLAGS = "--jinja -ngl 999 -fa on -c 32768 -ctk q8_0 -ctv q8_0"
# 3B (Q8_0):
LLM_MODEL = "unsloth/granite-4.1-3b-GGUF:Q8_0"
LLM_FLAGS = "--jinja -ngl 999 -fa on -c 32768 -ctk q8_0 -ctv q8_0"
```

- `--jinja` **required** (unsloth card): loads the GGUF's embedded chat-template
  fix. No thinking mode and no MTP head → no `--reasoning-budget` /
  `--spec-type`.
- KV: 8B = 40 layers × 8 KV heads × 128 = 2.6 GiB @32k.
- Golden: 3B = 17/17, **34 missing** · 8B = 25/9, **20 missing**.

## Gemma-4 family

Native thinking (enable via `--reasoning-budget`); hybrid sliding-window
attention (tiny KV). No `--jinja` needed (built-in template). MTP head ships as
a **separate** file auto-discovered by recent llama.cpp from `-hf` — do **not**
pass `--model-draft` (unsloth card). Context 256K.

### gemma-4-12B-it-qat (48L Dense, QAT-lossless UD-Q4)

```toml
LLM_MODEL = "unsloth/gemma-4-12B-it-qat-GGUF:UD-Q4_K_XL"
LLM_FLAGS = "--spec-type draft-mtp --spec-draft-n-max 4 --no-mmproj -ngl 999 -fa on -c 32768 -ctk q8_0 -ctv q8_0 --reasoning-budget 1024"
```

- MTP n4 (draft `mtp-gemma-4-12B-it.gguf` auto-discovered), thinking budget 1024
  (budget A/B/C 2026-08, reasoning-ON, 6 cases × 2 runs: **1024** → 0/12 empty +
  34/35 facts; **4096** → 0/12 empty + 33/35; **default -1** → 1/12 empty +
  33/35; 1024 stays).
- `--no-mmproj` + `-c 32768` shaved **8,019 MiB** (was 8,741). Weights 6.26 GiB.
- Golden: **31 PASS / 3 FAIL, 7 facts missing** (≈ qwen 9B's 8; deterministic
  re-baseline below: 8 missing vs qwen 12).

### gemma-4-26B-A4B-it-qat (30L MoE, 3.8B active, QAT-lossless UD-Q4)

```toml
LLM_MODEL = "unsloth/gemma-4-26B-A4B-it-qat-GGUF:UD-Q4_K_XL"
LLM_FLAGS = "--spec-type draft-mtp --spec-draft-n-max 4 -ngl 999 -fa on -c 65536 -ctk q8_0 -ctv q8_0 --reasoning-budget 4096"
```

- MoE runs ~like a 4B for speed. Weights 13.27 GiB, measured **17,224 MiB**.
- Golden: **27 PASS / 7 FAIL, 12 facts missing** — worse than the 12B (7) and
  qwen (8); sparse MoE routing + terse output drops exhaustive facts.

### gemma-4-26B-A4B-it (non-QAT, UD-Q2_K_XL)

```toml
LLM_MODEL = "unsloth/gemma-4-26B-A4B-it-GGUF:UD-Q2_K_XL"
LLM_FLAGS = "-ngl 999 -fa on -c 65536 -ctk q8_0 -ctv q8_0 --reasoning-budget 4096"
```

## Qwen3.8-27B (UD-Q2_K_XL) — ruled out

```toml
LLM_MODEL = "unsloth/Qwen3.8-27B-GGUF:UD-Q2_K_XL"
LLM_FLAGS = "--spec-type draft-mtp --spec-draft-n-max 6 -ngl 999 -fa on -c 65536 -ctk q8_0 -ctv q8_0 --reasoning-budget 4096"
```

- **Dense** (qwen35 Mamba2-hybrid, 65L, 4 KV heads → tiny cache). MTP head
  bundled → single-file `draft-mtp`. Native thinking. Measured **15,339 MiB**.
- Golden: **29 PASS / 5 FAIL, 10 facts missing** — Q2 capacity does NOT beat
  lossless-QAT density; Q2 damaged extraction.

## Card flags & determinism (empirically validated 2026-08)

Unsloth's llama-server launch guide recommends `--chat-template-kwargs
{"enable_thinking":…}` + card sampling `--temp 1.0 --top-p 0.95 --top-k 64`.
Validated against this llama.cpp:

- **`enable_thinking` is deprecated in our build** — the server logs
  `Setting 'enable_thinking' via --chat-template-kwargs is deprecated. Use
  --reasoning on / --reasoning off instead`. The modern, non-deprecated synonym
  is `--reasoning on/off` (same semantics). Reasoning defaults **ON** even with
  no flag (matches the card's intent), so production `LLM_FLAGS` needs no
  explicit `--reasoning` — the default already IS reasoning-on.
- **Card sampling is non-reproducible.** `--temp 1.0 --top-p 0.95 --top-k 64`
  diverges run-to-run on any real multi-clause answer even with a fixed `seed` —
  sampling diversity defeats the seed in this build. It is for open chat, NOT a
  benchmark. The golden gate therefore uses greedy **temp 0 + fixed seed**
  (`EVAL_GENERATION` in `pgrag.agentic.eval`).
- **Determinism rule of thumb**: reasoning ON + greedy is NOT reproducible (the
  thinking trajectory isn't greedy-pinned — observed 7/8/10 single-draw golden
  variance was LLM-side, not a harness miss). reasoning OFF (`--reasoning off`)
  + greedy + seed IS byte-reproducible for gemma AND qwen. The golden harness
  absorbs the residual noise with a **retry-once** (better attempt wins).

### Dev gate vs acceptance gate (two modes, one harness)

Same `mise golden*` commands; only the *server flags* differ.

- **Dev gate — deterministic signal while you change things.** Run the LLM with
  reasoning off:

  ```powershell
  mise llm-stop
  $env:LLM_REASONING = 'off'
  mise llm-start
  mise golden-short               # a FAIL now means a real change, not a coin flip
  ```

  POSIX form — the var is read at spawn, so an inline assignment reaches the
  server just as well:

  ```bash
  LLM_REASONING=off mise llm-start
  ```

  `scripts/services.py` appends `--reasoning off` when `LLM_REASONING=off`
  is set (it prints the switch so the task output records which mode served the
  run).
  Cost: this is **not** the production config — with thinking off, per-case
  latency drops hard, but the run measures routing + plain generation, not the
  production answer. Use it to answer "did my change break something?", not
  "does production still pass?".
- **Acceptance gate — the production config.** Default flags (reasoning ON,
  `--reasoning-budget 4096`), `mise golden` over the full set. Reasoning-ON
  single-sample noise is expected and absorbed by the retry-once; treat a failure
  as a triage item (GEN-side vs RET-side via `--diagnose`), not as proof.

Revert with `mise llm-stop; $env:LLM_REASONING = ''; mise llm-start` — POSIX:
`mise llm-stop && mise llm-start` (the inline assignment never persisted) (or
remove `--reasoning off` from `LLM_FLAGS` if you added it there instead).

### Deterministic re-baseline (identical conditions: reasoning OFF, temp 0,
seed 0)

| model | facts missing | FAIL cases |
|---|---|---|
| **gemma-4-12B-it-qat** | **8** | dungcrafting, gardening, grow-field-mushrooms, recipes_using_animal_feces |
| **Qwen3.5-9B-MTP** | **12** | + field-mushroom-locations, pig_poop |

Former noisy "7 vs 8" was a single lucky draw for qwen; deterministically gemma
leads by a real 4-fact margin. Production reasoning-ON spot-check on the FAIL
set: gemma recovers `recipes_using_animal_feces` **5/5**; qwen **0/5** even with
reasoning. `gardening/spade assault` is **GEN-side** (the model omits a fact
that is in the fed context), not a retrieval gap.

## Final leaderboard (facts missing, lower = better; historical sets)

| rank | model | missing | FAIL | VRAM |
|---|---|---|---|---|
| 1 | **gemma-4-12B-it-qat** | **7** | 3 | 8.0 |
| 2 | Qwen3.5-9B-MTP | 8 | 3 | 8.2 |
| 2 | gemma-26B-A4B Q2 | 8 | 4 | — |
| 4 | Qwen3.8-27B Q2 | 10 | 5 | 15.3 |
| 5 | gemma-26B-A4B-it-qat | 12 | 7 | 17.2 |
| 6 | granite-4.1-8b | 20 | 9 | 8.4 |
| 7 | granite-4.1-3b | 34 | 17 | — |

The deterministic reasoning-OFF re-baseline re-measured the top two (gemma 8,
qwen 12) — same ordering.

Winner: **Ornith-1.5-9B** — current production selection (`LLM_MODEL`/
`LLM_FLAGS` in `mise.toml [env]`; flags in its section above). Its record run
reported 0 missing (~6.6 GiB, lightest). The prior winner,
**gemma-4-12B-it-qat**, stays the documented dense+QAT benchmark (7 facts
missing on the older set). The swap lives in `mise.toml [env]` — treat that file
as the source of truth for what ships.
