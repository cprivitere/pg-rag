"""Contract: the docs-drift gate (`scripts/check_docs.py`).

Two halves. The *scan*: every backticked `scripts|src|tests|docs|.mise/tasks`-prefixed path in
AGENTS.md and the skills must exist — at the repo root, or (for a skill) next to the SKILL.md that
names it, since a skill bundles its own scripts — over `.py`, `.sh`, `.ps1`, `.mjs`, `.md`, `.json`
and `.toml`. The executable-script classes and the hyphen in the character class are both
load-bearing: the 2026-10-16 task/script audit deleted `.mise/tasks/*.ps1` and hyphenated
`scripts/*.ps1` / `*.sh` files, and a `.py`-only, hyphen-free scan passes exactly the pointers this
gate exists for. The *report*: a provisioned tree is clean, and every unprovisioned input — a
missing source, a dead lint suppression, an untracked gate runner, a
task list that disagrees with `mise.toml`, a stale suite count — is a printed drift, never a
traceback and never a silent skip.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.check_docs as cd

# The module's real collector, captured before any fixture stubs it: the tests
# below that exercise the subprocess path restore this reference.
_REAL_COLLECTED_COUNTS = cd._collected_counts


def test_captures_every_script_class():
    """`.py`, `.sh`, `.ps1`, `.mjs`, `.md`, `.json` and `.toml` pointers under the
    scanned prefixes are named — hyphenated names included, which is the class
    the audit deleted."""
    refs = cd._doc_refs(
        "`scripts/check_ts.mjs` `docs/TEST_CONTRACTS.md` `docs/data/x.json` `data/golden/*.json`"
    )
    assert refs == {"scripts/check_ts.mjs", "docs/TEST_CONTRACTS.md", "docs/data/x.json"}
    refs = cd._doc_refs(
        "see `scripts/build_sqlstore.py`, `scripts/fixture_sample.ps1`, "
        "`scripts/fixture-sample.sh`, `docs/note.md`, `src/pgrag/agentic/store.py` "
        "and `.mise/tasks/deleted_task.ps1`"
    )
    assert refs == {
        "scripts/build_sqlstore.py",
        "scripts/fixture_sample.ps1",
        "scripts/fixture-sample.sh",
        "docs/note.md",
        "src/pgrag/agentic/store.py",
        ".mise/tasks/deleted_task.ps1",
    }
    # bare filenames, non-scanned prefixes and out-of-class extensions stay out
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
    to the SKILL.md, not only at the repo root."""
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


def _clean_tree(tmp_path: Path, **overrides: str | None) -> Path:
    """A tree provisioned with exactly what the gate needs to report zero drift.

    The helper doubles as the gate's input contract: a section that starts
    *requiring* another file makes `test_a_provisioned_tree_reports_no_drift`
    fail, which is the intended cost of adding a check."""
    from pgrag.agentic import eval as agentic_eval

    goldens = ["case", *agentic_eval.SHORT_TIER_IDS]
    skill = "---\nname: {n}\ndescription: x\n---\n"
    # The gate counts the suite by running pytest; these trees have none, so the
    # docs quote the zero the stubbed `_collected_counts` returns.
    counts = "0 collected / 0 deselected / 0 default\n"
    files: dict[str, str] = {
        "AGENTS.md": (
            "DOCUMENTS_VERSION\n" + counts + "The `mise.toml` `[tasks]` set is exactly: `demo`.\n"
        ),
        "docs/TEST_CONTRACTS.md": "DOCUMENTS_VERSION\n{id, question, type, facts\n",
        "docs/REVIEW.md": counts,
        ".omp/RULES.md": "DOCUMENTS_VERSION\n",
        ".omp/WATCHDOG.md": "{id, question, type, facts\n",
        "mise.toml": (
            '[env]\nLLM_MODEL = "org/Model:Q4_K_M"\nLLM_FLAGS = "-ngl 999"\n'
            '\n[tasks.demo]\nrun = "true"\n'
        ),
        "ruff.toml": '[lint]\nselect = ["F"]\nignore = ["F841"]\n',
        "src/pgrag/cli.py": "def main():\n    pass\n",
        "scripts/services.py": "# reads LLM_MODEL / LLM_FLAGS from the environment\n",
        "scripts/agentic_tune.py": "# reads LLM_MODEL / LLM_FLAGS from the environment\n",
        "scripts/golden_check.py": (
            "--tier --type --max-facts --diagnose --export --ids --corpus "
            "--max-rounds --store --report\n"
        ),
        ".agents/skills/evaluation/SKILL.md": (
            skill.format(n="evaluation")
            + f"{len(goldens)} files exist today entity {len(goldens)} 0 store-only\n"
        ),
        ".agents/skills/pg-rag/SKILL.md": skill.format(n="pg-rag") + "DOCUMENTS_VERSION\n",
    }
    for name in ("pg-data", "retrieval", "testing"):
        files[f".agents/skills/{name}/SKILL.md"] = skill.format(n=name)
    files[".agents/skills/testing/SKILL.md"] = skill.format(n="testing") + counts
    for gid in goldens:
        files[f"data/golden/{gid}.json"] = json.dumps({"id": gid, "type": "entity", "store": False})

    for rel, text in overrides.items():
        if text is None:
            files.pop(rel, None)
        else:
            files[rel] = text
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _stub_collection(monkeypatch):
    """`main()` counts the suite by running `pytest --collect-only`; the tmp trees
    hold no tests, so the count is stubbed to the zero their docs quote. The
    count check itself is covered by `test_the_count_quote_is_checked`, and the
    parser by `test_collection_summaries_are_parsed`."""
    monkeypatch.setattr(
        cd, "_collected_counts", lambda: ((0, 0, 0), "0 collected / 0 deselected / 0 default")
    )
    # the tmp trees live outside the repo: `git ls-files` there answers nothing
    monkeypatch.setattr(cd, "_git_tracked", lambda paths: set(paths))


def test_collection_summaries_are_parsed():
    """§11's parser accepts every summary pytest prints, and a *failed* collection
    is its own verdict.

    The first version understood only the `-m`-filtered triple, so a legitimate
    0-deselected (or all-deselected) config reported "cannot count the test
    suite" — a false drift — while a broken collection's partial summary was
    accepted and then blamed on the docs."""
    assert cd._parse_collection("490/568 tests collected (78 deselected) in 1.0s") == (568, 78, 490)
    assert cd._parse_collection("12 tests collected in 0.4s") == (12, 0, 12)
    assert cd._parse_collection("no tests collected (3 deselected) in 0.1s") == (0, 3, 0)
    assert cd._parse_collection("no tests ran in 0.02s") is None


def test_a_failed_collection_is_not_a_stale_count(monkeypatch, capsys):
    """A collection that fails (`rc != 0`) reports its own reason, never a count:
    the partial summary a broken run prints names a number that does not exist,
    and blaming the docs for it is the misleading half of the same bug."""
    from types import SimpleNamespace

    monkeypatch.setattr(cd, "_collected_counts", _REAL_COLLECTED_COUNTS)  # past the stub
    monkeypatch.setattr(
        cd.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=2, stdout="2/3 tests collected (1 deselected), 1 error in 0.17s\n"
        ),
    )
    counts, detail = cd._collected_counts()
    assert counts is None
    assert detail == "pytest --collect-only rc 2"

    monkeypatch.setattr(
        cd.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="7 tests collected in 0.2s\n"),
    )
    assert cd._collected_counts() == ((7, 0, 7), "7 collected / 0 deselected / 7 default")


def test_the_count_quote_is_checked(tmp_path, monkeypatch, capsys):
    """The stale-count class, pinned: a doc whose triple disagrees with the live
    collection is drift, and one that does not quote it at all is drift too —
    "537/459" outlived the tree by twelve tests because nothing computed it."""
    monkeypatch.setattr(
        cd,
        "_collected_counts",
        lambda: ((557, 78, 479), "557 collected / 78 deselected / 479 default"),
    )
    monkeypatch.setattr(cd, "ROOT", _clean_tree(tmp_path))
    assert cd.main() == 1
    out = capsys.readouterr().out
    assert (
        "quotes stale suite counts" in out and "557 collected / 78 deselected / 479 default" in out
    )

    clean = _clean_tree(tmp_path / "quoted", **{"AGENTS.md": "DOCUMENTS_VERSION\n"})
    monkeypatch.setattr(cd, "ROOT", clean)
    assert cd.main() == 1
    assert "does not quote the suite counts" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("overrides", "label"),
    [
        (
            {"ruff.toml": '[lint]\nselect = ["F"]\nignore = ["S110"]\n'},
            "lint suppressions name unselected rules",
        ),
        (
            # `B` selects flake8-bugbear, not flake8-blind-except: the prefix the
            # first predicate matched is not what ruff matches.
            {
                "ruff.toml": (
                    '[lint]\nselect = ["B"]\n\n[lint.per-file-ignores]\n"scripts/**" = ["BLE001"]\n'
                )
            },
            "lint suppressions name unselected rules",
        ),
        (
            {"scripts/services.py": 'LLM_MODEL = "org/Model:Q4_K_M"\n'},
            "scripts/services.py hardcodes model literal",
        ),
        (
            {"AGENTS.md": "DOCUMENTS_VERSION\n0 collected / 0 deselected / 0 default\n"},
            "does not list the task set",
        ),
        (
            {
                "AGENTS.md": "DOCUMENTS_VERSION\n0 collected / 0 deselected / 0 default\n"
                "The `mise.toml` `[tasks]` set is exactly: `other`.\n"
            },
            "task list drifted",
        ),
    ],
)
def test_a_wiring_drift_is_reported(tmp_path, monkeypatch, capsys, overrides, label):
    """The wiring checks have teeth: a suppression for an unselected rule and
    a task list that disagrees with `mise.toml` are each drift — the classes
    that shipped silently in the PR."""
    monkeypatch.setattr(cd, "ROOT", _clean_tree(tmp_path, **overrides))
    assert cd.main() == 1
    assert label in capsys.readouterr().out


def test_a_prose_mention_of_the_tests_bullet_is_not_a_second_layer(tmp_path, monkeypatch, capsys):
    """The map *describes* the `- **Tests**:` convention in prose. Only the bullet
    form opens a layer block: otherwise the description is itself a block, and
    every file it happens to name looks doubly owned (which is how this check
    first fired on the real doc)."""
    monkeypatch.setattr(
        cd,
        "ROOT",
        _clean_tree(
            tmp_path,
            **{
                "docs/TEST_CONTRACTS.md": (
                    "DOCUMENTS_VERSION\n{id, question, type, facts\n"
                    "0 collected / 0 deselected / 0 default\n"
                    "- **Tests**: `test_alpha.py`\n"
                    "- **Contracts**: one layer's `**Tests**` block names `test_alpha.py`.\n"
                ),
                "tests/test_alpha.py": "def test_x():\n    pass\n",
            },
        ),
    )
    assert cd.main() == 0
    assert "every test file is owned by exactly one layer" in capsys.readouterr().out


def test_a_suppression_in_a_family_the_selector_only_string_prefixes_is_dead():
    """§8's predicate is ruff's, not a string prefix: a selector resolves *inside
    its linter*, so `B` (flake8-bugbear) never covers `BLE001`
    (flake8-blind-except) and `F` (pyflakes) never covers `FA`/`FURB`.

    The first version matched raw prefixes, which judged every one of those
    families selected — i.e. it accepted exactly the dead suppressions the check
    exists to catch (verified against ruff itself: `--select B` lints a bare
    `except Exception:` clean, `--select BLE` reports `BLE001`)."""
    select = ["E4", "E7", "E9", "F", "I", "W", "B", "UP", "SIM", "ISC", "FLY", "RUF"]
    assert cd._code_selected("B008", select)  # bugbear, selected by `B`
    assert cd._code_selected("E402", select)  # matched by the `E4` prefix
    assert cd._code_selected("RUF059", select)
    assert not cd._code_selected("BLE001", select)  # blind-except under `B`
    assert not cd._code_selected("FA100", select)  # future-annotations under `F`
    assert not cd._code_selected("FURB189", select)  # refurb under `F`
    assert not cd._code_selected("S110", select)


def test_the_ownership_parser_follows_the_convention_not_the_markup(tmp_path, monkeypatch, capsys):
    """All three bullet markers open a layer block, a fenced *example* of the
    convention is not one, and a file named twice inside one block is still one
    owner.

    Each of these was a wrong verdict: a `*`/`+` reformat made every file
    "unowned", a fenced example double-counted a file into two layers, and a
    repeated name inside one layer reported "more than one layer"."""
    for bullet in ("-", "*", "+"):
        monkeypatch.setattr(
            cd,
            "ROOT",
            _clean_tree(
                tmp_path / bullet.replace("*", "star").replace("+", "plus"),
                **{
                    "docs/TEST_CONTRACTS.md": (
                        "DOCUMENTS_VERSION\n{id, question, type, facts\n"
                        "0 collected / 0 deselected / 0 default\n"
                        f"{bullet} **Tests**: `test_alpha.py`\n"
                    ),
                    "tests/test_alpha.py": "def test_x():\n    pass\n",
                },
            ),
        )
        assert cd.main() == 0, bullet
        assert "every test file is owned by exactly one layer" in capsys.readouterr().out

    monkeypatch.setattr(
        cd,
        "ROOT",
        _clean_tree(
            tmp_path / "fenced",
            **{
                "docs/TEST_CONTRACTS.md": (
                    "DOCUMENTS_VERSION\n{id, question, type, facts\n"
                    "0 collected / 0 deselected / 0 default\n"
                    "```\n- **Tests**: `test_alpha.py`\n```\n"
                    "- **Tests**: `test_alpha.py`\n"
                ),
                "tests/test_alpha.py": "def test_x():\n    pass\n",
            },
        ),
    )
    assert cd.main() == 0
    assert "every test file is owned by exactly one layer" in capsys.readouterr().out


def test_a_provisioned_tree_reports_no_drift(tmp_path, monkeypatch, capsys):
    """The gate's own happy path: everything it needs is present, so it must
    report zero drift *and* exit 0 — the baseline the missing-file cases below
    are measured against."""
    monkeypatch.setattr(cd, "ROOT", _clean_tree(tmp_path))
    assert cd.main() == 0
    assert "0 drift" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("missing", "label"),
    [
        ("AGENTS.md", "AGENTS.md: source missing"),
        # A skill's SKILL.md is not in `_doc_sources` (that list is glob-derived),
        # so a *deleted* one is caught by the canonical list instead — the same
        # loudness, from the other owner.
        (".agents/skills/pg-rag/SKILL.md", "missing skill: pg-rag"),
        (".omp/RULES.md", "RULES.md missing"),
        ("scripts/golden_check.py", "golden_check.py missing"),
        ("mise.toml", "cannot parse mise.toml"),
        ("scripts/services.py", "scripts/services.py missing"),
        (".agents/skills/evaluation/SKILL.md", "evaluation skill missing"),
    ],
)
def test_a_missing_input_is_loud_and_never_a_traceback(
    tmp_path, monkeypatch, capsys, missing, label
):
    """Every input the gate reads is either reported or absent-loud.

    Two failure modes are pinned here: a *silent* skip (the doc sources and the
    contract docs `continue`d, so deleting one reported a clean run while
    checking nothing) and an unguarded `read_text` (which raised before the
    report loop, discarding the drift lines it had already collected). Either
    way the operator sees a verdict, never a traceback."""
    monkeypatch.setattr(cd, "ROOT", _clean_tree(tmp_path, **{missing: None}))
    assert cd.main() == 1
    assert label in capsys.readouterr().out


def test_an_untracked_runner_and_an_unanswerable_git_are_their_own_drifts(
    tmp_path, monkeypatch, capsys
):
    """§9's two arms, neither pinned elsewhere: a runner file the index does not
    know (a fresh clone then has no TypeScript gate at all, and its tests merely
    *skip*), and a git that cannot answer — which must not be reported as the
    first, because "untracked" is a fact git never stated."""
    monkeypatch.setattr(cd, "ROOT", _clean_tree(tmp_path))
    monkeypatch.setattr(cd, "_git_tracked", lambda paths: {p for p in paths if p != "package.json"})
    assert cd.main() == 1
    assert "gate runner files are untracked" in capsys.readouterr().out

    monkeypatch.setattr(cd, "_git_tracked", lambda paths: None)
    assert cd.main() == 1
    out = capsys.readouterr().out
    assert "cannot read the git index" in out
    assert "gate runner files are untracked" not in out


def test_the_real_docs_point_only_at_live_files():
    """The actual AGENTS.md + skill blobs are clean under the widened scan — the
    invariant `mise drift` reports, pinned here so a doc edit that names a
    deleted script fails in the offline suite too."""
    assert cd._all_missing_doc_refs() == []
