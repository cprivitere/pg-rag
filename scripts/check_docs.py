"""Drift guard: keep the docs/skills truthful about the repo.

Prose documentation (AGENTS.md, .omp/RULES.md, skills, docs/TEST_CONTRACTS.md)
drifts unless something checks it against the code. The one guardrail in this
repo that never drifted is enforced code (conftest's immutability guard) — this
script is the documentation analogue. It asserts stable, meaningful facts:

- every test file is covered by docs/TEST_CONTRACTS.md, owned by exactly one
  layer of it (and nothing it references has vanished) — the layer->test map
  cannot quietly diverge;
- file paths named in backticks under AGENTS.md / the skills actually exist
  (`.py`, `.sh`, `.ps1`, `.mjs`, `.md`, `.json` and `.toml` — the executable
  classes were deleted by the task/script audit, so a `.py`-only scan could not
  see a surviving pointer), and a *missing* source is drift rather than a skip;
- contract constants (DOCUMENTS_VERSION) and the golden shape agree across the
  docs that quote them;
- the golden-set counts the evaluation skill quotes are real;
- the corpus writer locks the directory it writes (`cli.py` locks
  `DOCUMENTS_PATH.parent`, never the package-anchored `DATA_DIR` — the same path
  from the repo root, two paths anywhere else);
- every lint suppression in `ruff.toml` names a rule `select` carries (six of
  nine entries were dead, suppressing nothing while looking like a safeguard);
- the gates' own runner files (`scripts/check_ts.mjs`, `package.json`,
  `package-lock.json`) are tracked by git, or a clone has no runner at all;
- AGENTS.md's `[tasks]` list is the real `mise.toml` task set, set for set;
- the suite counts the docs quote match a real `pytest --collect-only`
  ("N collected / M deselected / K default" — the numbers churned three review
  passes in a row while nothing computed them).

Exit 0 = clean; 1 = one or more drifts (printed). Run via `mise drift`
(uv run python scripts/check_docs.py). Only stable facts are asserted — a
check that breaks on innocuous churn gets ignored, which is how guardrails rot.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_lines: list[str] = []  # ("OK" | "DRIFT", label[, detail])


def _report(tag: str, label: str, detail: str = "") -> None:
    _lines.append((tag, label, detail))


def _drift(label: str, detail: str = "") -> None:
    _report("DRIFT", label, detail)


def _read(*parts: str) -> str:
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def _exists(*parts: str) -> bool:
    return ROOT.joinpath(*parts).exists()


# Backticked repo-relative paths named in AGENTS.md / the skills. The
# executable-script classes are in scope: the 2026-10-16 task/script audit
# deleted `scripts/*.ps1` and `scripts/*.sh` files, and a `.py`-only scan
# cannot see a surviving pointer to one of them. The character class carries
# `-` because the deleted names are hyphenated (`execute-code.ps1`,
# `chat-start.ps1`, `ensure-logs-dir.ps1`); a class without it passes exactly
# the pointers this scan exists for. `.mise/tasks/` is a prefix because that
# whole tree was deleted with the same audit.
_DOC_PATH_RE = re.compile(
    r"`((?:scripts|src|tests|docs|\.mise/tasks)/[A-Za-z0-9_./-]+\.(?:py|sh|ps1|mjs|md|json|toml))"
    # A pointer may carry a selector after the path — `::test_name`, `:12-40`,
    # ` --flag`. The path is what must resolve, so the selector is matched and
    # dropped rather than excluding the pointer from the scan (which is how
    # `tests/test_lint.py::test_shipped_source_stays_python_313_parseable` and
    # `scripts/golden_check.py --diagnose` went unvalidated).
    r"(?:(?:::|:[\d-]+|\s+--?)[A-Za-z0-9_=.\[\]-]*)*"
    r"`"
)


def _doc_refs(text: str) -> set[str]:
    """The repo-relative paths named in backticks in `text`."""
    return set(_DOC_PATH_RE.findall(text))


# pytest's own summary ("479/557 tests collected (78 deselected) in 0.98s") and
# the canonical triple every quoting doc must carry.
_COLLECTED_RE = re.compile(r"(\d+)/(\d+) tests? collected \((\d+) deselected\)")
_COUNTS_QUOTE_RE = re.compile(r"(\d+) collected / (\d+) deselected / (\d+) default")

# The directory the documents rebuild must lock: the one it writes to. Asserted
# as a spelling so the writer and the lock cannot drift apart again (they were
# `DATA_DIR` vs `data/` — the same path from the repo root, two paths anywhere
# else).
LOCK_TARGET = "DOCUMENTS_PATH.parent"


def _missing_doc_refs(text: str, base: Path | None = None) -> list[str]:
    """Refs in `text` that resolve neither at the repo root nor — for a skill,
    whose own bundled scripts are addressed as `scripts/…` — next to the source
    file."""
    missing = []
    for ref in sorted(_doc_refs(text)):
        if _exists(ref):
            continue
        if base is not None and (base / ref).exists():
            continue
        missing.append(ref)
    return missing


def _doc_sources() -> list[Path]:
    """AGENTS.md (repo-root-relative) + every skill's SKILL.md (which may point
    at its own bundled scripts)."""
    return [ROOT / "AGENTS.md", *sorted((ROOT / ".agents" / "skills").glob("*/SKILL.md"))]


# Skills the repo promises to ship. The union with the tree-derived names is
# deliberate: a tree-only list silently drops a skill that was deleted outright
# (globbing cannot report what is gone), while a canonical-only list silently
# drops a new skill (nothing names it until someone remembers this tuple).
# `molab-notebook` is shipped (AGENTS.md → Key Directories) and must be here, or
# deleting its directory reports no drift at all.
CANONICAL_SKILLS = ("pg-data", "pg-rag", "retrieval", "evaluation", "testing", "molab-notebook")


def _expected_skills() -> list[str]:
    """Every skill that must exist: `CANONICAL_SKILLS` plus the tree."""
    present = {p.parent.name for p in (ROOT / ".agents" / "skills").glob("*/SKILL.md")}
    return sorted(set(CANONICAL_SKILLS) | present)


def _code_selected(code: str, select: list[str]) -> bool:
    """True when `select` covers `code`: ruff matches by prefix, so `B` covers
    `B008` and `E4` covers `E402`."""
    return any(code == sel or code.startswith(sel) for sel in select)


def _git_tracked(paths: tuple[str, ...]) -> set[str] | None:
    """The subset of `paths` the git index knows, or None when git cannot answer
    (not a repo, no git binary)."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--", *paths],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _collected_counts() -> tuple[int, int, int] | None:
    """`(collected, deselected, default)` from a real collection, or None.

    One `pytest --collect-only -q` run (~1.4 s here) is the only honest source:
    the golden cases are parametrized from the files on disk, so counting `def
    test_` functions statically cannot see them. The docs quote the triple
    `N collected / M deselected / K default` and this is what it must equal —
    the numbers churned three review passes in a row ("537/459" outlived the
    tree by 8 tests) precisely because nothing computed them.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    matches = _COLLECTED_RE.findall(proc.stdout or "")
    if not matches:
        return None
    default, collected, deselected = (int(g) for g in matches[-1])  # the summary line
    return collected, deselected, default


def _all_missing_doc_refs() -> list[str]:
    """Every unresolved doc pointer across the sources.

    A *source* that has vanished is reported too: skipping it silently is how a
    deleted AGENTS.md (or a whole skill) would leave this scan reporting zero
    drift while checking nothing.
    """
    missing: list[str] = []
    for source in _doc_sources():
        if not source.exists():
            missing.append(f"{source.relative_to(ROOT).as_posix()}: source missing")
            continue
        base = source.parent if source.name == "SKILL.md" else None
        missing += [
            f"{source.relative_to(ROOT).as_posix()}: {ref}"
            for ref in _missing_doc_refs(source.read_text(encoding="utf-8"), base)
        ]
    return missing


def main() -> int:
    # The report belongs to this run: a second call (a test, or a caller that
    # retries) must not accumulate the previous run's lines and print them again.
    _lines.clear()

    # --- 1. TEST_CONTRACTS covers every test file (and vice versa) ---------
    tc = ROOT / "docs" / "TEST_CONTRACTS.md"
    if not tc.exists():
        _drift("TEST_CONTRACTS.md missing", "docs/TEST_CONTRACTS.md")
    else:
        tc_text = tc.read_text(encoding="utf-8")
        test_files = sorted(p.name for p in (ROOT / "tests").glob("test_*.py"))
        orphans = [f for f in test_files if f not in tc_text]
        if orphans:
            _drift("TEST_CONTRACTS missing test files", ", ".join(orphans))
        else:
            _report("OK", "TEST_CONTRACTS covers all tests")
        # "Covered" must mean *owned*: a file listed under two layers leaves two
        # claims on one contract and no way to tell which is current (and a file
        # no `**Tests**` block names is owned by nothing at all). Names are read
        # from the layer blocks only — the map also discusses files in prose.
        owners: dict[str, int] = {}
        in_tests = False
        for line in tc_text.splitlines():
            if line.lstrip().startswith("- **Tests**"):
                in_tests = True
            elif in_tests and (not line.strip() or line.lstrip().startswith(("#", "-", "*", "|"))):
                in_tests = False
                continue
            if in_tests:
                for name in re.findall(r"`(test_[a-zA-Z0-9_]+\.py)`", line):
                    owners[name] = owners.get(name, 0) + 1
        multi_layer = sorted(n for n, count in owners.items() if count > 1)
        unowned = sorted(f for f in test_files if f not in owners)
        if multi_layer:
            _drift("TEST_CONTRACTS gives a test file more than one layer", ", ".join(multi_layer))
        elif unowned:
            _drift("TEST_CONTRACTS layers own no test file", ", ".join(unowned))
        else:
            _report("OK", "every test file is owned by exactly one layer")
        referenced = set(re.findall(r"`(test_[a-zA-Z0-9_]+\.py)`", tc_text))
        vanished = sorted(f for f in referenced if not _exists("tests", f))
        if vanished:
            _drift("TEST_CONTRACTS references missing tests", ", ".join(vanished))
        else:
            _report("OK", "TEST_CONTRACTS refs resolve")

    # --- 2. Backticked file paths in AGENTS.md + skills resolve ------------
    missing_files = _all_missing_doc_refs()
    if missing_files:
        _drift("docs reference missing files", ", ".join(missing_files))
    else:
        _report("OK", "doc file references resolve")

    # --- 3. Contract constants agree across the docs that quote them --------
    try:
        from pgrag.config import DOCUMENTS_VERSION
    except Exception as exc:  # pragma: no cover - environment
        _drift("cannot import pgrag.config", str(exc))
        DOCUMENTS_VERSION = None

    if DOCUMENTS_VERSION is not None:
        # The VALUE lives only in config.py; docs should reference the symbol
        # (asserting the numeric value would force a drift-prone literal on
        # every version bump).
        for label, path in [
            ("AGENTS.md", ROOT / "AGENTS.md"),
            ("TEST_CONTRACTS", tc),
            ("RULES.md", ROOT / ".omp" / "RULES.md"),
            ("pg-rag skill", ROOT / ".agents" / "skills" / "pg-rag" / "SKILL.md"),
        ]:
            if not path.exists():
                # Never a silent skip: the file is one of the docs this gate
                # exists to keep truthful, so its absence *is* the drift.
                _report("DRIFT", f"{label} missing — cannot check DOCUMENTS_VERSION")
                continue
            ok = "DOCUMENTS_VERSION" in path.read_text(encoding="utf-8")
            _report("OK" if ok else "DRIFT", f"DOCUMENTS_VERSION referenced in {label}")

    # Golden shape literal: {id, question, type, facts   (owners: TEST_CONTRACTS
    # L6 + WATCHDOG trap; RULES restates nothing)
    shape = "{id, question, type, facts"
    for label, path in [
        ("TEST_CONTRACTS", tc),
        ("WATCHDOG.md", ROOT / ".omp" / "WATCHDOG.md"),
    ]:
        ok = path.exists() and shape in path.read_text(encoding="utf-8")
        _report("OK" if ok else "DRIFT", f"golden shape in {label}")

    # --- 4. Golden counts the evaluation skill quotes are real --------------
    eval_skill = ROOT / ".agents" / "skills" / "evaluation" / "SKILL.md"
    golden_dir = ROOT / "data" / "golden"
    if not eval_skill.exists():
        _drift("evaluation skill missing", ".agents/skills/evaluation/SKILL.md")
    elif not golden_dir.is_dir():
        _drift("golden dir missing", "data/golden")
    else:
        golden_files = sorted(golden_dir.glob("*.json"))
        goldens = [json.loads(f.read_text(encoding="utf-8")) or {} for f in golden_files]
        types = Counter(g.get("type", "?") for g in goldens)
        text = eval_skill.read_text(encoding="utf-8")
        known_types = {"entity", "recipe", "comparison", "general"}
        mismatches = []
        # A `type` outside the harness vocabulary is an unroutable golden: it
        # slips past the store-only skip (keyed on the `store` flag) AND past
        # golden_check's `--type` choices, so nothing owns it.
        unknown = sorted(t for t in types if t not in known_types)
        if unknown:
            mismatches.append(
                "unroutable type(s): " + ", ".join(f"{t}={types[t]}" for t in unknown)
            )
        if f"{len(golden_files)} files exist today" not in text:
            mismatches.append(f"count ({len(golden_files)})")
        for t, n in sorted(types.items()):
            if t not in known_types:
                continue
            if f"{t} {n}" not in text and f"{n} {t}" not in text:
                mismatches.append(f"{t}={n} not quoted in skill")
        store_only = sum(1 for g in goldens if g.get("store"))
        if f"{store_only} store-only" not in text:
            mismatches.append(f"store-only={store_only} not quoted in skill")
        if mismatches:
            _drift("evaluation skill golden counts stale", "; ".join(mismatches))
        else:
            _report("OK", "evaluation skill golden counts match")

    # --- 4b. Golden harness surface (owner: pgrag.agentic.eval) ------------
    # The classic harness was deleted; golden ownership + tier membership now
    # live in pgrag.agentic.eval, driven by the loop CLI scripts/golden_check.py.
    # Assert that binding (not the old pipeline shape) so a re-split is caught.
    try:
        from pgrag.agentic import eval as agentic_eval
    except Exception as exc:  # pragma: no cover - environment
        _drift("cannot import pgrag.agentic.eval", str(exc))
    else:
        gd = ROOT / agentic_eval.GOLDEN_DIR
        if not gd.is_dir():
            _drift("agentic eval GOLDEN_DIR missing", str(agentic_eval.GOLDEN_DIR))
        else:
            _report("OK", "agentic eval GOLDEN_DIR resolves")
            missing_tier = [
                t for t in agentic_eval.SHORT_TIER_IDS if not (gd / f"{t}.json").exists()
            ]
            if missing_tier:
                _drift("SHORT_TIER_IDS name missing goldens", ", ".join(missing_tier))
            else:
                _report("OK", "SHORT_TIER_IDS resolve to golden files")
        gc_path = ROOT / "scripts" / "golden_check.py"
        if not gc_path.exists():
            # Unguarded, this read crashed the whole report — including the drift
            # lines already collected — with a traceback.
            _drift("golden_check.py missing", "scripts/golden_check.py")
        else:
            gc = gc_path.read_text(encoding="utf-8")
            wanted_flags = [
                "--tier",
                "--type",
                "--max-facts",
                "--diagnose",
                "--export",
                "--ids",
                "--corpus",
                "--max-rounds",
                "--store",
                "--report",
            ]
            missing_flags = [f for f in wanted_flags if f not in gc]
            if missing_flags:
                _drift("golden_check.py missing flags", ", ".join(missing_flags))
            else:
                _report("OK", "golden_check.py carries the documented flags")

    # --- 5. Expected skills exist and are well-formed -----------------------
    # `CANONICAL_SKILLS` plus the tree, NOT the tree alone: the list silently
    # stopped covering `molab-notebook` when that skill landed (a broken
    # frontmatter there produced no drift), but a tree-only list also cannot
    # report a whole canonical skill being deleted. See `_expected_skills`.
    for name in _expected_skills():
        p = ROOT / ".agents" / "skills" / name / "SKILL.md"
        if not p.exists():
            _drift(f"missing skill: {name}")
            continue
        head = p.read_text(encoding="utf-8")[:600]
        if "name:" not in head or "description:" not in head:
            _drift(f"skill {name} missing frontmatter")
        else:
            _report("OK", f"skill {name} present")

    # --- 6. Model refs are single-sourced in mise.toml [env] ---------------
    # Every consumer (scripts/services.py, scripts/agentic_tune.py, AGENTS.md)
    # must reference the *_MODEL/*_FLAGS variable — never the literal value. A
    # swapped model without the SAME edit in all consumers is exactly the drift
    # this catches (a bare literal in any consumer file = a leftover from before
    # the swap).
    try:
        mise_env = tomllib.loads(_read("mise.toml")).get("env", {})
    except (OSError, tomllib.TOMLDecodeError) as exc:  # pragma: no cover
        _drift("cannot parse mise.toml [env]", str(exc))
        mise_env = {}

    model_keys = [
        "LLM_MODEL",
        "LLM_FLAGS",
    ]
    if all(k in mise_env for k in model_keys):
        # Files where a literal model ref/flag WOULD be a leftover.
        consumers = {
            "scripts/services.py": ROOT / "scripts" / "services.py",
            "scripts/agentic_tune.py": ROOT / "scripts" / "agentic_tune.py",
            "AGENTS.md": ROOT / "AGENTS.md",
        }
        for label, path in consumers.items():
            if not path.exists():
                _drift(f"{label} missing — cannot check model literals")
                continue
            text = path.read_text(encoding="utf-8")
            leaked = [k for k in model_keys if mise_env[k] in text]
            if leaked:
                _drift(
                    f"{label} hardcodes model literal",
                    ", ".join(f"{k}={mise_env[k]}" for k in leaked),
                )
            else:
                varref = [v for v in model_keys if v in text]
                _report("OK", f"{label} references env vars: {', '.join(varref) or 'none'}")

    else:
        # A missing key silently skips all model checks — fail loudly instead.
        _drift(
            "missing model env keys in mise.toml [env]",
            ", ".join(k for k in model_keys if k not in mise_env),
        )

    # --- 7. The corpus writer locks the tree it writes, CWD-relative --------
    # The data tree is addressed from the CWD (the molab sandbox roots `data/`
    # at the sandbox CWD while the package lives in `pg-rag-src/`), so a lock on
    # the package-anchored `DATA_DIR` would guard a *different* directory than
    # the file being written — two writers, no serialization, and the pre-hook
    # (which resolves the repo root) would agree with neither.
    cli_path = ROOT / "src" / "pgrag" / "cli.py"
    if not cli_path.exists():
        _drift("src/pgrag/cli.py missing — cannot check the corpus lock")
    else:
        # Parse, don't grep: the file's *comments* explain the trap (and name the
        # constant), while only the code decides what gets locked.
        try:
            cli_tree = ast.parse(cli_path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover - a broken cli.py is its own failure
            _drift("cannot parse src/pgrag/cli.py", str(exc))
            cli_tree = ast.Module(body=[], type_ignores=[])
        lock_targets = [
            ast.unparse(node.args[0])
            for node in ast.walk(cli_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "corpus_lock"
            and node.args
        ]
        data_dir_refs = [
            node.lineno
            for node in ast.walk(cli_tree)
            if isinstance(node, ast.Name) and node.id == "DATA_DIR"
        ]
        problems = []
        if not lock_targets:
            problems.append("build-documents takes no corpus lock")
        if any(t != LOCK_TARGET for t in lock_targets):
            problems.append(f"locks {', '.join(lock_targets)} instead of {LOCK_TARGET}")
        if data_dir_refs:
            problems.append(f"references the package-anchored DATA_DIR (line {data_dir_refs[0]})")
        if problems:
            _drift("the documents rebuild does not lock its data tree", "; ".join(problems))
        else:
            _report("OK", "the documents rebuild locks its data tree")

    # --- 8. No lint suppression names a rule that is not selected -----------
    # An `ignore` for an unselected family suppresses nothing while looking like
    # a safeguard (six of nine entries were dead here), so the list is checked
    # against `select` instead of trusted.
    try:
        ruff_cfg = tomllib.loads(_read("ruff.toml"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        _drift("cannot parse ruff.toml", str(exc))
        ruff_cfg = {}
    lint_cfg = ruff_cfg.get("lint", {}) or {}
    selected = list(lint_cfg.get("select", []))
    dead = [f"lint.ignore:{c}" for c in lint_cfg.get("ignore", []) if not _code_selected(c, selected)]
    for scope, codes in (lint_cfg.get("per-file-ignores", {}) or {}).items():
        dead += [f"{scope}:{c}" for c in codes if not _code_selected(c, selected)]
    if dead:
        _drift("lint suppressions name unselected rules", ", ".join(sorted(dead)))
    elif not selected:
        _drift("ruff.toml selects nothing", "lint.select is empty or missing")
    else:
        _report("OK", "every lint suppression names a selected rule")

    # --- 9. The gates' own runner files are tracked -------------------------
    # `mise lint-ts` and the L8 tests shell out to these. Untracked (in .gitignore's
    # "tracked" shadow, say) a fresh clone has no runner at all — and the TS tests
    # *skip* when the parser is missing, so nothing else would ever say so.
    gate_files = ("scripts/check_ts.mjs", "package.json", "package-lock.json")
    tracked = _git_tracked(gate_files)
    if tracked is None:
        _drift("cannot read the git index", "git ls-files failed")
    else:
        untracked = [p for p in gate_files if p not in tracked]
        if untracked:
            _drift("gate runner files are untracked", ", ".join(untracked))
        else:
            _report("OK", "the gates' runner files are tracked")

    # --- 10. AGENTS.md's task list is the real task set ---------------------
    # The doc says "exactly", so it is compared set-for-set; a task added without
    # the doc (or a deleted one left listed) is drift.
    try:
        task_names = set(tomllib.loads(_read("mise.toml")).get("tasks", {}))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        _drift("cannot parse mise.toml [tasks]", str(exc))
        task_names = set()
    agents_path = ROOT / "AGENTS.md"
    if not task_names:
        _report("OK", "mise.toml defines no tasks to check")
    elif not agents_path.exists():
        _drift("AGENTS.md missing — cannot check the task list")
    else:
        listed = re.search(
            r"`\[tasks\]` set is exactly:(.*?)\.", agents_path.read_text(encoding="utf-8"), re.S
        )
        if listed is None:
            _drift("AGENTS.md does not list the task set", "the `[tasks]` set is exactly: … line")
        else:
            quoted = set(re.findall(r"`([a-z][a-z0-9-]*)`", listed.group(1)))
            if quoted != task_names:
                _drift(
                    "AGENTS.md task list drifted",
                    f"missing: {sorted(task_names - quoted) or '—'}; "
                    f"not tasks: {sorted(quoted - task_names) or '—'}",
                )
            else:
                _report("OK", f"AGENTS.md lists all {len(task_names)} tasks")

    # --- 11. The suite counts the docs quote are the live ones --------------
    # Volatile by nature (every added test moves them), so nothing but a real
    # collection may be their source — and every doc that quotes them must quote
    # the same triple, or the reader cannot tell which one is current.
    quote_docs = {
        "AGENTS.md": ROOT / "AGENTS.md",
        "testing skill": ROOT / ".agents" / "skills" / "testing" / "SKILL.md",
        "REVIEW.md": ROOT / "docs" / "REVIEW.md",
    }
    counts = _collected_counts()
    if counts is None:
        _drift("cannot count the test suite", "pytest --collect-only produced no count")
    else:
        collected, deselected, default = counts
        hint = f"{collected} collected / {deselected} deselected / {default} default"
        for label, path in quote_docs.items():
            if not path.exists():
                _drift(f"{label} missing — cannot check the suite counts")
                continue
            match = _COUNTS_QUOTE_RE.search(path.read_text(encoding="utf-8"))
            if match is None:
                _drift(f"{label} does not quote the suite counts", hint)
            elif tuple(int(g) for g in match.groups()) != counts:
                _drift(f"{label} quotes stale suite counts", f"{match.group(0)} — live: {hint}")
            else:
                _report("OK", f"{label} quotes the live suite counts")

    # --- Report -------------------------------------------------------------
    print(
        f"drift check: {sum(1 for t, *_ in _lines if t == 'OK')} OK, "
        f"{sum(1 for t, *_ in _lines if t == 'DRIFT')} drift"
    )
    for tag, label, detail in _lines:
        line = f"[{tag}] {label}"
        if detail:
            line += f" — {detail}"
        print(line)
    return 1 if any(t == "DRIFT" for t, *_ in _lines) else 0


if __name__ == "__main__":
    raise SystemExit(main())
