"""Compliance scanner: pg-rag x Project Gorgon TOS/CoC hard lines.

Port of glogger-oddio's check-glogger-constraints.ps1 (docs/research/
pg-rag-game-interaction-limits.md §6). Exits 1 on ANY violation. Skippable
only with PG_SKIP_CONSTRAINTS=1 (prints a loud warning, never silent).

Self-exclusion: this script and the policy doc name the prohibited patterns
in order to grep for them, so they are excluded from their own scan.

Categories (§2 of the policy doc):
  1 FORBIDDEN-API    process/memory access, input synthesis, clock hooks
  2 PACKET-TOOLING   capture libraries + game-port socket references
  3 RE-ARTIFACT      client binaries, metadata files, dump outputs, dumper names
  4 WIRE-KNOWLEDGE   wire-protocol reader identifiers
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_ENV = "PG_SKIP_CONSTRAINTS"

# Trees scanned for code patterns. (Docs, skills, and manifests are scanned
# for artifact/wire patterns only — the API greps are code-shaped.)
CODE_GLOBS = ["src/**/*.py", "scripts/**/*.py", "tests/**/*.py"]
ALL_GLOBS = [
    *CODE_GLOBS,
    "docs/**/*.md",
    ".agents/**/*.md",
    "*.md",
    "*.toml",
]

# Paths excluded from every category: the checker itself (names patterns),
# the policy doc (quotes the TOS text), and VCS/cache noise.
SELF_EXCLUDED = {
    "scripts/check_constraints.py",
    "docs/research/pg-rag-game-interaction-limits.md",
}

DIR_EXCLUSIONS = {".git", "__pycache__", ".venv", "node_modules", "data", "logs"}

# 1 — process/memory access, input synthesis, clock manipulation
FORBIDDEN_API = [
    r"\bOpenProcess\b",
    r"\bReadProcessMemory\b",
    r"\bWriteProcessMemory\b",
    r"\bVirtualAllocEx\b",
    r"\bVirtualProtectEx\b",
    r"\bCreateRemoteThread\b",
    r"\bNtCreateThreadEx\b",
    r"\bRtlCreateUserThread\b",
    r"\bDebugActiveProcess\b",
    r"\bSetWindowsHookEx\b",
    r"\bSendInput\b",
    r"\bkeybd_event\b",
    r"\bmouse_event\b",
    r"\bBlockInput\b",
    r"\bSetSystemTime\b",
    r"\bSetLocalTime\b",
    r"\bNtSetSystemTime\b",
    r"\bZwSetSystemTime\b",
]

# 2 — capture tooling + game-port socket references
PACKET_TOOLING = [
    r"\bWinDivert\b",
    r"\bNpcap\b",
    r"\bpcapng\b",
    r"\bpcap\b",
    r"\bpktmon\b",
    r"\bPacketMon\b",
    r"\bCapturePacket\b",
    r"\bPacketCapture\b",
    r"\btcpdump\b",
    r"\btshark\b",
    r"\bwindump\b",
    r"\b9002\b",  # the game server port — a socket reference here is capture intent
]

# 3 — client-derived artifacts (binaries, metadata, dump outputs, dumper names)
RE_ARTIFACT = [
    r"GameAssembly",
    r"global-metadata",
    r"il2cpp_data",
    r"Metadata\.dat",
    r"dump\.cs",
    r"DummyDll",
    r"Cpp2IL",
    r"il2cpp",  # covers Il2CppDumper / il2cpp-dumper / il2cppdumper / IL2CPP_DIR
]

# 4 — wire-protocol knowledge
WIRE_KNOWLEDGE = [
    r"\bLEB128\b",
    r"\bReadLeb128\b",
    r"\bWriteLeb128\b",
    r"\bGorgonClient\b",
    r"\bGorgonProtocolUtils\b",
    r"\bServerCommand\b",
    r"\bClientCommand\b",
    r"\bReadBytesWithDebugPadding\b",
]


def _candidate_files() -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for pattern in ALL_GLOBS:
        for path in ROOT.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel in SELF_EXCLUDED or any(rel.endswith(s) for s in SELF_EXCLUDED):
                continue
            if any(part in DIR_EXCLUSIONS for part in path.parts):
                continue
            if path in seen:
                continue
            seen.add(path)
            out.append(path)
    return sorted(out)


def _scan(paths: list[Path], category: str, patterns: list[str], *, code_only: bool) -> list[str]:
    violations: list[str] = []
    regexes = [re.compile(p, re.IGNORECASE) for p in patterns]
    for path in paths:
        rel = path.relative_to(ROOT).as_posix()
        if code_only and not rel.startswith(("src/", "scripts/", "tests/")):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for regex in regexes:
            m = regex.search(text)
            if m:
                violations.append(f"{category}: {rel}: {regex.pattern}")
    return violations


def main() -> int:
    if os.environ.get(SKIP_ENV):
        print(f"WARNING: {SKIP_ENV}=1 — compliance scan SKIPPED (never do this silently)")
        return 0

    paths = _candidate_files()
    violations: list[str] = []
    violations += _scan(paths, "FORBIDDEN-API", FORBIDDEN_API, code_only=True)
    violations += _scan(paths, "PACKET-TOOLING", PACKET_TOOLING, code_only=True)
    violations += _scan(paths, "RE-ARTIFACT", RE_ARTIFACT, code_only=False)
    violations += _scan(paths, "WIRE-KNOWLEDGE", WIRE_KNOWLEDGE, code_only=False)

    if violations:
        print(f"COMPLIANCE VIOLATIONS ({len(violations)}):")
        for v in violations:
            print(" -", v)
        print("\nPolicy: docs/research/pg-rag-game-interaction-limits.md")
        print("No carve-outs. Remove the violation; do not weaken the scanner.")
        return 1

    print(f"compliance check: OK ({len(paths)} files scanned, 4 categories clean)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
