"""Offline guard for scripts.golden_check.main()'s xfail gate (the `mise golden`
path has no other test). Contract: an xfail-marked golden that still misses is a
known gap (exit 0, `[KNOWN-GAP]`);once it passes, exit 1 with
`[XPASS]` to force unflagging;a real (non-xfail) FAIL stays exit 1."""

import json
import sys

import scripts.golden_check as gc


def _run_main(monkeypatch, tmp_path, files):
    """Run main() seated in a tmp GOLDEN_DIR of canned checks.

    files: {filename: (misses, xfail_flag)}"""
    gc_dir = tmp_path / "golden"
    gc_dir.mkdir(parents=True, exist_ok=True)
    for name, (_misses, xfail) in files.items():
        golden = {
            "id": name.rsplit(".", 1)[0],
            "question": "q",
            "type": "general",
            "facts": [["probe-fact"]],
        }
        if xfail:
            golden["xfail"] = True
        (gc_dir / name).write_text(json.dumps(golden, indent=2), encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["golden_check"])
    monkeypatch.setattr(gc, "GOLDEN_DIR", gc_dir)
    monkeypatch.setattr(gc, "TRACE_DIR", tmp_path / "traces")

    def fake_check(golden, trace=None):
        name = f"{golden['id']}.json"
        misses, _ = files[name]
        return (
            {"answer": "none", "query_type": "general", "documents": []},
            misses,
            "",
        )

    monkeypatch.setattr(gc, "check_golden", fake_check)

    try:
        gc.main()
    except SystemExit as ei:
        return ei.code
    return 0


def test_xfail_gap_persists_main_exits_zero(tmp_path, monkeypatch, capsys):
    code = _run_main(monkeypatch, tmp_path, {"gap.json": ([(["probe-fact"], "probe-fact")], True)})
    assert code == 0
    assert "[KNOWN-GAP]" in capsys.readouterr().out


def test_xfail_gap_closed_main_exits_one(tmp_path, monkeypatch, capsys):
    code = _run_main(monkeypatch, tmp_path, {"gap.json": ([], True)})
    assert code == 1
    assert "[XPASS]" in capsys.readouterr().out


def test_real_fail_main_exits_one(tmp_path, monkeypatch, capsys):
    code = _run_main(
        monkeypatch, tmp_path, {"regress.json": ([(["probe-fact"], "probe-fact")], False)}
    )
    assert code == 1
    assert "[FAIL]" in capsys.readouterr().out


# ---------- --type / --max-facts filter gate (offline) ----------


def test_main_type_filter_runs_only_matching(tmp_path, monkeypatch, capsys):
    """Contract: --type skips non-matching goldens before check_golden (no LLM
    call for filtered-out cases) and runs the rest normally."""
    called = []
    gc_dir = tmp_path / "golden"
    gc_dir.mkdir()
    for i, name in enumerate(("one.json", "two.json")):
        golden = {"id": name[:-5], "question": "q", "type": "recipe" if i == 0 else "general", "facts": [["probe-fact"]]}
        (gc_dir / name).write_text(json.dumps(golden), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["golden_check", "--type", "recipe"])
    monkeypatch.setattr(gc, "GOLDEN_DIR", gc_dir)
    monkeypatch.setattr(gc, "TRACE_DIR", tmp_path / "traces")

    def fake_check(golden, trace=None):
        called.append(golden["id"])
        return ({"answer": "none", "query_type": "general", "documents": []}, [], "")

    monkeypatch.setattr(gc, "check_golden", fake_check)
    gc.main()
    assert called == ["one"]
    assert "OK: all golden facts present" in capsys.readouterr().out


def test_main_empty_match_exits_zero_with_message(tmp_path, monkeypatch, capsys):
    """Contract: filters excluding every golden print `no goldens match`, exit
    0, and never invoke check_golden (no misleading OK line)."""
    gc_dir = tmp_path / "golden"
    gc_dir.mkdir()
    golden = {"id": "one", "question": "q", "type": "general", "facts": [["probe-fact"]]}
    (gc_dir / "one.json").write_text(json.dumps(golden), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["golden_check", "--type", "comparison"])
    monkeypatch.setattr(gc, "GOLDEN_DIR", gc_dir)
    monkeypatch.setattr(gc, "TRACE_DIR", tmp_path / "traces")

    def fake_check(golden, trace=None):
        raise AssertionError("check_golden must not run for filtered-out set")

    monkeypatch.setattr(gc, "check_golden", fake_check)
    gc.main()
    out = capsys.readouterr().out
    assert "no goldens match --type comparison" in out
    assert "OK: all golden facts present" not in out
