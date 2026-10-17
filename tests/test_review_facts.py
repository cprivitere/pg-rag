"""Contract: the review facts sheet (`scripts/review_facts.py`).

One read-only sheet per review pass, so sixteen reviewers stop re-deriving the
same numbers (and stop reporting a fact as a "finding"). The contract is the
*shape* it promises — a section per fact class, each either measured or
explicitly unavailable — plus the facts that must never be quietly wrong: whether
the tree is frozen, and whether the runner files the repo's own gates shell out
to are actually committed. The sheet caught that last one on itself: a
"last output line" reader reported one file of three and called the other two
untracked.
"""

from __future__ import annotations

import json

import scripts.review_facts as rf


def test_every_section_renders():
    """Each fact class is present in both forms — the payload a brief can carry
    as `--json` and the markdown block it pastes verbatim."""
    payload = rf.collect()
    assert set(payload) == {"counts", "gates", "tasks", "tree", "liveness", "limits"}
    sheet = rf.markdown(payload)
    for section in payload:
        assert f"## {section}" in sheet
    assert "do not re-derive" in sheet


def test_the_sheet_is_read_only_and_self_consistent(capsys, monkeypatch):
    """`--json` parses, and the counts agree with themselves: the collected total
    is the default run plus what `not slow` deselected. Nothing here needs a
    server, and nothing writes to the repo.

    The gate rows are stubbed to one cheap command: `mise drift` and `mise test`
    already run those three for real, and re-running them here would add ~8 s to
    every offline suite run for a shape assertion."""
    monkeypatch.setattr(rf, "GATE_COMMANDS", (("head", ("git", "rev-parse", "--short", "HEAD")),))
    assert rf.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tree"]["head"], "no HEAD — git could not be read"
    assert isinstance(payload["tasks"]["names"], list)
    assert payload["gates"]["head"].startswith("rc 0")
    counts = payload["counts"]
    if counts is not None:
        assert counts["collected"] == counts["default"] + counts["deselected"]


def test_the_runners_the_gates_need_are_tracked():
    """The sheet's tree section is what tells a reviewer whether a clone could run
    the gates at all; it must read *every* `git ls-files` line, not the last, and
    it must ask git **once** for the whole set (a call per path spawned eleven
    gits per sheet)."""
    assert rf._git_lines(["ls-files", "--", *rf.GATE_FILES]) == sorted(rf.GATE_FILES)
    assert rf._tracked(rf.GATE_FILES) == set(rf.GATE_FILES)
    assert rf._tracked(("scripts/nope_missing.py",)) == set()
    data = rf.tree()
    assert data["untracked_gate_files"] == []
    assert data["frozen"] is (not data["dirty"])


def test_no_gates_skips_the_slow_rows(capsys):
    """`--no-gates` is for the caller that already knows the verdicts: the sheet
    still carries the facts, and says the rows were skipped rather than looking
    green."""
    assert rf.main(["--json", "--no-gates"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "skipped" in payload["gates"]
    assert payload["tree"]["head"], "the rest of the sheet must still be measured"
