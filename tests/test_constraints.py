"""Compliance gate: `scripts/check_constraints.py` must stay wired and honest.

Contract (layer L9 in `docs/TEST_CONTRACTS.md`; policy owner
`docs/research/pg-rag-game-interaction-limits.md` §2/§6): the scanner is the
mechanical enforcement of the Project Gorgon TOS/CoC hard lines, so it is a test
gate, not just a push hook. Four invariants:

1. The working tree scans clean — a prohibited pattern in `src`/`scripts`/
   `tests`/`docs`/`.agents`/root manifests fails the offline suite.
2. The candidate set really covers those trees (a broken glob turning the scan
   into a no-op would otherwise print OK forever).
3. A planted violation in any scanned family is reported with its path.
4. `PG_SKIP_CONSTRAINTS` is loud: exit 0 *with* the warning, never silent.

Invariant 1 reads the real `ROOT` (the scanner is tree-shaped); the probe cases
build a synthetic tree and re-point the module's `ROOT` at it, which is the only
way to plant violations without writing into the repo. The probes are assembled
from fragments so this file — itself scanned for the patterns it plants — does
not match its own fixtures.
"""

import re
from pathlib import Path

import scripts.check_constraints as cc

ROOT = Path(__file__).resolve().parent.parent


def _probe(*parts):
    """Join a prohibited token from fragments so no probe matches this file."""
    return "".join(parts)


# One violating body per scanned glob family: code trees (categories 1-2 are
# code-only) plus docs/skills/root manifests (categories 3-4 are not).
PROBES = {
    "src/probe_mod.py": _probe("Read", "ProcessMemory", "(handle, addr, buf, 0, None)\n"),
    "scripts/probe_mod.py": _probe("Win", "Divert", "(filter)\n"),
    "tests/probe_mod.py": _probe("Send", "Input", "(keys, 1)\n"),
    "docs/probe_page.md": f"staged client artifact: {_probe('il2', 'cpp')} {_probe('dump', '.cs')}\n",
    ".agents/skills/probe/SKILL.md": f"reader: {_probe('Read', 'Leb', '128')}(stream)\n",
    "PROBE.md": f"tooling: {_probe('Cpp2', 'IL')}\n",
    "mise.toml": f'x = "{_probe("global", "-metadata")}.dat"\n',
}


def test_scanner_covers_the_documented_trees():
    rels = {p.relative_to(ROOT).as_posix() for p in cc._candidate_files()}
    for rel in (
        "src/pgrag/agentic/loop.py",
        "scripts/check_docs.py",
        "tests/test_lint.py",
        "docs/TEST_CONTRACTS.md",
        ".agents/skills/testing/SKILL.md",
        "AGENTS.md",
        "mise.toml",
    ):
        assert rel in rels, f"{rel} fell out of the compliance scan"
    # The two documented self-exclusions: the checker names the patterns, the
    # policy doc quotes the TOS text.
    assert "scripts/check_constraints.py" not in rels
    assert "docs/research/pg-rag-game-interaction-limits.md" not in rels


def test_scanner_clean_on_repo(monkeypatch, capsys):
    monkeypatch.delenv(cc.SKIP_ENV, raising=False)

    rc = cc.main()

    out = capsys.readouterr().out
    assert rc == 0, out
    match = re.search(r"OK \((\d+) files scanned", out)
    assert match, out
    assert int(match.group(1)) > 0


def test_scanner_reports_planted_violation_in_every_scanned_family(tmp_path, monkeypatch, capsys):
    for rel, body in PROBES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(cc, "ROOT", tmp_path)
    monkeypatch.delenv(cc.SKIP_ENV, raising=False)

    rc = cc.main()

    out = capsys.readouterr().out
    assert rc == 1, out
    for rel in PROBES:
        assert rel in out, f"{rel} not reported:\n{out}"


def test_skip_env_is_loud(tmp_path, monkeypatch, capsys):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "probe_mod.py").write_text(PROBES["src/probe_mod.py"], encoding="utf-8")
    monkeypatch.setattr(cc, "ROOT", tmp_path)
    monkeypatch.setenv(cc.SKIP_ENV, "1")

    assert cc.main() == 0

    out = capsys.readouterr().out
    assert "SKIPPED" in out and cc.SKIP_ENV in out, out
