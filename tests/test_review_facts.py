"""Contract: the review facts sheet (`scripts/review_facts.py`).

One read-only sheet per review pass, so sixteen reviewers stop re-deriving the
same numbers (and stop reporting a fact as a "finding"). The contract is the
*shape* it promises — a section per fact class, each either measured or explicitly
unavailable — plus the facts that must never be quietly wrong: whether the tree is
frozen, whether the runner files the repo's own gates shell out to are actually
committed, and whether git answered at all. The sheet caught three of these on
itself: a "last output line" reader reported one file of three, a plain membership
test called the tracked `data/golden` fixture set "clone-invisible" (`git ls-files`
never prints a directory), and its own docstring described the two liveness probes
the wrong way round.

Nothing here runs the sheet's slow rows or the suite: `mise drift` / `mise test`
already run the gates for real, and re-running them from inside the offline suite
costs seconds per test and races the outer pytest's `.pytest_cache`.
"""

from __future__ import annotations

import json
import subprocess

import pytest

import scripts.review_facts as rf


@pytest.fixture(autouse=True)
def _offline_sheet(monkeypatch):
    """`counts()` shells out to `pytest --collect-only` and `collect()` runs the
    three repo gates (one of which spawns that same collection): both are stubbed,
    so no second pytest runs inside this one. The rows' own code is covered by the
    unit tests below, and the real numbers by `mise drift`."""
    monkeypatch.setattr(rf, "counts", lambda: {"collected": 568, "deselected": 78, "default": 490})
    monkeypatch.setattr(rf, "GATE_COMMANDS", (("head", ("git", "rev-parse", "--short", "HEAD")),))


def _porcelain() -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=rf.ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def test_every_section_renders():
    """Each fact class is present in both forms — the payload a brief can carry as
    `--json` and the markdown block it pastes verbatim."""
    payload = rf.collect()
    assert set(payload) == {"counts", "gates", "tasks", "tree", "liveness", "limits"}
    sheet = rf.markdown(payload)
    for section in payload:
        assert f"## {section}" in sheet
    assert "do not re-derive" in sheet


def test_an_unmeasured_section_is_unavailable_not_a_number(capsys, monkeypatch):
    """A `None` section renders as explicitly unavailable — "not measured" must not
    read as zero, nor as the bare word `None`."""
    sheet = rf.markdown({"counts": None, "tasks": {}, "limits": []})
    assert "- (unavailable)" in sheet
    assert "- (none)" in sheet
    assert "None" not in sheet

    monkeypatch.setattr(rf, "counts", lambda: None)
    assert rf.main(["--json", "--no-gates"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"] is None


def test_the_gate_summary_keeps_the_diagnosis():
    """A red row must carry the *diagnosis*, not only the trailer: each gate prints
    its verdict after the finding it describes, so a last-line reader would report
    compliance's "No carve-outs. Remove the violation…" and name no violation."""
    rc, line = rf._run(
        [
            "python",
            "-c",
            "print('a.py:1:1: F401 x');print('Found 1 error.');print('[*] 1 fixable')",
        ]
    )
    assert rc == 0
    assert "F401" in line and "1 fixable" in line


def test_the_sheet_is_read_only_and_self_consistent(capsys):
    """`--json` parses, the tree is exactly as the sheet found it (a pass is
    report-only), and `counts` has the shape the sheet promises — `None` or the
    triple. The identity `collected == default + deselected` is pytest's own
    arithmetic, so asserting it would test the regex, not the sheet."""
    before = _porcelain()
    assert rf.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert _porcelain() == before, "the sheet changed the working tree"
    assert payload["tree"]["head"], "no HEAD — git could not be read"
    assert isinstance(payload["tasks"]["names"], list)
    assert payload["gates"]["head"].startswith("rc 0")
    counts = payload["counts"]
    assert counts is None or set(counts) == {"collected", "deselected", "default"}


def test_the_runners_the_gates_need_are_tracked():
    """The sheet's tree section is what tells a reviewer whether a clone could run
    the gates at all; it must read *every* `git ls-files` line, not the last, and it
    must ask git **once** for the whole set."""
    assert set(rf._git_lines(["ls-files", "--", *rf.GATE_FILES]) or []) == set(rf.GATE_FILES)
    assert rf._tracked(rf.GATE_FILES) == set(rf.GATE_FILES)
    assert rf._tracked(("scripts/nope_missing.py",)) == set()  # an answer, not a failure
    data = rf.tree()
    assert data["untracked_gate_files"] == []
    assert data["frozen"] is (not data["dirty"])


def test_a_git_that_cannot_answer_is_not_an_untracked_verdict(monkeypatch):
    """`_git_lines`/`_tracked` answer None (not `[]`) when git fails, and both
    consumers say so: `tree()` must not list the runners as untracked, and
    `limits()` must not call a present input "clone-invisible" — a certificate
    git never issued."""
    assert rf._git_lines(["not-a-subcommand"]) is None
    monkeypatch.setattr(rf, "_git_lines", lambda args: None)
    assert rf.tree()["untracked_gate_files"] == ["<git ls-files failed>"]
    # `limits()` lists only the inputs a working tree happens to have, so the
    # assertion is on the rows that exist (a bare clone has none).
    assert all("git could not answer" in row for row in rf.limits())


def test_a_tracked_directory_is_not_clone_invisible():
    """A directory is tracked when any file under it is: `ls-files` never prints the
    directory itself, which is how the tracked `data/golden` fixture set was
    labelled clone-invisible for a whole review pass."""
    tracked = {"data/golden/case.json", "data/documents.json"}
    assert rf._is_tracked("data/golden", tracked) is True
    assert rf._is_tracked("data/documents.json", tracked) is True
    assert rf._is_tracked("data/bm25_index.pkl", tracked) is False
    assert rf._is_tracked("data/golden", None) is None

    rows = {row.split(" —")[0]: row for row in rf.limits()}  # the real working tree
    if "data/golden" in rows:
        assert "tracked" in rows["data/golden"]


def test_a_string_task_body_does_not_crash_the_sheet(tmp_path, monkeypatch):
    """mise's trivial form (`[tasks]` holding a bare string) has no `alias` key;
    reading one raised `AttributeError` out of `tasks()`."""
    (tmp_path / "mise.toml").write_text(
        '[tasks]\nbuild = "cargo build"\n\n[tasks.demo]\nrun = "true"\nalias = "d"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(rf, "ROOT", tmp_path)
    assert rf.tasks() == {"names": ["build", "demo"], "aliases": ["d"]}


def test_no_gates_skips_the_slow_rows(capsys):
    """`--no-gates` is for the caller that already knows the verdicts: the sheet
    still carries the facts, and says the rows were skipped rather than looking
    green."""
    assert rf.main(["--json", "--no-gates"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "skipped" in payload["gates"]
    assert payload["tree"]["head"], "the rest of the sheet must still be measured"
