#!/usr/bin/env python3
"""LLM golden bakeoff — run the fact-presence golden eval across multiple
LLM backends on :8080 and compare.

For each candidate: stop the current LLM → swap mise.toml [env] LLM_MODEL /
LLM_FLAGS → start → wait for health → run golden_check.py --diagnose →
capture output. Only the LLM changes; retrieval (the agentic tool loop) is
identical across models, so the golden delta isolates the model.

Reasoning OFF (deterministic): each candidate's documented flags are used but
with `--reasoning off` appended, matching the doc's rigorous re-baseline method
(temp 0 + seed 0 is already wired in golden_check.py GENERATION).

Usage:
    uv run python scripts/llm_golden_bakeoff.py
    uv run python scripts/llm_golden_bakeoff.py --models ornith,gemma-12b
    uv run python scripts/llm_golden_bakeoff.py --dry-run

Restores the original mise.toml LLM_MODEL/LLM_FLAGS at the end (best effort,
also on Ctrl-C). Does NOT touch the chat UIs.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MISE_TOML = ROOT / "mise.toml"
GOLDEN = ROOT / "scripts" / "golden_check.py"
RESULTS_DIR = ROOT / "data"
LLM_PORT = 8080
HEALTH_URL = f"http://localhost:{LLM_PORT}/health"

# Reasoning OFF (deterministic) flags for every candidate. The reasoning-off
# token `--reasoning off` is appended after each model's documented flags.
# Each entry mirrors docs/LLM_MODEL_WIRING.md exactly; `--reasoning off` is the
# modern non-deprecated synonym for the deprecated `enable_thinking: false`.
CANDIDATES = {
    # current production
    "ornith": {
        "model": "ornith-ai/Ornith-1.5-9B-GGUF:Q4_K_M",
        "flags": "--no-mmproj -ngl 999 -fa on -c 65536 -ctk q8_0 -ctv q8_0 --reasoning-budget 4096",
        "note": "current production (dense ~9B, no MTP, qwen-style thinking)",
    },
    "gemma-12b": {
        "model": "unsloth/gemma-4-12B-it-qat-GGUF:UD-Q4_K_XL",
        "flags": "--spec-type draft-mtp --spec-draft-n-max 4 --no-mmproj -ngl 999 -fa on -c 32768 -ctk q8_0 -ctv q8_0 --reasoning-budget 1024",
        "note": "prior winner (48L dense QAT, MTP n4, budget 1024)",
    },
    "qwen-9b": {
        "model": "unsloth/Qwen3.5-9B-MTP-GGUF:UD-Q4_K_XL",
        "flags": "--spec-type draft-mtp --spec-draft-n-max 6 -ngl 999 -fa on -c 65536 -ctk q8_0 -ctv q8_0 --reasoning-budget 4096",
        "note": "runner-up (Mamba2-hybrid, MTP n6 bundled, budget 4096)",
    },
    "gemma-26b-qat": {
        "model": "unsloth/gemma-4-26B-A4B-it-qat-GGUF:UD-Q4_K_XL",
        "flags": "--spec-type draft-mtp --spec-draft-n-max 4 -ngl 999 -fa on -c 65536 -ctk q8_0 -ctv q8_0 --reasoning-budget 4096",
        "note": "30L MoE 3.8B active QAT (MTP n4, needs PG closed)",
    },
    "gemma-26b-q2": {
        "model": "unsloth/gemma-4-26B-A4B-it-GGUF:UD-Q2_K_XL",
        "flags": "-ngl 999 -fa on -c 65536 -ctk q8_0 -ctv q8_0 --reasoning-budget 4096",
        "note": "30L MoE non-QAT Q2 (NO MTP file — no --spec-type)",
    },
    "qwen-27b": {
        "model": "unsloth/Qwen3.8-27B-GGUF:UD-Q2_K_XL",
        "flags": "--spec-type draft-mtp --spec-draft-n-max 6 -ngl 999 -fa on -c 65536 -ctk q8_0 -ctv q8_0 --reasoning-budget 4096",
        "note": "65L dense Q2 (MTP bundled, needs PG closed)",
    },
    "granite-8b": {
        "model": "unsloth/granite-4.1-8b-GGUF:UD-Q4_K_XL",
        "flags": "--jinja -ngl 999 -fa on -c 32768 -ctk q8_0 -ctv q8_0",
        "note": "granite 8B (needs --jinja, no thinking/MTP)",
    },
    "granite-3b": {
        "model": "unsloth/granite-4.1-3b-GGUF:Q8_0",
        "flags": "--jinja -ngl 999 -fa on -c 32768 -ctk q8_0 -ctv q8_0",
        "note": "granite 3B (needs --jinja, no thinking/MTP)",
    },
}

# Reasoning-OFF token appended to every candidate for the deterministic
# re-baseline. Kept as a separate string so a model's own flags stay verbatim
# from the wiring doc (the doc is the source of truth for per-model flags).
REASONING_OFF = "--reasoning off"


def _read_mise() -> str:
    return MISE_TOML.read_text(encoding="utf-8")


def _write_mise(content: str) -> None:
    MISE_TOML.write_text(content, encoding="utf-8")


_MODEL_RE = re.compile(r'^LLM_MODEL = ".*"$', re.MULTILINE)
_FLAGS_RE = re.compile(r'^LLM_FLAGS = ".*"$', re.MULTILINE)


def snapshot_mise() -> tuple[str, str]:
    """Capture the current LLM_MODEL / LLM_FLAGS values for restore."""
    text = _read_mise()
    m_model = _MODEL_RE.search(text)
    m_flags = _FLAGS_RE.search(text)
    if not m_model or not m_flags:
        raise RuntimeError("could not find LLM_MODEL/LLM_FLAGS in mise.toml")
    return m_model.group(0), m_flags.group(0)


def set_mise(model: str, flags: str) -> None:
    text = _read_mise()
    text = _MODEL_RE.sub(f'LLM_MODEL = "{model}"', text)
    text = _FLAGS_RE.sub(f'LLM_FLAGS = "{flags}"', text)
    _write_mise(text)


def stop_llm() -> None:
    subprocess.run(
        ["mise", "llm-stop"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # Make sure the port is actually free (taskkill /T /F can leave a linger).
    _wait_port_free(LLM_PORT, timeout=30)


def start_llm() -> None:
    subprocess.run(
        ["mise", "llm-start"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _wait_port_free(port: int, timeout: float) -> None:
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            s.close()
            time.sleep(0.5)
        except OSError:
            s.close()
            return
    # still bound — warn but proceed
    print(f"  ! port {port} still bound after {timeout:.0f}s", flush=True)


def wait_health(url: str, timeout: float = 240) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(1.5)
    return False


def run_golden() -> tuple[int, str, bool]:
    """Run golden_check.py. Returns (rc, output, timed_out).

    On timeout, kills the child and returns whatever output was captured so
    the sweep records a `timeout` status and continues instead of crashing.
    """
    proc = subprocess.Popen(
        ["uv", "run", "python", str(GOLDEN), "--diagnose"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        out, _ = proc.communicate(timeout=3600)
        return proc.returncode, out or "", False
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        return proc.returncode, (out or "") + "\n[BAKEOFF: golden timed out]", True


def parse_missing(output: str) -> tuple[int, list[str]]:
    """Extract total missing facts + per-case FAIL ids from golden output."""
    miss = 0
    fails: list[str] = []
    for line in output.splitlines():
        if line.startswith("FAIL:"):
            m = re.search(r"(\d+)", line)
            if m:
                miss = int(m.group(1))
        if line.startswith("[FAIL]"):
            m = re.search(r"\[FAIL\]\s+(\S+)", line)
            if m:
                fails.append(m.group(1))
    return miss, fails


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM golden bakeoff across backends.")
    ap.add_argument(
        "--models",
        default=",".join(CANDIDATES),
        help="comma list of candidate keys (default: all)",
    )
    ap.add_argument("--dry-run", action="store_true", help="print plan, run nothing")
    ap.add_argument(
        "--skip-restore",
        action="store_true",
        help="leave the last model set (don't restore mise.toml)",
    )
    args = ap.parse_args()

    keys = [k.strip() for k in args.models.split(",") if k.strip()]
    bad = [k for k in keys if k not in CANDIDATES]
    if bad:
        print("unknown model key(s): " + ", ".join(bad))
        print("available: " + ", ".join(CANDIDATES))
        return 2

    original_model_line, original_flags_line = snapshot_mise()
    print(f"Snapshot mise.toml: {original_model_line} / {original_flags_line}", flush=True)

    if args.dry_run:
        print("\nDRY RUN — plan:")
        for k in keys:
            c = CANDIDATES[k]
            print(f"  [{k}] {c['model']}")
            print(f"        flags: {c['flags']} {REASONING_OFF}")
            print(f"        note: {c['note']}")
        print(f"\nWould restore: {original_model_line}")
        return 0

    ts = _dt.datetime.now(_dt.UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    results: dict[str, dict] = {}
    restored = False
    try:
        for k in keys:
            c = CANDIDATES[k]
            print(f"\n{'=' * 72}", flush=True)
            print(f"== [{k}] {c['model']}", flush=True)
            print(f"== flags: {c['flags']} {REASONING_OFF}", flush=True)
            print(f"== note: {c['note']}", flush=True)
            print(f"{'=' * 72}", flush=True)

            # Swap mise.toml (env source of truth the launcher reads).
            set_mise(c["model"], c["flags"])
            time.sleep(0.5)

            print("  stopping current LLM ...", flush=True)
            stop_llm()
            print("  starting LLM ...", flush=True)
            start_llm()
            print("  waiting for health on :8080 ...", flush=True)
            if not wait_health(HEALTH_URL, timeout=300):
                print("  !! LLM did not become healthy — skipping", flush=True)
                results[k] = {
                    "model": c["model"],
                    "flags": c["flags"],
                    "status": "unhealthy",
                    "missing": None,
                    "fails": [],
                    "output": "",
                }
                continue

            # Note: no explicit warmup — golden's first query is the cold
            # prompt-cache fill. constancy across models is what matters here.
            print("  health OK — running golden ...", flush=True)
            t0 = time.time()
            rc, out, timed_out = run_golden()
            elapsed = time.time() - t0
            miss, fails = parse_missing(out)
            status = "timeout" if timed_out else ("PASS" if rc == 0 else "FAIL")
            print(
                f"  -> {status}: {miss} missing, {len(fails)} FAIL cases, {elapsed:.0f}s",
                flush=True,
            )
            results[k] = {
                "model": c["model"],
                "flags": c["flags"],
                "status": status,
                "missing": miss,
                "fails": fails,
                "elapsed": round(elapsed, 1),
                "output": out,
            }
    except KeyboardInterrupt:
        print("\n!! interrupted — restoring mise.toml", flush=True)
    finally:
        if not args.skip_restore:
            set_mise(*_split_line(original_model_line, original_flags_line))
            # also restart the original LLM so the box is left as found.
            print("\nRestoring mise.toml + restarting original LLM ...", flush=True)
            stop_llm()
            start_llm()
            wait_health(HEALTH_URL, timeout=300)
            restored = True

    # Write results.
    stamp = RESULTS_DIR / f"llm_bakeoff_{ts}.json"
    # Strip the verbose per-model output from the summary but keep it in a
    # separate file for diagnose forensics.
    summary = {
        "timestamp": ts,
        "reasoning": "off (deterministic: --reasoning off, temp 0, seed 0)",
        "golden_cases": sum(1 for _ in (ROOT / "data" / "golden").glob("*.json")),
        "restored": restored,
        "results": {
            k: {kk: vv for kk, vv in v.items() if kk != "output"} for k, v in results.items()
        },
    }
    stamp.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    # full output dump
    full = RESULTS_DIR / f"llm_bakeoff_{ts}_full.json"
    full.write_text(
        json.dumps({"timestamp": ts, "results": results}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"\n{'=' * 72}")
    print("BAKEOFF SUMMARY (reasoning OFF, deterministic)")
    print(f"{'=' * 72}")
    print(f"{'model':<14} {'missing':>8} {'fails':>6} {'time':>7}  status")
    print("-" * 50)
    # Rank by missing facts (lower better); timeouts/unhealthy (missing=None) last.
    ranked = sorted(
        results.items(),
        key=lambda kv: (kv[1].get("missing") is None, kv[1].get("missing") or 999),
    )
    for k, r in ranked:
        miss = r.get("missing")
        miss_s = str(miss) if miss is not None else "N/A"
        nf = len(r.get("fails", []))
        t = r.get("elapsed", 0)
        print(f"{k:<14} {miss_s:>8} {nf:>6} {t:>6.0f}s  {r['status']}")
    print(f"\nsummary  -> {stamp}")
    print(f"full log -> {full}")
    if restored:
        print("mise.toml restored to original LLM_MODEL/LLM_FLAGS.")
    return 0


def _split_line(model_line: str, flags_line: str) -> tuple[str, str]:
    """Recover raw model/flags values from the snapshot lines for set_mise()."""
    m = re.search(r'^LLM_MODEL = "(.*)"$', model_line)
    f = re.search(r'^LLM_FLAGS = "(.*)"$', flags_line)
    if not m or not f:
        raise RuntimeError("could not parse snapshot lines for restore")
    return m.group(1), f.group(1)


if __name__ == "__main__":
    sys.exit(main())
