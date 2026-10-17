"""Contract: the docs-drift gate's file-pointer scan (`scripts/check_docs.py`).

Every backticked `scripts|src|tests|docs|.mise/tasks`-prefixed **.py / .sh /
.ps1** path in AGENTS.md and the skills must exist — at the repo root, or (for a
skill) next to the SKILL.md that names it, since a skill bundles its own
scripts. The executable-script classes and the hyphen in the character class are
both load-bearing: the 2026-10-16 task/script audit deleted `.mise/tasks/*.ps1`
and hyphenated `scripts/*.ps1` / `*.sh` files, and a `.py`-only, hyphen-free
scan passes exactly the pointers this gate exists for.
"""

from __future__ import annotations

import scripts.check_docs as cd


def test_captures_every_script_class():
    """`.py`, `.sh` and `.ps1` pointers under the scanned prefixes are named —
    hyphenated names included, which is the class the audit deleted."""
    refs = cd._doc_refs(
        "see `scripts/build_tool_corpus.py`, `scripts/fixture_sample.ps1`, "
        "`scripts/fixture-sample.sh`, `docs/note.md`, `src/pgrag/buildlock.py` "
        "and `.mise/tasks/deleted_task.ps1`"
    )
    assert refs == {
        "scripts/build_tool_corpus.py",
        "scripts/fixture_sample.ps1",
        "scripts/fixture-sample.sh",
        "src/pgrag/buildlock.py",
        ".mise/tasks/deleted_task.ps1",
    }
    # bare filenames, non-scanned prefixes and non-script extensions stay out
    assert (
        cd._doc_refs("`discover-servers.ps1` / `notebooks/x.py` / `data/documents.json`") == set()
    )
    # A selector after the path (`::test_name`, `:12-40`, ` --flag`) is not part
    # of it: the pointer still has to resolve, which is how the two real
    # selector-carrying refs in AGENTS.md/skills are covered.
    assert cd._doc_refs(
        "`tests/test_lint.py::test_shipped_source_stays_python_313_parseable`, "
        "`scripts/golden_check.py --diagnose`, `scripts/golden_rerun.py:12-40`"
    ) == {
        "tests/test_lint.py",
        "scripts/golden_check.py",
        "scripts/golden_rerun.py",
    }


def test_a_vanished_pointer_is_reported(tmp_path, monkeypatch):
    """A pointer to a deleted script is drift; a live one is not."""
    monkeypatch.setattr(cd, "ROOT", tmp_path)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "still_here.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    assert cd._missing_doc_refs("run `scripts/still_here.sh` (not `scripts/gone.ps1`)") == [
        "scripts/gone.ps1"
    ]


def test_a_skill_resolves_its_own_bundled_scripts(tmp_path, monkeypatch):
    """A skill addresses its bundled scripts as `scripts/…`: that resolves next
    to the SKILL.md, not only at the repo root (which is how
    `.agents/skills/molab-notebook/SKILL.md` names `execute-code.ps1`)."""
    monkeypatch.setattr(cd, "ROOT", tmp_path)
    skill = tmp_path / ".agents" / "skills" / "demo"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "execute-code.ps1").write_text("x", encoding="utf-8")

    ref = "`scripts/execute-code.ps1`"
    assert cd._missing_doc_refs(ref) == ["scripts/execute-code.ps1"]  # not at the root
    assert cd._missing_doc_refs(ref, skill) == []  # but real next to the skill


def test_expected_skills_unions_canonical_and_tree(tmp_path, monkeypatch):
    """Contract: `_expected_skills()` names `CANONICAL_SKILLS` *and* the tree.

    A tree-only list cannot report a deleted canonical skill (a glob cannot name
    what is gone), so `main()`'s missing-skill branch would be unreachable; a
    canonical-only list cannot report a newly landed skill. The union keeps both
    branches live: here a tmp tree carries only `demo` — every canonical name is
    still listed, so `pg-rag` is reported missing, while `demo` (tree-present)
    is named too.
    """
    monkeypatch.setattr(cd, "ROOT", tmp_path)
    demo = tmp_path / ".agents" / "skills" / "demo"
    demo.mkdir(parents=True)
    (demo / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")

    expected = cd._expected_skills()
    assert set(cd.CANONICAL_SKILLS) <= set(expected)
    assert "demo" in expected
    assert "pg-rag" in expected  # named although no such directory exists


def test_the_real_docs_point_only_at_live_files():
    """The actual AGENTS.md + skill blobs are clean under the widened scan — the
    invariant `mise drift` reports, pinned here so a doc edit that names a
    deleted script fails in the offline suite too."""
    assert cd._all_missing_doc_refs() == []
