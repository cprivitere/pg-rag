"""Drift guard: keep the docs/skills truthful about the repo.

Prose documentation (AGENTS.md, .omp/RULES.md, skills, docs/TEST_CONTRACTS.md)
drifts unless something checks it against the code. The one guardrail in this
repo that never drifted is enforced code (conftest's immutability guard) — this
script is the documentation analogue. It asserts stable, meaningful facts:

- every test file is covered by docs/TEST_CONTRACTS.md (and nothing it
  references has vanished) — the layer->test map cannot quietly diverge;
- file paths named in backticks under AGENTS.md / the skills actually exist;
- contract constants (DOCUMENTS_VERSION, EMBEDDING_DIM) and the golden shape
  agree across the docs that quote them;
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
    doc_sources = [ROOT / "AGENTS.md", *(ROOT / ".agents" / "skills").glob("*/SKILL.md")]
    doc_blob = "\n".join(f.read_text(encoding="utf-8") for f in doc_sources if f.exists())
    named = set(re.findall(r"`((?:scripts|src|tests|docs)/[A-Za-z0-9_./]+\.py)`", doc_blob))
    missing_files = sorted(f for f in named if not _exists(f))
    if missing_files:
        _drift("docs reference missing files", ", ".join(missing_files))
    else:
        _report("OK", "doc file references resolve")

    # --- 3. Contract constants agree across the docs that quote them --------
    try:
        from pgrag.config import DOCUMENTS_VERSION, EMBEDDING_DIM
    except Exception as exc:  # pragma: no cover - environment
        _drift("cannot import pgrag.config", str(exc))
        DOCUMENTS_VERSION = EMBEDDING_DIM = None

    if DOCUMENTS_VERSION is not None:
        # The VALUE lives only in config.py; docs should reference the symbol
        # (asserting the numeric value would force a drift-prone literal on
        # every version bump). EMBEDDING_DIM (384) is a stable contract.
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
        for label, path in [
            ("AGENTS.md", ROOT / "AGENTS.md"),
            ("pg-rag skill", ROOT / ".agents" / "skills" / "pg-rag" / "SKILL.md"),
        ]:
            ok = str(EMBEDDING_DIM) in path.read_text(encoding="utf-8")
            _report("OK" if ok else "DRIFT", f"EMBEDDING_DIM=={EMBEDDING_DIM} in {label}")

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
        types = Counter(
            (json.loads(f.read_text(encoding="utf-8")) or {}).get("type", "?") for f in golden_files
        )
        text = eval_skill.read_text(encoding="utf-8")
        expected = {"entity": 23, "general": 22, "recipe": 7, "comparison": 6}
        mismatches = []
        if f"{len(golden_files)} files exist today" not in text:
            mismatches.append(f"count ({len(golden_files)})")
        for t, n in expected.items():
            if f"{t} {n}" not in text and f"{n} {t}" not in text:
                mismatches.append(f"{t}={types.get(t, 0)} (doc says {n})")
        if mismatches:
            _drift("evaluation skill golden counts stale", "; ".join(mismatches))
        else:
            _report("OK", "evaluation skill golden counts match")

    # --- 5. Expected skills exist and are well-formed -----------------------
    expected_skills = ["pg-data", "pg-rag", "retrieval", "evaluation", "testing"]
    for name in expected_skills:
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
    # Every consumer (.mise/tasks/*-start.ps1, mise debug-* tasks, AGENTS.md,
    # skills docs) must reference the *_MODEL/*_FLAGS variable / template —
    # never the literal value. A swapped model without the SAME edit in all
    # consumers is exactly the drift this catches (a bare literal in any
    # consumer file = a leftover from before the swap).
    try:
        mise_env = tomllib.loads(_read("mise.toml")).get("env", {})
    except (OSError, tomllib.TOMLDecodeError) as exc:  # pragma: no cover
        _drift("cannot parse mise.toml [env]", str(exc))
        mise_env = {}

    model_keys = [
        "LLM_MODEL",
        "LLM_FLAGS",
        "EMBED_MODEL",
        "EMBED_FLAGS",
        "RERANK_MODEL",
        "RERANK_FLAGS",
    ]
    if all(k in mise_env for k in model_keys):
        # Files where a literal model ref/flag WOULD be a leftover. vram_sweep
        # carries deliberate equal fallbacks (reads env first) — excluded.
        consumers = {
            "llm-start.ps1": ROOT / ".mise" / "tasks" / "llm-start.ps1",
            "embed-start.ps1": ROOT / ".mise" / "tasks" / "embed-start.ps1",
            "rerank-start.ps1": ROOT / ".mise" / "tasks" / "rerank-start.ps1",
            "mise.toml debug-* tasks": ROOT / "mise.toml",
            "AGENTS.md": ROOT / "AGENTS.md",
        }
        for label, path in consumers.items():
            text = path.read_text(encoding="utf-8")
            if label == "mise.toml debug-* tasks":
                # mise.toml itself legitimately CONTAINS the values (it is
                # the source) — only the debug-* task run-strings must
                # reference {{ env.X }}. Only debug-embed + debug-llm exist
                # (no debug-rerank), so check just the EMBED/LLM keys.
                task_keys = [k for k in model_keys if k.startswith(("LLM_", "EMBED_"))]
                leaks = [k for k in task_keys if f"{{{{ env.{k} }}}}" not in text]
                if leaks:
                    _drift("mise debug tasks missing env template", ", ".join(leaks))
                else:
                    _report("OK", "mise debug tasks reference env templates")
            else:
                leaked = [k for k in model_keys if mise_env[k] in text]
                if leaked:
                    _drift(
                        f"{label} hardcodes model literal",
                        ", ".join(f"{k}={mise_env[k]}" for k in leaked),
                    )
                else:
                    varref = [
                        v
                        for v in (
                            "LLM_MODEL",
                            "EMBED_MODEL",
                            "RERANK_MODEL",
                            "LLM_FLAGS",
                            "EMBED_FLAGS",
                            "RERANK_FLAGS",
                        )
                        if v in text
                    ]
                    _report("OK", f"{label} references env vars: {', '.join(varref) or 'none'}")

        # embed_eval.py's production bake-off candidate must read EMBED_MODEL
        # from [env] (it legitimately carries OTHER candidates as literals —
        # only the production row is single-sourced). Assert the env reference
        # rides along with the manifest, not that literals are absence (which
        # would false-positive on the comparison models).
        ee = (ROOT / "scripts" / "embed_eval.py").read_text(encoding="utf-8")
        if "EMBED_MODEL" in ee and not re.search(
            r'tomllib\.loads.*EMBED_MODEL|get\("EMBED_MODEL"\)', ee, re.S
        ):
            _drift("embed_eval.py references EMBED_MODEL but not from mise.toml [env]")
        elif "EMBED_MODEL" not in ee:
            _drift("embed_eval.py no longer references EMBED_MODEL (production bake-off unfixed)")
        else:
            _report("OK", "embed_eval.py production candidate sourced from EMBED_MODEL")

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
