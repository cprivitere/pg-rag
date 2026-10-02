"""Tuning-harness contracts (L-agentic): case parsing, run-id, compare math.

Fixture-driven; no live LLM. run_sweep_case is exercised via a stubbed
run_loop so the sweep math (PASS/FAIL/ERROR classification, fact counting)
is asserted without server dependencies.
"""

from __future__ import annotations

import json
import sys as _sys
from pathlib import Path

_sys.path.insert(0, "scripts")

import agentic_tune
from agentic_tune import _load_cases, _run_id, compare, run_sweep_case


class _Args:
    temperature = 0.0
    seed = 0
    max_rounds = 6
    store = "data/sqlite_gorgon.db"


def test_load_cases_golden_short(monkeypatch):
    monkeypatch.setattr(agentic_tune, "_short_ids_from_tests", lambda: ["bacon-for-joeh"])
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
