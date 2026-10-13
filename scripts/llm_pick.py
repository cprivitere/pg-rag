#!/usr/bin/env python3
"""LLM model picker — select the default LLM model from tested candidates.

Switches LLM_MODEL / LLM_FLAGS in mise.toml and optionally restarts the LLM
server. Reuses the CANDIDATES registry from llm_golden_bakeoff.py.

Usage:
    uv run python scripts/llm_pick.py           # interactive menu
    uv run python scripts/llm_pick.py ornith    # switch to ornith
    uv run python scripts/llm_pick.py --list     # print available models
    uv run python scripts/llm_pick.py qwen-9b --no-restart  # switch, skip restart
"""

from __future__ import annotations

import argparse
import re
import socket
import sys
import time

# ── shared registry & helpers from golden bakeoff ──────────────────────────
from llm_golden_bakeoff import (
    CANDIDATES,
    HEALTH_URL,
    LLM_PORT,
    _read_mise,
    set_mise,
    start_llm,
    stop_llm,
)

# ── helpers ────────────────────────────────────────────────────────────────


def _server_is_running() -> bool:
    """Return True if port 8080 is accepting connections (LLM server up)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", LLM_PORT))
        s.close()
        return True
    except OSError:
        return False


def _get_current_model() -> str:
    """Read the current LLM_MODEL value from mise.toml."""
    text = _read_mise()
    m = re.search(r'^LLM_MODEL = "(.*)"$', text, re.MULTILINE)
    return m.group(1) if m else ""


def _format_name(key: str) -> str:
    """One-line display:  key — model (note)."""
    c = CANDIDATES[key]
    return f"{key} — {c['model']} ({c['note']})"


def _find_key(prefix: str) -> str | None:
    """Case-insensitive exact then prefix match against candidate keys."""
    lower = prefix.lower()
    # exact
    for k in CANDIDATES:
        if k.lower() == lower:
            return k
    # unique prefix
    matches = [k for k in CANDIDATES if k.lower().startswith(lower)]
    if len(matches) == 1:
        return matches[0]
    return None


# ── restart ────────────────────────────────────────────────────────────────


def _restart_llm() -> None:
    import urllib.request

    print("Stopping LLM...", flush=True)
    stop_llm()
    print("Starting LLM...", flush=True)
    start_llm()

    print("Waiting for LLM health", end="", flush=True)
    deadline = time.time() + 240
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=3) as resp:
                if resp.status == 200:
                    print(" OK", flush=True)
                    return
        except OSError:
            pass
        time.sleep(1.5)
        print(".", end="", flush=True)
    print(" TIMEOUT — server may not be ready yet.", flush=True)


# ── interactive mode ───────────────────────────────────────────────────────


def _interactive(no_restart: bool = False) -> None:
    current_model = _get_current_model()
    keys = sorted(CANDIDATES)

    print("\nAvailable LLM models:\n")
    for i, key in enumerate(keys, 1):
        marker = " [CURRENT]" if CANDIDATES[key]["model"] == current_model else ""
        print(f"  {i:2d}. {_format_name(key)}{marker}")
    print()

    while True:
        try:
            choice = input(f"Pick model [1-{len(keys)}/q]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if choice.lower() in ("q", "quit", "exit"):
            return

        try:
            idx = int(choice) - 1
            if 0 <= idx < len(keys):
                break
        except ValueError:
            pass
        print(f"Invalid choice. Enter 1-{len(keys)} or 'q'.")

    key = keys[idx]
    c = CANDIDATES[key]

    if c["model"] == current_model:
        print(f"Already {_format_name(key)}")
        return

    old = current_model or "(unknown)"
    set_mise(c["model"], c["flags"])
    print(f"Switched from {old} to {_format_name(key)}")

    if not no_restart and _server_is_running():
        resp = input("Restart LLM server now? [Y/n]: ").strip().lower()
        if resp in ("", "y", "yes"):
            _restart_llm()


# ── non-interactive mode ───────────────────────────────────────────────────


def _non_interactive(key_str: str, no_restart: bool) -> None:
    matched = _find_key(key_str)
    if matched is None:
        print(f"Unknown model key: '{key_str}'")
        print("Available keys:", ", ".join(sorted(CANDIDATES)))
        sys.exit(1)

    key = matched
    c = CANDIDATES[key]
    current_model = _get_current_model()

    if c["model"] == current_model:
        print(f"Already {key}")
        return

    old = current_model or "(unknown)"
    set_mise(c["model"], c["flags"])
    print(f"Switched from {old} to {key}")

    if not no_restart and _server_is_running():
        _restart_llm()


# ── CLI ────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Pick the default LLM model from tested candidates")
    ap.add_argument(
        "model_key",
        nargs="?",
        help="Candidate key (case-insensitive prefix match). Omit for interactive menu.",
    )
    ap.add_argument(
        "--no-restart",
        action="store_true",
        help="Skip LLM server restart after switching",
    )
    ap.add_argument(
        "--list",
        action="store_true",
        help="Print available models and exit",
    )
    args = ap.parse_args()

    if args.list:
        current_model = _get_current_model()
        print("Available LLM models:\n")
        for key in sorted(CANDIDATES):
            c = CANDIDATES[key]
            marker = " [CURRENT]" if c["model"] == current_model else ""
            print(f"  {key}{marker}")
            print(f"    model: {c['model']}")
            print(f"    flags: {c['flags']}")
            if c["note"]:
                print(f"    note:  {c['note']}")
            print()
        return 0

    if args.model_key:
        _non_interactive(args.model_key, args.no_restart)
    else:
        _interactive(args.no_restart)

    return 0


if __name__ == "__main__":
    sys.exit(main())
