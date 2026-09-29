"""Parity guard: shared code must stay byte-identical across the two-repo split.

`pg-rag-public` and `pg-rag-private` deliberately share one source of truth for
pipeline code: the private checkout is a strict superset (identical shared
files + IL2CPP decomp source). This script enforces that invariant by diffing
every shared file (tracked in BOTH repos), with line endings normalized, and
failing on any *unsanctioned* difference.

Sanctioned differences live in SANCTIONED below with the reason they differ —
add a file there when a divergence is a deliberate design decision, never as
a convenience merge.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# This script lives in BOTH repos. The repo it runs from is irrelevant; both
# halves of the split are hard-coded below (both checkouts are on this
# machine). Run from either repo — checks are symmetric.
PUBLIC = Path(r"F:\ProjectGorgon\pg-rag-public")
# The private repo's local checkout (dir name is historical — the private
# repo's GitHub name is pg-rag-builder; its package identity is pg-rag-private).
PRIVATE = Path(r"F:\ProjectGorgon\pg-rag-builder")

# Files allowed to differ, with the sanctioned reason.
SANCTIONED = {
    "AGENTS.md": "overlay role vs public-corpus policy prose",
    "docs/MOLAB_OPS.md": "notebook lives in public; private variant points there",
    "docs/TEST_CONTRACTS.md": "private contract map includes decomp tests",
    "mise.toml": "private has the sync-il2cpp task (public has check-parity note wording)",
    ".gitignore": "private tracks data/eval/*.json fixtures (public has no data/eval)",
    "pyproject.toml": "package name pg-rag-public vs pg-rag-private",
    "uv.lock": "package name only",
    "scripts/check_docs.py": "private quotes the 42-golden set (incl. il2cpp)",
    "scripts/pg_rag.py": "PG_RAG_ROOT default points at its own checkout",
    "src/pgrag/documents/builder.py": "private imports decomp_builder directly (no guarded hook)",
    "tests/test_golden_check.py": "private golden set includes the il2cpp xfail case",
    # identity prose (repo name appears in the text)
    ".agents/skills/pg-rag/SKILL.md": "repo-name identity + overlay mention",
    ".agents/skills/testing/SKILL.md": "repo-name identity",
    ".omp/RULES.md": "repo-name identity",
    ".omp/WATCHDOG.md": "repo-name identity",
    ".omp/config.yml": "repo-name identity comment",
    ".omp/hooks/pre/00-repo-lock.ts": "repo-name identity comment",
    "GOLDEN_CORPUS_REBALANCING_PLAN.md": "repo-root path in instructions",
    # golden-count prose (42 incl. il2cpp vs public 38)
    ".agents/skills/evaluation/SKILL.md": "golden counts 42 (private) vs 38 (public)",
    # overlay section differs: public documents the hook as external; private documents the real source
    ".agents/skills/pg-data/SKILL.md": "il2cpp section: external-hook note (public) vs real source (private)",
}

# The private-only overlay (decomp source + its docs/tests/goldens).
# Legitimate to live only in the private repo — but must NEVER appear in the
# public repo (leak guard in main()).
OVERLAY_FILES = {
    "IL2CPP_DECOMP_CORPUS_PHASE_1_PLAN.md",
    "docs/discovered-mechanic-prose.md",
    "docs/discovered-schemas.md",
    "scripts/analyze_schemas.py",
    "scripts/analyze_stringliteral.py",
    "scripts/sync_il2cpp.py",
    "src/pgrag/documents/decomp_builder.py",
    "tests/test_decomp_builder.py",
    "tests/test_sync_il2cpp.py",
    # decomp-derived golden cases (removed from the public 38-golden set)
    "data/golden/peaceableness-rakshasa.json",
    "data/golden/il2cpp-enum-ability-requirement.json",
    "data/golden/il2cpp-mechanic-combat-xp.json",
    "data/golden/il2cpp-mechanic-curse-remedy.json",
    "data/golden/il2cpp-schema-item-fields.json",
    # agentic SQLite prototype (store + tool loop + CLI + tests)
    "src/pgrag/agentic/store.py",
    "src/pgrag/agentic/session.py",
    "src/pgrag/agentic/tools.py",
    "src/pgrag/agentic/loop.py",
    "scripts/build_sqlstore.py",
    "scripts/agentic_chat.py",
    "scripts/build_tool_corpus.py",
    "tests/test_sqlstore_build.py",
    "tests/test_agentic_tools.py",
    "tests/test_agentic_loop.py",
    "tests/test_session_ingest.py",
    # agentic tuning harness (sweeps over model/temperature/reasoning budget)
    "scripts/agentic_tune.py",
    "tests/test_agentic_tune.py",
    # agentic eval case fixtures (private cases for the loop eval)
    "data/eval/tool_cases.json",
    "data/eval/tune_cases.json",
    "data/agentic_tune/README.md",
}


def tracked_files(repo: Path) -> set[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return {f.replace("\\", "/") for f in out}


def normalized(repo: Path, rel: str) -> bytes:
    return (repo / rel).read_bytes().replace(b"\r\n", b"\n")


def main() -> int:
    pub_files = tracked_files(PUBLIC)
    pri_files = tracked_files(PRIVATE)
    shared = pub_files & pri_files

    private_only = sorted(pri_files - pub_files)
    unsanctioned = [f for f in private_only if f not in OVERLAY_FILES]
    if unsanctioned:
        for f in unsanctioned:
            print(f"[FAIL] private-only tracked file not in public: {f}")
        return 1

    # Leak guard: none of the overlay files may ever appear in the public repo.
    leaked = sorted(OVERLAY_FILES & pub_files)
    if leaked:
        for f in leaked:
            print(f"[FAIL] OVERLAY LEAK into public repo: {f}")
        return 1

    failures = 0
    for f in sorted(shared):
        try:
            same = normalized(PUBLIC, f) == normalized(PRIVATE, f)
        except FileNotFoundError:
            print(f"[FAIL] shared file missing on disk: {f}")
            failures += 1
            continue
        if same:
            continue
        if f in SANCTIONED:
            continue
        print(f"[FAIL] shared file drifted: {f}")
        failures += 1

    # Staleness report: if a sanctioned file becomes identical again, the
    # entry should be removed so the sanctioned list tracks reality.
    for f, reason in SANCTIONED.items():
        if f not in shared:
            continue
        try:
            if normalized(PUBLIC, f) == normalized(PRIVATE, f):
                print(f"[STALE] {f} is identical now — remove from SANCTIONED ({reason})")
        except FileNotFoundError:
            pass

    if failures:
        print(
            f"\nparity check: {failures} failure(s). Mirror the file from the repo you "
            "edited in, or add a SANCTIONED entry if the divergence is deliberate."
        )
        return 1
    print(f"parity check: OK — {len(shared)} shared files identical, {len(SANCTIONED)} sanctioned divergences")
    return 0


if __name__ == "__main__":
    sys.exit(main())
