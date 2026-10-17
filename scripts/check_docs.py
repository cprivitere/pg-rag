"""Drift guard: keep the docs/skills truthful about the repo.

Prose documentation (AGENTS.md, .omp/RULES.md, skills, docs/TEST_CONTRACTS.md)
drifts unless something checks it against the code. The one guardrail in this
repo that never drifted is enforced code (conftest's immutability guard) — this
script is the documentation analogue. It asserts stable, meaningful facts:

- every test file is covered by docs/TEST_CONTRACTS.md (and nothing it
  references has vanished) — the layer->test map cannot quietly diverge;
- file paths named in backticks under AGENTS.md / the skills actually exist
  (`.py`, `.sh` and `.ps1` — the executable-script classes were deleted by the
  task/script audit, so a `.py`-only scan could not see a surviving pointer);
- contract constants (DOCUMENTS_VERSION) and the golden shape agree across the
  docs that quote them;
- the golden-set counts the evaluation skill quotes are real.

Exit 0 = clean; 1 = one or more drifts (printed). Run via `mise drift`
(uv run python scripts/check_docs.py). Only stable facts are asserted — a
check that breaks on innocuous churn gets ignored, which is how guardrails rot.
"""

from __future__ import annotations

import json
import re
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
    r"`((?:scripts|src|tests|docs|\.mise/tasks)/[A-Za-z0-9_./-]+\.(?:py|sh|ps1|mjs))"
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


def _all_missing_doc_refs() -> list[str]:
    """Every unresolved doc pointer across the sources."""
    missing: list[str] = []
    for source in _doc_sources():
        if not source.exists():
            continue
        base = source.parent if source.name == "SKILL.md" else None
        missing += [
            f"{source.relative_to(ROOT).as_posix()}: {ref}"
            for ref in _missing_doc_refs(source.read_text(encoding="utf-8"), base)
        ]
    return missing


def main() -> int:
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
    if eval_skill.exists() and golden_dir.is_dir():
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
        gc = (ROOT / "scripts" / "golden_check.py").read_text(encoding="utf-8")
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
