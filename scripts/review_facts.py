"""Shared facts for a review pass — computed once, handed to every reviewer.

A review fans out over the same questions from every slice: how many tests does
the suite collect, do the gates pass, which tasks exist, is the tree frozen,
what does the platform say about a dead pid. Re-deriving them in each brief cost
roughly a third of the last pass's wall time and produced three "findings" that
were really just facts a machine already knew (a stale count, an untracked gate
runner, a drift status).

Run it once on the frozen SHA (`uv run python scripts/review_facts.py`, `--json`
for machines) and paste the output into the *shared context* of every reviewer
brief, with the instruction not to re-run the rows it already carries.

Read-only by construction: it inspects, it writes nothing git-visible (bytecode
and the ruff cache only), and it starts no service. The gate rows (ruff / drift /
compliance) are the slow part (~2.5 s warm here, ~7 s cold) — `--no-gates` skips
them, `--json` emits the same facts for a machine, and an interrupt exits 130
without a traceback (nothing was half-written). Two verdicts are deliberately
**not** rows: the suite's (`uv run pytest` costs minutes, and `counts` is a
*collection*, not a run) and the TypeScript gate's (`mise lint-ts`).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The runner files the repo's gates shell out to: if one is untracked, a fresh
# clone has no TypeScript gate at all (and its tests only *skip*, so nothing
# reports that).
GATE_FILES = ("scripts/check_ts.mjs", "package.json", "package-lock.json")

GATE_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ruff", ("uv", "run", "ruff", "check", "src", "scripts", "tests")),
    ("drift", ("uv", "run", "python", "scripts/check_docs.py")),
    ("compliance", ("uv", "run", "python", "scripts/check_constraints.py")),
)

# Inputs that exist only in a working tree (gitignored `data/`): a clone-only
# check cannot see them, and a reviewer must not reason about them as if they
# were committed.
LIMIT_PATHS = (
    "data/documents.json",
    "data/bm25_index.pkl",
    "data/tool_documents.json",
    "data/tool_bm25.pkl",
    "data/sqlite_gorgon.db",
    "data/derived/documents_version.json",
    "data/wiki/.meta.json",
    "data/golden",
)

# A pid no process can own, for the liveness row below.
RELEASED_PID = 2147483646


def _run_raw(argv: tuple[str, ...] | list[str]) -> tuple[int, str]:
    """`(returncode, stdout+stderr)`; `(-1, reason)` when it cannot even start."""
    try:
        # An explicit codec with `errors="replace"`: one non-UTF-8 byte under the
        # strict default kills subprocess's reader thread, `stdout` comes back
        # `None`, and the `+` below then raises a `TypeError` that neither this
        # guard nor `main` catches (the fix `scripts/services.py` already carries).
        proc = subprocess.run(
            argv,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _run(argv: tuple[str, ...] | list[str]) -> tuple[int, str]:
    """`(returncode, one-line summary)`. A non-zero rc is a *fact* here, not an
    error — `drift` failing is exactly what a reviewer needs, so the summary keeps
    the diagnosis as well as the trailer: each gate prints its verdict *after* the
    finding it describes (ruff's `Found N errors`, compliance's "No carve-outs…",
    drift's per-drift lines), so the last line alone would name none of them."""
    rc, out = _run_raw(argv)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not lines:
        return rc, ""
    head = lines[:2]
    tail = lines[-1:] if len(lines) > len(head) else []
    return rc, " | ".join(dict.fromkeys(head + tail))[:300]


def _git(args: list[str]) -> str:
    rc, out = _run(["git", *args])
    return out if rc == 0 else f"<git {args[0]} failed: {out}>"


def _git_lines(args: list[str]) -> list[str] | None:
    """Every line git printed, or None when git could not answer at all — the two
    are different facts, and conflating them fabricates a "not tracked" verdict for
    every path. `ls-files` names several files, and a "last line only" reader
    silently reports the earlier ones as untracked."""
    rc, out = _run_raw(["git", *args])
    if rc != 0:
        return None
    return [line.strip() for line in out.splitlines() if line.strip()]


def _tracked(paths: tuple[str, ...]) -> set[str] | None:
    """Which of `paths` the git index knows, in **one** `ls-files` call (a call
    per path spawned eleven gits per sheet and gave an interrupt eleven chances to
    land inside one), or None when git could not answer — see `_git_lines`."""
    lines = _git_lines(["ls-files", "--", *paths])
    return None if lines is None else set(lines)


def _is_tracked(rel: str, tracked: set[str] | None) -> bool | None:
    """Whether `rel` is in the index per `git ls-files`, or None when git could
    not answer.

    A *directory* is tracked when any file under it is: `ls-files` never prints the
    directory itself, so a plain membership test labelled the tracked `data/golden`
    fixture set "clone-invisible"."""
    if tracked is None:
        return None
    return rel in tracked or any(name.startswith(f"{rel}/") for name in tracked)


def counts() -> dict[str, int] | None:
    """The suite's live counts, via the drift gate's own collector — one source of
    truth for "how many tests", so this sheet and `mise drift` cannot disagree.

    None means *not measured* (the gate could not collect), never zero: the sheet
    renders it as unavailable instead of as a number.
    """
    # Function-local: importing this module (a test does) must not put `scripts/`
    # on the interpreter's path for everything else in the process.
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_docs  # deliberate: reuse the gate's collector, one source of truth

    got, _detail = check_docs._collected_counts()
    if got is None:
        return None
    collected, deselected, default = got
    return {"collected": collected, "deselected": deselected, "default": default}


def gates(enabled: bool = True) -> dict[str, str]:
    """`name -> "rc <code> — <summary>"` for the three repo-wide Python gates
    (ruff / drift / compliance). The suite and `mise lint-ts` are deliberately not
    rows: a full `uv run pytest` costs minutes, and `counts` is a collection, not a
    verdict — a reviewer who needs either asks for it. These three are the slow
    part of the sheet (~2.5 s warm), so a caller that already knows their verdicts
    can skip them."""
    if not enabled:
        return {"skipped": "--no-gates (the reviewer should ask for the verdicts, not re-run them)"}
    return {
        name: f"rc {rc} — {line}" if line else f"rc {rc}"
        for name, argv in GATE_COMMANDS
        for rc, line in [_run(argv)]
    }


def tasks() -> dict[str, object]:
    """The `mise.toml` task set and its aliases (the list AGENTS.md claims
    "exactly")."""
    try:
        data = tomllib.loads((ROOT / "mise.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    defined = data.get("tasks", {})
    aliases: list[str] = []
    for body in defined.values():
        if not isinstance(body, dict):  # mise's trivial string-task form
            continue
        alias = body.get("alias")
        if isinstance(alias, str) and alias:
            aliases.append(alias)
        elif isinstance(alias, list):
            aliases.extend(a for a in alias if a)
    return {"names": sorted(defined), "aliases": sorted(set(aliases))}


def tree() -> dict[str, object]:
    """Whether the tree under review is frozen, and what the git index knows."""
    status_rc, status = _run_raw(["git", "status", "--porcelain"])
    dirty = (
        sorted(line[3:] for line in status.splitlines() if len(line) > 3)
        if status_rc == 0
        else [f"<git status failed: {status.strip()}>"]
    )
    tracked = _tracked(GATE_FILES)
    return {
        "head": _git(["rev-parse", "--short", "HEAD"]),
        "frozen": not dirty,
        "dirty": dirty,
        # An unanswered `ls-files` is reported as itself: an empty set would read
        # as "all three untracked", a fact git never stated.
        "untracked_gate_files": (
            ["<git ls-files failed>"]
            if tracked is None
            else [p for p in GATE_FILES if p not in tracked]
        ),
    }


def liveness() -> dict[str, str]:
    """How each runtime classifies a pid nobody owns.

    The divergence this exists to record: on win32 `os.kill(pid, 0)` reports an
    *exited* process as alive while a reference to it still lives, and
    Node/libuv (`GetExitCodeProcess`) says dead — which is why `pgrag.buildlock` is
    the **weak** side by policy (`os.kill`; opening a handle to another process is
    a scanned forbidden API in `scripts/check_constraints.py`) while the pre-hook,
    which asks libuv, may call an owner dead that buildlock keeps waiting out to
    `TTL_S`. The two deliberately diverge, and the released pid measured below is
    the case where they agree. Reviewers kept re-measuring this by hand."""
    try:
        os.kill(RELEASED_PID, 0)
        python = "alive"
    except OSError as exc:
        python = f"{type(exc).__name__}(errno={exc.errno}, winerror={getattr(exc, 'winerror', None)})"
    node = "node not on PATH"
    if shutil.which("node"):
        rc, out = _run(
            [
                "node",
                "-e",
                (
                    f"try{{process.kill({RELEASED_PID},0);console.log('alive')}}"
                    "catch(e){console.log(e.code ?? e.message)}"
                ),
            ]
        )
        node = out if rc == 0 else f"<node failed: {out}>"
    return {"python_released_pid": python, "python_self_pid": _self_liveness(), "node": node}


def _self_liveness() -> str:
    try:
        os.kill(os.getpid(), 0)
        return "alive"
    except OSError as exc:  # pragma: no cover - our own pid is alive
        return f"{type(exc).__name__}: {exc}"


def limits() -> list[str]:
    """Working-tree-only inputs, with their tracking state spelled out."""
    tracked = _tracked(LIMIT_PATHS)
    rows = []
    for rel in LIMIT_PATHS:
        if not (ROOT / rel).exists():
            continue
        known = _is_tracked(rel, tracked)
        if known is None:
            state = "git could not answer"
        elif known:
            state = "tracked"
        else:
            state = "gitignored (clone-invisible)"
        rows.append(f"{rel} — present, {state}")
    return rows


def collect(with_gates: bool = True) -> dict[str, object]:
    """The whole sheet, one key per section."""
    return {
        "counts": counts(),
        "gates": gates(with_gates),
        "tasks": tasks(),
        "tree": tree(),
        "liveness": liveness(),
        "limits": limits(),
    }


def markdown(payload: dict[str, object]) -> str:
    """The sheet as a block a reviewer brief can carry verbatim."""
    out = ["# Facts — do not re-derive (no slice re-runs the rows below)", ""]
    for section, body in payload.items():
        out.append(f"## {section}")
        if body is None:  # "not measured" — not the number zero, not the word None
            out.append("- (unavailable)")
        elif isinstance(body, dict):
            out += [f"- {k}: {v}" for k, v in body.items()] or ["- (none)"]
        elif isinstance(body, list):
            out += [f"- {item}" for item in body] or ["- (none)"]
        else:
            out.append(f"- {body}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Shared facts for a review pass (read-only).")
    parser.add_argument("--json", action="store_true", help="machine-readable payload")
    parser.add_argument(
        "--no-gates",
        action="store_true",
        help="skip the ruff/drift/compliance rows (~7 s of the run)",
    )
    args = parser.parse_args(argv)
    try:
        payload = collect(with_gates=not args.no_gates)
    except KeyboardInterrupt:
        # Read-only, so an interrupt leaves nothing half-done: exit quietly rather
        # than dumping a traceback out of subprocess's pipe reader.
        return 130
    print(json.dumps(payload, indent=2) if args.json else markdown(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
