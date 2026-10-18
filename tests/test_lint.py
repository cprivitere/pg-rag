"""Lint gate: `ruff check src scripts tests` must be clean.

Contract: any finding in the linted trees fails the offline test suite, so a
regression introduced by an edit is caught by `mise test` / `uv run pytest`.
Config lives in `ruff.toml` (repo root) — reads it for the skip tier:
excluded modules stay unguarded. Formatting is NOT gated here (the formatter
is a tool, not a test assertion); only `check` findings fail.

The layer also holds the TypeScript gate for the repo's script-class JS
(`scripts/*.mjs`, covered by its own `scripts/check_ts.mjs`): ruff reads
Python only, so nothing else would notice a `.mjs` edit that breaks parsing.
Both TS tests skip loudly when node/node_modules are
absent — they are developer tooling, not runtime deps.

Module-level because the lint scan is process-wide: one subprocess per pytest
session, not per test. `--no-sync` keeps the nested `uv run` from deciding the
outer pytest session's venv is stale and trying to recreate it mid-run
(Windows: the running pytest.exe holds .venv/Scripts → os error 32/5).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

RUFF_CMD = ["uv", "run", "--no-sync", "ruff", "check", "src", "scripts", "tests"]

# The TypeScript gate (`scripts/check_ts.mjs` + the dev-only parser
# from `npm install`). Node and node_modules are developer-tooling, not runtime
# deps of this repo, so a checkout without them skips loudly rather than failing
# — the gate still runs on any box that has run `npm install`.
NODE = shutil.which("node")
TS_PARSER = ROOT / "node_modules" / "typescript"
TS_NODE_TYPES = ROOT / "node_modules" / "@types" / "node"


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


def _require_ts_parser():
    if NODE is None:
        pytest.skip("node not on PATH — the TypeScript gate needs it (`npm install`)")
    if not (TS_PARSER.exists() and TS_NODE_TYPES.exists()):
        pytest.skip(
            "node_modules is missing the dev-only TypeScript parser — run `npm install` "
            "(the gate is `node scripts/check_ts.mjs`)"
        )


def _run_ts_gate(*paths):
    return subprocess.run(
        [NODE or "node", "scripts/check_ts.mjs", *paths],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_shipped_typescript_parses():
    """The repo's script-class JS (`scripts/*.mjs`, covered by its own
    `scripts/check_ts.mjs`): ruff reads Python only, so nothing else would
    notice a `.mjs` edit that breaks parsing.

    Contract: `mise lint-ts` (`node scripts/check_ts.mjs`) parses clean."""
    _require_ts_parser()
    proc = _run_ts_gate()
    output = (proc.stdout + proc.stderr).strip()
    assert proc.returncode == 0, output
    assert "check_ts.mjs" in output, f"the gate did not cover its own script: {output}"
    assert "no diagnostics" in output, output


def test_the_typescript_gate_reports_a_planted_break(tmp_path):
    """A gate that cannot fail is not a gate: a planted syntax error must come
    back as exit 1 naming the file. Pins that the gate is a real check and
    not an empty file list that exits 0."""
    _require_ts_parser()
    planted = tmp_path / "planted.ts"
    planted.write_text("const x: string = ;\n", encoding="utf-8")

    proc = _run_ts_gate(str(planted))
    output = (proc.stdout + proc.stderr).strip()
    assert proc.returncode == 1, output
    assert "planted.ts" in output and "TS1109" in output, output
