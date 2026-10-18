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


# pytest's own collection summaries, with and without a deselect count:
#   "490/568 tests collected (78 deselected) in 0.98s"
#   "12 tests collected in 0.4s"
#   "no tests collected (3 deselected) in 0.1s"
# A config that deselects nothing is legitimate, so the deselect half is optional;
# demanding it (the first version of this check) reported "cannot count the test
# suite" for a tree that was merely configured differently.
_COLLECTED_RE = re.compile(r"(?:(\d+)/)?(\d+|no) tests? collected(?: \((\d+) deselected\))?")
_COUNTS_QUOTE_RE = re.compile(r"(\d+) collected / (\d+) deselected / (\d+) default")

# The leading letter run of a rule code, i.e. its linter family (`BLE001` -> `BLE`).
_FAMILY_RE = re.compile(r"[A-Z]+")


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
CANONICAL_SKILLS = ("pg-data", "pg-rag", "retrieval", "evaluation", "testing")


def _expected_skills() -> list[str]:
    """Every skill that must exist: `CANONICAL_SKILLS` plus the tree."""
    present = {p.parent.name for p in (ROOT / ".agents" / "skills").glob("*/SKILL.md")}
    return sorted(set(CANONICAL_SKILLS) | present)


def _code_selected(code: str, select: list[str]) -> bool:
    """True when `select` covers `code`.

    Ruff resolves a selector *inside its linter first*: `B` is flake8-bugbear and
    so never covers `BLE001` (flake8-blind-except), `F` is pyflakes and never
    covers `FA`/`FURB`. A raw string prefix — the first version of this check —
    judged those families "selected", i.e. it accepted exactly the dead
    suppressions it exists to catch. The rule is therefore: a code-prefix match
    *within the same leading-letter family*, which still lets `B` cover `B008`,
    `E4` cover `E402` and `RUF` cover `RUF059`."""
    family = _FAMILY_RE.match(code)
    if family is None:  # pragma: no cover - every ruff code starts with letters
        return False
    for sel in select:
        sel_family = _FAMILY_RE.match(sel)
        if sel_family is not None and code.startswith(sel) and sel_family.group() == family.group():
            return True
    return False


def _git_tracked(paths: tuple[str, ...]) -> set[str] | None:
    """The subset of `paths` the git index knows, or None when git cannot answer
    (not a repo, no git binary).

    The explicit codec is load-bearing: a decode failure kills subprocess's reader
    thread, so `stdout` comes back `None` with no exception and `splitlines()` on
    it raises. `cwd=ROOT` is load-bearing too — `git ls-files -- package.json`
    from a subdirectory answers rc 0 with empty output, which would report every
    runner as untracked."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--", *paths],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return {line.strip() for line in (proc.stdout or "").splitlines() if line.strip()}


def _parse_collection(out: str) -> tuple[int, int, int] | None:
    """`(collected, deselected, default)` from pytest's summary, or None.

    All three shapes count: `N/M tests collected (K deselected)`, `N tests
    collected` (nothing deselected — `default == collected`) and `no tests
    collected (K deselected)` (everything deselected)."""
    matches = _COLLECTED_RE.findall(out)
    if not matches:
        return None
    default_raw, collected_raw, deselected_raw = matches[-1]  # the summary line
    collected = 0 if collected_raw == "no" else int(collected_raw)
    default = collected if not default_raw else int(default_raw)
    return collected, int(deselected_raw or 0), default


def _collected_counts() -> tuple[tuple[int, int, int] | None, str]:
    """`((collected, deselected, default) | None, detail)` from a real collection.

    One `pytest --collect-only -q` run (~1.4 s here) is the only honest source:
    the golden cases are parametrized from the files on disk, so counting `def
    test_` functions statically cannot see them. The docs quote the triple
    `N collected / M deselected / K default` and this is what it must equal —
    the numbers churned three review passes in a row ("537/459" outlived the
    tree by 8 tests) precisely because nothing computed them.

    `None` means *not measured*, with `detail` saying why: a collection that
    failed is its own failure (the partial summary a broken run prints names a
    count that does not exist), so it is never reported as a stale count.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"pytest --collect-only could not start ({type(exc).__name__})"
    if proc.returncode != 0:
        return None, f"pytest --collect-only rc {proc.returncode}"
    got = _parse_collection(proc.stdout or "")
    if got is None:
        return None, "pytest --collect-only printed no summary line"
    collected, deselected, default = got
    return got, f"{collected} collected / {deselected} deselected / {default} default"


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
        # from the layer blocks only — the map also discusses files in prose — and
        # once *per block*: a layer that names one file twice, or a fenced example
        # of the convention, is not two layers. Fences are skipped and all three
        # bullet markers open a block, because a `*`/`+` reformat used to make every
        # file unowned (a loud drift with the wrong reason).
        owners: dict[str, set[int]] = {}
        in_tests = False
        in_fence = False
        block = -1
        for line in tc_text.splitlines():
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if re.match(r"\s*[-*+]\s+\*\*Tests\*\*", line):
                in_tests = True
                block += 1
            elif in_tests and (
                not line.strip() or line.lstrip().startswith(("#", "-", "*", "+", "|"))
            ):
                in_tests = False
                continue
            if in_tests:
                for name in re.findall(r"`(test_[a-zA-Z0-9_]+\.py)`", line):
                    owners.setdefault(name, set()).add(block)
        multi_layer = sorted(n for n, blocks in owners.items() if len(blocks) > 1)
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
    # `CANONICAL_SKILLS` plus the tree, NOT the tree alone: the list once
    # silently stopped covering a newly landed skill (a broken frontmatter
    # there produced no drift), but a tree-only list also cannot report a
    # whole canonical skill being deleted. See `_expected_skills`.
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
    # `lint.ignore` is absent from ruff.toml today (its last entry went with the
    # dead-suppression cleanup), so this arm guards its return; the live
    # suppressions are the per-file-ignores below.
    dead = [
        f"lint.ignore:{c}" for c in lint_cfg.get("ignore", []) if not _code_selected(c, selected)
    ]
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
    # the same triple, or the reader cannot tell which one is current. This map
    # states the *convention* (with placeholders) rather than a count, so it is
    # deliberately not in the set and carries no triple to drift.
    quote_docs = {
        "AGENTS.md": ROOT / "AGENTS.md",
        "testing skill": ROOT / ".agents" / "skills" / "testing" / "SKILL.md",
    }
    counts, detail = _collected_counts()
    if counts is None:
        _drift("cannot count the test suite", detail)
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
