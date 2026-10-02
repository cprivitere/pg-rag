"""Lint gate: `ruff check src scripts tests` must be clean.

Contract: any finding in the linted trees fails the offline test suite, so a
regression introduced by an edit is caught by `mise test` / `uv run pytest`.
Config lives in `ruff.toml` (repo root) — reads it for the skip tier:
excluded modules stay unguarded. Formatting is NOT gated here (the formatter
is a tool, not a test assertion); only `check` findings fail.

Module-level because the lint scan is process-wide: one subprocess per pytest
session, not per test. `--no-sync` keeps the nested `uv run` from deciding the
outer pytest session's venv is stale and trying to recreate it mid-run
(Windows: the running pytest.exe holds .venv/Scripts → os error 32/5).
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RUFF_CMD = ["uv", "run", "--no-sync", "ruff", "check", "src", "scripts", "tests"]


def _collect_ruff_violations():
    proc = subprocess.run(
        RUFF_CMD,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode == 0:
        return 0, ""
    return 1, (proc.stdout + proc.stderr).strip()


def _findings(output):
    return [line for line in output.splitlines() if line and ":" in line]


def test_ruff_clean():
    rc, output = _collect_ruff_violations()
    findings = _findings(output)
    # Distinguish a config/drive failure (missing ruff, broken ruff.toml,
    # interpreter not locatable in PATH through PATH shell) from real findings.
    if rc != 0 and not findings and "No such file or directory" not in output:
        raise AssertionError("ruff invocation failed: " + output)
    assert rc == 0, "{} ruff findings:\n{}".format(len(findings), "\n".join(findings[:40]))
