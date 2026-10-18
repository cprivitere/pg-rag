"""Tuning-harness contracts (L-agentic): case parsing, run-id, compare math,
plus server control — `current`/`ornith` resolving to the live [env] pair, the
direct-spawn restart path (hidden-window launch flags on Windows), and the
restart verdict that keeps a mislabeled run row from ever being written.

Fixture-driven; no live LLM. run_sweep_case is exercised via a stubbed
run_loop so the sweep math (PASS/FAIL/ERROR classification, fact counting)
is asserted without server dependencies, and `_maybe_restart_server` is
exercised with `_server_running`/`_spawn_server`/`subprocess.run` stubbed so no
server is ever spawned.
"""

from __future__ import annotations

import argparse
import json
import sys as _sys
from pathlib import Path

import pytest

_sys.path.insert(0, "scripts")

import agentic_tune
from agentic_tune import _load_cases, _run_id, compare, run_sweep_case


class _Args:
    temperature = 0.0
    seed = 0
    max_rounds = 6
    store = "data/sqlite_gorgon.db"


@pytest.fixture(autouse=True)
def _fresh_served_state(monkeypatch):
    """`_served`/`_disturbed` are module state: a test that moved the server must
    not decide the next test's restart verdict."""
    monkeypatch.setattr(agentic_tune, "_served", None)
    monkeypatch.setattr(agentic_tune, "_disturbed", False)


def test_load_cases_golden_short(monkeypatch):
    monkeypatch.setattr(agentic_tune, "SHORT_TIER_IDS", ["bacon-for-joeh"])
    cases = _load_cases("golden-short", None)
    assert len(cases) == 1
    assert cases[0]["id"] == "bacon-for-joeh"
    assert "facts" in cases[0] and "question" in cases[0]


def test_load_cases_custom_file(tmp_path):
    p = tmp_path / "cases.json"
    p.write_text(json.dumps({"cases": [{"id": "x", "question": "q", "facts": [["a"], ["b"]]}]}))
    cases = _load_cases("cases", str(p))
    assert cases[0]["id"] == "x"


def test_run_id_shape():
    rid = _run_id("cases", "ornith-ai/Ornith-1.5-9B-GGUF:Q4_K_M", 0.2, 1, 1024, 6)
    assert rid.startswith("cases__")
    assert "t0.2_s1_b1024_r6" in rid


def test_run_sweep_case_pass_and_fail(monkeypatch):
    case = {"id": "c1", "question": "q", "facts": [["alpha"], ["42"]]}
    monkeypatch.setattr(agentic_tune, "run_loop", lambda *a, **k: {"answer": "Alpha says 42.", "rounds": 2})
    rec = run_sweep_case(case, _Args())
    assert rec["status"] == "PASS" and rec["missing"] == [] and rec["rounds"] == 2

    monkeypatch.setattr(agentic_tune, "run_loop", lambda *a, **k: {"answer": "nothing useful", "rounds": 5})
    rec = run_sweep_case(case, _Args())
    assert rec["status"] == "FAIL" and set(rec["missing"]) == {"alpha", "42"}


def test_run_sweep_case_server_error(monkeypatch):
    from pgrag.rag.llm import LLMServerError

    case = {"id": "c2", "question": "q", "facts": [["alpha"]]}
    def boom(*a, **k):
        raise LLMServerError("http://localhost:8080/v1/chat/completions unreachable")
    monkeypatch.setattr(agentic_tune, "run_loop", boom)
    rec = run_sweep_case(case, _Args())
    assert rec["status"] == "ERROR" and "8080" in rec["error"]


def test_compare_math(tmp_path, capsys):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps({"run_id": "cases__m__t0_s0_b4096_r6", "temperature": 0, "seed": 0,
                             "reasoning_budget": 4096, "model": "org/M-A:Q4", "passed": 4,
                             "total": 6, "facts_missing": 3, "elapsed_s": 40.2}))
    b.write_text(json.dumps({"run_id": "cases__m__t0.4_s0_b4096_r6", "temperature": 0.4, "seed": 0,
                             "reasoning_budget": 4096, "model": "org/M-A:Q4", "passed": 5,
                             "total": 6, "facts_missing": 1, "elapsed_s": 35.0}))
    assert compare([str(a), str(b)]) == 0
    out = capsys.readouterr().out
    assert "cases__m__t0_s0_b4096_r6" in out and "cases__m__t0.4_s0_b4096_r6" in out
    # temperature 0 row sorts before the 0.4 row within the same eval-set group
    assert out.index("t0_s0") < out.index("t0.4")


def test_parity_pin(tmp_path, monkeypatch):
    """golden-short always runs temp 0/seed 0 regardless of CLI args."""
    import argparse as _ap

    args = _ap.Namespace(eval_set="golden-short", cases=None, temperature=0.3, seed=2,
                         max_rounds=6, store="x.db", model="current", reasoning_budget=4096,
                         force=True, llm_restart=False)
    captured = {}

    def fake_run_sweep_case(case, a):
        captured["t"] = a.temperature
        captured["s"] = a.seed
        return {"id": case["id"], "status": "PASS", "rounds": 1, "missing": [], "answer": "", "elapsed_s": 1.0}

    monkeypatch.setattr(agentic_tune, "run_sweep_case", fake_run_sweep_case)
    monkeypatch.setattr(agentic_tune, "_load_cases", lambda s, p: [{"id": "g", "question": "q", "facts": []}])
    monkeypatch.setattr(Path, "write_text", lambda self, *a, **k: None)
    monkeypatch.setattr(Path, "mkdir", lambda self, *a, **k: None)
    agentic_tune.run_once(args)
    assert captured["t"] == 0 and captured["s"] == 0


def test_candidate_current_is_the_live_env_pair(monkeypatch):
    """CONTRACT: `current` and `ornith` are one row — both re-read the
    LLM_MODEL/LLM_FLAGS pair from the environment and both fail closed
    (SystemExit) when that pair is unset."""
    monkeypatch.setenv("LLM_MODEL", "org/Prod-1.5-9B:Q4_K_M")
    monkeypatch.setenv("LLM_FLAGS", "-ngl 999 --reasoning-budget 4096")
    row = agentic_tune.candidate("current")
    assert row == agentic_tune.candidate("ornith")
    assert row["model"] == "org/Prod-1.5-9B:Q4_K_M"

    monkeypatch.delenv("LLM_FLAGS")
    with pytest.raises(SystemExit):
        agentic_tune.candidate("current")
    with pytest.raises(SystemExit):
        agentic_tune.candidate("ornith")


def test_current_with_non_default_budget_spawns_directly(monkeypatch):
    """CONTRACT: `--model current` resolves to the live [env] pair, so a restart
    for a non-default --reasoning-budget rewrites the budget and takes the
    direct `_spawn_server` path — never `mise run llm-start`, which would
    re-apply [env] (4096) and serve it under a `b2048` run-id."""
    monkeypatch.setenv("LLM_MODEL", "org/Prod-1.5-9B:Q4_K_M")
    monkeypatch.setenv("LLM_FLAGS", "--no-mmproj -ngl 999 --reasoning-budget 4096")
    running = {"now": True}
    ran, spawned = [], []

    def fake_run(argv, **kwargs):
        ran.append(argv)
        running["now"] = False  # llm-stop released the port

    def fake_spawn(model, flags):
        spawned.append((model, flags))
        running["now"] = True  # the new server is serving

    monkeypatch.setattr(agentic_tune, "_server_running", lambda: running["now"])
    monkeypatch.setattr(agentic_tune, "_spawn_server", fake_spawn)
    monkeypatch.setattr(agentic_tune.subprocess, "run", fake_run)
    monkeypatch.setattr(agentic_tune.time, "sleep", lambda _s: None)

    args = argparse.Namespace(model="current", reasoning_budget=2048, llm_restart=True)
    assert agentic_tune._maybe_restart_server(args) is True
    assert spawned == [("org/Prod-1.5-9B:Q4_K_M", "--no-mmproj -ngl 999 --reasoning-budget 2048")]
    assert ran == [["mise", "run", "llm-stop"]]
    assert all("llm-start" not in argv for argv in ran)


def test_failed_restart_aborts_without_recording_a_run(tmp_path, monkeypatch):
    """CONTRACT: a restart that never reaches the requested state (server never
    comes up, or an unknown model key) returns a False verdict, and `main()`
    exits non-zero without entering the sweep — a row must never be persisted
    under a model/budget the server did not serve."""
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_FLAGS", raising=False)
    ran, spawned = [], []
    monkeypatch.setattr(agentic_tune, "_server_running", lambda: False)
    monkeypatch.setattr(agentic_tune, "_spawn_server", lambda m, f: spawned.append((m, f)))
    monkeypatch.setattr(agentic_tune.subprocess, "run", lambda argv, **k: ran.append(argv))
    monkeypatch.setattr(agentic_tune.time, "sleep", lambda _s: None)
    monkeypatch.setattr(agentic_tune, "TUNE_DIR", tmp_path)
    runs = []
    monkeypatch.setattr(agentic_tune, "run_once", lambda a: runs.append(a) or {"passed": 0})

    args = argparse.Namespace(model="gemma-26b-qat", reasoning_budget=2048, llm_restart=True)
    assert agentic_tune._maybe_restart_server(args) is False  # spawned, never came up
    assert len(spawned) == 1
    model, flags = spawned[0]
    assert model == "unsloth/gemma-4-26B-A4B-it-qat-GGUF:UD-Q4_K_XL"
    assert flags.endswith("--reasoning-budget 2048")  # the requested budget, not [env]'s
    assert "--reasoning-budget 4096" not in flags
    # unknown model key = the requested pair was not reached either
    unknown = argparse.Namespace(model="nope", reasoning_budget=4096, llm_restart=True)
    assert agentic_tune._maybe_restart_server(unknown) is False
    assert len(spawned) == 1  # the unknown key never launched anything


    argv = [
        "agentic_tune.py",
        "--model",
        "gemma-26b-qat",
        "--reasoning-budget",
        "2048",
        "--llm-restart",
    ]
    monkeypatch.setattr(_sys, "argv", argv)
    assert agentic_tune.main() == 1
    assert runs == []
    assert list(tmp_path.glob("*.json")) == []


def test_spawn_server_hides_the_console_window(monkeypatch, tmp_path):
    """CONTRACT: `_spawn_server` launches `llama-server` with the same hidden-window
    flags as `scripts.services.start` — `creationflags=_CREATE_FLAGS` plus a hidden
    `STARTUPINFO` on Windows background (`None` on POSIX) — so a tune `--llm-restart`
    never pops a console window."""
    import os
    import subprocess

    import scripts.services as services_mod

    spawns = []

    def fake_popen(argv, **kwargs):
        spawns.append({"argv": list(argv), **kwargs})

        class _Proc:
            pid = 4242

        return _Proc()

    monkeypatch.setattr(agentic_tune.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(services_mod, "LOG_DIR", tmp_path)
    agentic_tune._spawn_server("m", "--flag 1")

    assert len(spawns) == 1
    spawn = spawns[0]
    assert spawn["argv"] == services_mod.llm_argv("m", "--flag 1")
    assert spawn["creationflags"] == services_mod._CREATE_FLAGS
    if os.name == "nt":
        # DETACHED_PROCESS must stay out (same console-inheritance reason as
        # `scripts.services.start`: the tune spawn is the same `llama-server`
        # binary, relaunched directly by `--llm-restart`).
        assert not spawn["creationflags"] & subprocess.DETACHED_PROCESS
        si = spawn["startupinfo"]
        assert si.dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert si.wShowWindow == subprocess.SW_HIDE
    else:
        assert spawn.get("startupinfo") is None


def test_reasoning_sweep_restores_the_production_pair(monkeypatch):
    """CONTRACT: a --sweep-reasoning run ends by re-asserting the production pair
    through the same guard, so the harness never leaves a swept model/budget
    serving. The guard is what decides — the restore passes no `force`, because
    the pair the sweep left serving is the reason it restarts."""
    monkeypatch.setenv("LLM_MODEL", "org/Prod-1.5-9B:Q4_K_M")
    monkeypatch.setenv("LLM_FLAGS", "-ngl 999 --reasoning-budget 4096")
    calls = []

    def fake_restart(args, force=False):
        calls.append((args.model, args.reasoning_budget, force))
        return True

    monkeypatch.setattr(agentic_tune, "_maybe_restart_server", fake_restart)
    monkeypatch.setattr(agentic_tune, "run_once", lambda a: {"passed": 6})
    monkeypatch.setattr(
        _sys,
        "argv",
        [
            "agentic_tune.py",
            "--model",
            "current",
            "--sweep-reasoning",
            "2048,256",
            "--llm-restart",
        ],
    )
    assert agentic_tune.main() == 0
    assert calls == [("current", 2048, False), ("current", 256, False), ("current", 4096, False)]


def test_an_ascending_budget_sweep_re_asserts_the_default(monkeypatch):
    """CONTRACT: the restart verdict is the pair the harness last put on :8080,
    not the default sentinel. `--sweep-reasoning 2048,4096` must come back to the
    production budget for the second row instead of measuring the leftover 2048
    server under a `b4096` run-id — the row would look like the 4096 setting's
    result and quietly misdirect the tuning. A sentinel-keyed guard cannot see
    it, because that row's `args` describe the default pair.

    Also pins the restore: after 4096 is serving again it is a no-op (no third
    spawn), so a sweep costs no needless restart."""
    monkeypatch.setenv("LLM_MODEL", "org/Prod-1.5-9B:Q4_K_M")
    monkeypatch.setenv("LLM_FLAGS", "--no-mmproj -ngl 999 --reasoning-budget 4096")
    running = {"now": True}
    spawned, ran = [], []

    def fake_spawn(model, flags):
        spawned.append(flags)
        running["now"] = True

    def fake_run(argv, **kwargs):
        ran.append(argv)
        running["now"] = False  # llm-stop released the port

    monkeypatch.setattr(agentic_tune, "_server_running", lambda: running["now"])
    monkeypatch.setattr(agentic_tune, "_spawn_server", fake_spawn)
    monkeypatch.setattr(agentic_tune.subprocess, "run", fake_run)
    monkeypatch.setattr(agentic_tune.time, "sleep", lambda _s: None)
    monkeypatch.setattr(agentic_tune, "run_once", lambda a: {"passed": 6})
    monkeypatch.setattr(
        _sys,
        "argv",
        [
            "agentic_tune.py",
            "--model",
            "current",
            "--sweep-reasoning",
            "2048,4096",
            "--llm-restart",
        ],
    )

    assert agentic_tune.main() == 0
    # one move per non-productive step, and none for the restore
    assert [flags.split()[-1] for flags in spawned] == ["2048", "4096"]


def test_server_probe_builds_its_url_from_the_shared_port(monkeypatch):
    """CONTRACT: the health probe's URL comes from `services.LLM_PORT` — the same
    knob `llm_argv` bakes into the launch argv — so a port change cannot leave
    the probe pointing at the old one."""
    import requests

    from scripts.services import LLM_PORT

    seen = {}

    def fake_get(url, timeout=None):
        seen["url"] = url
        return type("_R", (), {"ok": True})()

    monkeypatch.setattr(requests, "get", fake_get)
    assert agentic_tune._server_running() is True
    assert seen["url"] == f"http://localhost:{LLM_PORT}/v1/models"


def test_a_failed_move_restores_the_production_pair_and_still_aborts(tmp_path, monkeypatch):
    """CONTRACT: when the sweep's own move fails (the candidate never binds
    :8080), `main()` exits 1 *and* undoes the move — the old server was already
    stopped, so returning without a restart would strand the operator with no
    server at all.

    Pins both halves of the abort: the verdict must not be the restore's rc (a
    successful restore cannot turn an aborted sweep into `0`), and the restore
    must not be a no-op just because `_served` is still None after the failure.
    """
    monkeypatch.setenv("LLM_MODEL", "org/Prod-1.5-9B:Q4_K_M")
    monkeypatch.setenv("LLM_FLAGS", "--no-mmproj -ngl 999 --reasoning-budget 4096")
    running = {"now": True}
    spawned, ran = [], []

    def fake_spawn(model, flags):
        spawned.append((model, flags))
        running["now"] = "Prod" in model  # the candidate never binds, the restore does

    def fake_run(argv, **kwargs):
        ran.append(argv)
        running["now"] = False  # llm-stop released the port

    monkeypatch.setattr(agentic_tune, "_server_running", lambda: bool(running["now"]))
    monkeypatch.setattr(agentic_tune, "_spawn_server", fake_spawn)
    monkeypatch.setattr(agentic_tune.subprocess, "run", fake_run)
    monkeypatch.setattr(agentic_tune.time, "sleep", lambda _s: None)
    monkeypatch.setattr(agentic_tune, "TUNE_DIR", tmp_path)
    runs = []
    monkeypatch.setattr(agentic_tune, "run_once", lambda a: runs.append(a) or {"passed": 0})
    monkeypatch.setattr(
        _sys,
        "argv",
        [
            "agentic_tune.py",
            "--sweep-temperature",
            "0,0.2",
            "--model",
            "gemma-26b-qat",
            "--llm-restart",
        ],
    )

    assert agentic_tune.main() == 1  # the abort is the verdict, not the restore's rc
    assert runs == [] and list(tmp_path.glob("*.json")) == []
    assert [m for m, _ in spawned][-1] == "org/Prod-1.5-9B:Q4_K_M"  # production is back
    assert len(spawned) == 2
    assert ran == [["mise", "run", "llm-stop"]]  # the failed candidate was already gone


def test_a_labeled_run_without_llm_restart_is_refused(tmp_path, monkeypatch):
    """CONTRACT: a run-id records the model/budget the server was asked to serve,
    so `--sweep-reasoning`, a non-production `--model`, or a non-default
    `--reasoning-budget` without `--llm-restart` refuses (exit 2) before anything
    is measured: the rows would otherwise be labeled with a pair nothing served
    (a `--sweep-reasoning 2048,4096` run would measure the production 4096 server
    under a `b2048` id and misdirect the tuning).
    """
    monkeypatch.setattr(agentic_tune, "TUNE_DIR", tmp_path)
    runs, spawned = [], []
    monkeypatch.setattr(agentic_tune, "run_once", lambda a: runs.append(a) or {"passed": 0})
    monkeypatch.setattr(agentic_tune, "_spawn_server", lambda m, f: spawned.append((m, f)))

    for argv in (
        ["agentic_tune.py", "--sweep-reasoning", "2048,4096"],
        ["agentic_tune.py", "--model", "gemma-26b-qat"],
        ["agentic_tune.py", "--reasoning-budget", "2048"],
        ["agentic_tune.py", "--sweep-temperature", "0,0.2", "--model", "qwen-27b"],
    ):
        monkeypatch.setattr(_sys, "argv", argv)
        assert agentic_tune.main() == 2, argv
    assert runs == [] and spawned == []
    assert list(tmp_path.glob("*.json")) == []

    # the production pair at the default budget is the one thing that needs no
    # restart (its rows are labeled with what the operator's server already serves)
    monkeypatch.setattr(agentic_tune, "_maybe_restart_server", lambda a, force=False: True)
    monkeypatch.setattr(_sys, "argv", ["agentic_tune.py", "--temperature", "0.2"])
    assert agentic_tune.main() == 0


class _RunArgs(_Args):
    """The attribute set `run_once` reads for a single (non-sweep) run."""

    eval_set = "cases"
    cases = None
    model = "current"
    reasoning_budget = 4096
    force = True


def _one_run_report(cases_path, **overrides):
    args = _RunArgs()
    args.cases = str(cases_path)
    for key, value in overrides.items():
        setattr(args, key, value)
    return agentic_tune.run_once(args)


def test_a_default_budget_is_labelled_with_what_env_serves(tmp_path, monkeypatch):
    """CONTRACT: a *default* `--reasoning-budget` does not rewrite the live flags,
    so with `[env]` carrying a different budget the server serves that one — and the
    run-id plus the report must say so.

    Labelling the row `b4096` (the sentinel that stood in for the pair) while the
    server served 8192 is exactly the mislabelling the `--llm-restart` verdict
    exists to prevent, and it survived a swap of `[env]`'s flags — the file the repo
    documents as the one place to change them.
    """
    monkeypatch.setenv("LLM_MODEL", "org/Prod-1.5-9B:Q4_K_M")
    monkeypatch.setenv("LLM_FLAGS", "--no-mmproj -ngl 999 --reasoning-budget 8192")
    monkeypatch.setattr(agentic_tune, "TUNE_DIR", tmp_path)
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps([{"id": "c1", "question": "q", "facts": [["a"]]}]), encoding="utf-8"
    )
    monkeypatch.setattr(
        agentic_tune,
        "run_sweep_case",
        lambda case, args: {
            "id": case["id"],
            "status": "PASS",
            "rounds": 1,
            "missing": [],
            "elapsed_s": 0.0,
        },
    )

    production = _one_run_report(cases)
    assert production["reasoning_budget"] == 8192  # what [env] serves, not the sentinel
    assert "_b8192_" in production["run_id"]

    # An explicit budget is the requested one: it *is* written into the flags.
    explicit = _one_run_report(cases, reasoning_budget=2048)
    assert explicit["reasoning_budget"] == 2048
    assert "_b2048_" in explicit["run_id"]

    # No live [env] pair: the requested budget is all the row can honestly carry.
    monkeypatch.delenv("LLM_MODEL")
    monkeypatch.delenv("LLM_FLAGS")
    fallback = _one_run_report(cases, eval_set="cases")
    assert fallback["reasoning_budget"] == 4096
