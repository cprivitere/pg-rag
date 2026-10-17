---
description: Frozen-tree review pass — freeze the tag, one facts sheet, tiered agents, report only
argumentHint: "<tag|sha> [base tag or -] [focus...]"
---

Run a **frozen-tree review pass**, not a diff review of a moving branch.

TARGET: `$1` — a tag or SHA (e.g. `review-pass6`).
BASE: `$2` — a tag or SHA, or `-` for "derive it".
FOCUS: `$@[3]` — the operator's focus text, if any.

If TARGET is missing, use the newest `review-pass*` tag. A BASE that is `-`, empty or does not resolve as
a revision (`git rev-parse` fails) is *derived*, never assumed — and such a value is really the head of
the focus, so read FOCUS as the BASE slot's value followed by everything after it. The splitter drops an
empty *quoted* argument (measured: with `A "" B C` the second argument is `B`), so test the value, never
the position. State the pair you resolved, and stop if either does not resolve.

# Scope

- Review **only** the committed range `BASE..TARGET`. Derive a missing BASE with
  `git describe --tags --match 'review-*' --abbrev=0 <TARGET>^` — the nearest `review-*` tag behind the
  target, i.e. the base marker the freeze step created (verified on this history: `review-pass5^` →
  `review-base-5ddd05b`, `review-pass6^` → `review-pass5`). Never `git tag -l 'review-*' | sort`: it is
  lexicographic (`review-pass10` sorts before `review-pass5`) and can select a `review-base-*` marker.
  Do **not** use `origin/HEAD...main` unless it resolves to the same commit.
- **Verify before reviewing.** The invariant is that the diff handed to a reviewer and the files it
  reads are the *same revision*, so a frozen-tag pass needs both: `git rev-parse <TARGET>` resolves and
  `git status --porcelain` is empty. A dirty tree means the target is moving — report that instead of
  reviewing it; if the checkout is merely ahead of the tag, `git worktree add ../review-<tag> <tag>`
  puts every reviewer (and every path they execute) at the frozen SHA, main checkout untouched.
- **Report only.** No edits, no corpus builds (`mise sync-*` / `build-*` / `generate-docs`), no service
  start/stop. Fixes happen in a later, separate turn, on a new frozen tag.

# Facts — measured once, never re-derived

- The facts sheet is the shared context: `uv run python scripts/review_facts.py` (read-only, ~4 s warm;
  `--no-gates` for ~1.6 s, `--json` for machines). Paste its output into every agent's brief.
- Do **not** re-run the rows the sheet already carries — `ruff`, `mise drift` and `mise compliance` — in a
  slice; a red row is a *fact to report once*, not a finding to raise per file. The sheet does **not**
  carry the suite verdict (a full `uv run pytest`) or `mise lint-ts`: the one runner measures both once,
  and no slice runs the suite.
- `docs/REVIEW.md` → "Leftovers for the next pass" is the **accepted backlog**: out of scope. Never
  re-raise those items as new findings, and do not widen scope to untouched files except where one makes
  a claim about a file that changed.

# Fan-out

- Three risk tiers, **at most 8 concurrent agents**. 8 is a budget for a round, not a host ceiling: the
  host parks more than that (sixteen on one host in an early pass, and a broadcast revived exactly one),
  and a parked agent must be revived one message at a time:
  1. changed **logic** — deep, and allowed to execute code paths (both high findings of the last pass
     came from an agent running the path rather than reading it);
  2. wiring/config (`mise.toml`, hooks, gates);
  3. docs/data — one batched agent, or leave it to `mise drift`.
- **One runner.** The parent runs the shared commands once — the facts sheet plus the two verdicts it
  does not carry (`uv run pytest` and `node scripts/check_ts.mjs`); subagents never do.

# Report

Per finding: severity (critical / high / medium / low / nit) · `file:line` · the defect · evidence (an
exact quote, command, or output) · the fix · `verified` or `[INFERENCE]`. Then:

- a verdict — **ship** / **ship-with-fixes** / **blocked**;
- an explicit **fix-now vs backlog** split (fix-now = a defect in code this range changed);
- the one runner's two verdicts as measured on the frozen SHA — `uv run pytest` (counts and conclusion)
  and `node scripts/check_ts.mjs`;
- what could not be verified, and why.

Close-out rule: a pass closes the review when it returns 0 critical, 0 high and 0 fix-now mediums in
changed code on a frozen tag, **and** the suite verdict above is green on that SHA — nothing else in the
pass establishes it, and this repo has no CI, so a red suite is a blocked pass, not a finding. Anything
else goes to the backlog — that is ordinary work, not another review pass. At most one further
**verification-only** pass is allowed (verify the fixes, no new scope).
