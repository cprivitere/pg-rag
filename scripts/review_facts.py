"""Shared facts for a review pass — computed once, handed to every reviewer.

A review fans out over the same questions from every slice: how many tests does
the suite collect, do the gates pass, which tasks exist, is the tree frozen,
what does the platform say about a dead pid. Re-deriving them in each brief cost
roughly a third of the last pass's wall time and produced three "findings" that
were really just facts a machine already knew (a stale count, an untracked gate
runner, a drift status).

Run it once on the frozen SHA (`uv run python scripts/review_facts.py`, `--json`
for machines) and paste the output into the *shared context* of every reviewer
brief, with the instruction not to re-run the suite, ruff, drift or compliance.

Read-only by construction: it inspects, it never writes, and it starts no
service. `--counts` (the one slow section: a real `pytest --collect-only`) is on
by default; the whole sheet is a handful of seconds.
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
sys.path.insert(0, str(ROOT / "scripts"))

# The runner files the repo's gates shell out to: untracked, a fresh clone has no
# TypeScript gate at all (and its tests only *skip*, so nothing reports that).
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
        proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout + proc.stderr


def _run(argv: tuple[str, ...] | list[str]) -> tuple[int, str]:
    """`(returncode, last non-empty output line)`. A non-zero rc is a *fact*
    here, not an error — `drift` failing is exactly what a reviewer needs."""
    rc, out = _run_raw(argv)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return rc, lines[-1] if lines else ""


def _git(args: list[str]) -> str:
    rc, out = _run(["git", *args])
    return out if rc == 0 else f"<git {args[0]} failed: {out}>"


def _git_lines(args: list[str]) -> list[str]:
    """Every line git printed — `ls-files` names several files, and a "last line
    only" reader silently reports the earlier ones as untracked."""
    rc, out = _run_raw(["git", *args])
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def counts() -> dict[str, int] | None:
    """The suite's live counts, via the drift gate's own collector — one source of
    truth for "how many tests", so this sheet and `mise drift` cannot disagree."""
    import check_docs  # deliberate: reuse the gate's collector, one source of truth

    got = check_docs._collected_counts()
    if got is None:
        return None
    collected, deselected, default = got
    return {"collected": collected, "deselected": deselected, "default": default}


def gates() -> dict[str, str]:
    """`name -> "rc <code> — <last line>"` for each repo-wide gate."""
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
    tracked = set(_git_lines(["ls-files", "--", *GATE_FILES]))
    return {
        "head": _git(["rev-parse", "--short", "HEAD"]),
        "frozen": not dirty,
        "dirty": dirty,
        "untracked_gate_files": [p for p in GATE_FILES if p not in tracked],
    }


def liveness() -> dict[str, str]:
    """How each runtime classifies a pid nobody owns.

    The divergence this exists to record: on win32 `os.kill(pid, 0)` reports an
    *exited* process as alive whenever a handle to it is still open, while
    Node/libuv (`GetExitCodeProcess`) says dead — which is why
    `pgrag.buildlock` asks the OS the exit-code way and the pre-hook agrees with
    it. Reviewers kept re-measuring this by hand."""
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
    rows = []
    for rel in LIMIT_PATHS:
        if not (ROOT / rel).exists():
            continue
        tracked = bool(_git_lines(["ls-files", "--", rel]))
        rows.append(f"{rel} — present, {'tracked' if tracked else 'gitignored (clone-invisible)'}")
    return rows


def collect() -> dict[str, object]:
    """The whole sheet, one key per section."""
    return {
        "counts": counts(),
        "gates": gates(),
        "tasks": tasks(),
        "tree": tree(),
        "liveness": liveness(),
        "limits": limits(),
    }


def markdown(payload: dict[str, object]) -> str:
    """The sheet as a block a reviewer brief can carry verbatim."""
    out = ["# Facts — do not re-derive (do not re-run the suite/lint/drift)", ""]
    for section, body in payload.items():
        out.append(f"## {section}")
        if isinstance(body, dict):
            out += [f"- {k}: {v}" for k, v in body.items()]
        elif isinstance(body, list):
            out += [f"- {item}" for item in body] or ["- (none)"]
        else:
            out.append(f"- {body}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Shared facts for a review pass (read-only).")
    parser.add_argument("--json", action="store_true", help="machine-readable payload")
    args = parser.parse_args(argv)
    payload = collect()
    print(json.dumps(payload, indent=2) if args.json else markdown(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
