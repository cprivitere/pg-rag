---
description: Frozen-tree review pass — freeze the tag, one facts sheet, tiered agents, report only
argumentHint: "<tag|sha> <base-tag> [focus]"
---

Run a **frozen-tree review pass**, not a diff review of a moving branch.

TARGET: `$1` — a tag or SHA (e.g. `review-pass5`), and the operator focus below.
If `$1` is empty, TARGET is the newest `review-*` tag and BASE the one before it —
state which pair you resolved, and stop if either does not resolve.

# Scope

- Review **only** the committed range `BASE..TARGET`. BASE is the `review-*` tag named in the focus text;
  if none is named, use the newest `review-*` tag that is not TARGET (`git tag -l 'review-*' | sort`).
  Do **not** use `origin/HEAD...main` unless it resolves to the same commit.
- **Verify before reviewing**, and stop if either fails: `git rev-parse TARGET` resolves, and
  `git status --porcelain` is empty for the tree under review. A dirty tree means the review target is
  moving; report that instead of reviewing it.
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

- Three risk tiers, **at most 8 concurrent agents** (the host parks more than that, and a parked agent
  must be revived one message at a time):
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
- what could not be verified, and why.

Close-out rule: a pass closes the review when it returns 0 critical, 0 high and 0 fix-now mediums in
changed code on a frozen tag. Anything else goes to the backlog — that is ordinary work, not another
review pass. At most one further **verification-only** pass is allowed (verify the fixes, no new scope).
