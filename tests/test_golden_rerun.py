"""Offline tests for scripts/golden_rerun.py — the golden quick-rerun +
flaky-focus logic: history ledger I/O (append, truncated-tail tolerance),
flaky selection (recent-window ratio + optional manual FLAKY.txt), and
run_cases retry/attempt accounting (check_golden monkeypatched — no servers,
mirroring test_golden_xfail_gate.py's canned-check pattern).

L6 contract addition: data/golden/history.jsonl is an accumulating ledger,
NOT a build output — tests must never write the real one (tmp paths only).
A record is {id, missing, missing_facts, query_type, attempt_n, ts}; the
flaky predicate = (miss-share over the last _RECENT_WINDOW records) > 0.5.
"""

import json
import sys

sys.path.insert(0, "scripts")

import golden_rerun as gr


def _rec(gid, missing, ts, attempt=1):
    return {
        "id": gid,
        "missing": missing,
        "missing_facts": ["probe-fact"] if missing else [],
        "query_type": "entity",
        "attempt_n": attempt,
        "ts": ts,
    }


# ---------- history I/O ----------


def test_record_history_appends_jsonl(tmp_path):
    hist = tmp_path / "history.jsonl"
    gr.record_history(
        {
            "id": "a",
            "missing": True,
            "missing_facts": ["x"],
            "query_type": "entity",
            "attempt_n": 2,
        },
        path=hist,
    )
    gr.record_history(
        {"id": "b", "missing": False, "missing_facts": [], "query_type": "general", "attempt_n": 1},
        path=hist,
    )
    lines = hist.read_text(encoding="utf8").strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["id"] == "a" and rec["missing"] is True
    assert "ts" in rec  # timestamp injected for recency ordering


def test_record_history_creates_parent_dir(tmp_path):
    hist = tmp_path / "sub" / "dir" / "history.jsonl"
    gr.record_history(
        {"id": "a", "missing": False, "missing_facts": [], "query_type": "entity", "attempt_n": 1},
        path=hist,
    )
    assert hist.exists()


def test_load_history_reads_records(tmp_path):
    hist = tmp_path / "history.jsonl"
    hist.write_text(
        '{"id": "a", "missing": true, "ts": "2026-01-01T00:00:00+00:00"}\n'
        '{"id": "b", "missing": false, "ts": "2026-01-02T00:00:00+00:00"}\n',
        encoding="utf8",
    )
    recs = gr.load_history(hist)
    assert [r["id"] for r in recs] == ["a", "b"]


def test_load_history_skips_truncated_tail(tmp_path):
    """A torn last line (crash mid-write) is skipped, not fatal."""
    hist = tmp_path / "history.jsonl"
    hist.write_text(
        '{"id": "a", "missing": false, "ts": "t1"}\n{"id": "b", "missi', encoding="utf8"
    )  # torn record
    recs = gr.load_history(hist)
    assert [r["id"] for r in recs] == ["a"]


def test_load_history_absent_file_is_empty_list(tmp_path):
    assert gr.load_history(tmp_path / "absent.jsonl") == []


# ---------- flaky selection ----------


def test_flaky_derived_from_recent_miss_ratio():
    # 3 misses / 4 recent records = 75% > 50% threshold -> derived flaky.
    recs = [_rec("case", True, f"2026-01-0{i}T00:00:00+00:00") for i in range(1, 4)] + [
        _rec("case", False, "2026-01-04T00:00:00+00:00")
    ]
    assert gr.flaky_ids(history=recs) == ["case"]


def test_flaky_ignores_isolated_miss():
    # 1 miss / 10 recent = 10% — LLM-variance territory, NOT flaky.
    recs = [_rec("case", i == 0, f"2026-01-0{i + 1}T00:00:00+00:00") for i in range(10)]
    assert gr.flaky_ids(history=recs) == []


def test_flaky_uses_recent_window_not_full_history():
    # 8 old misses then 10 clean runs: window=10 sees only clean runs.
    old = [_rec("case", True, f"2025-01-{i + 1:02d}T00:00:00+00:00") for i in range(8)]
    recent_clean = [_rec("case", False, f"2026-06-{i + 1:02d}T00:00:00+00:00") for i in range(10)]
    assert gr.flaky_ids(history=old + recent_clean) == []


def test_flaky_recent_window_redeems_recovered_id():
    # Improvement arc: 5 misses then 6 clean. Total 11 > window 10, so the
    # last-10 = 4 old misses + 6 clean = 40% <= 50% - a fixed id stops being
    # flagged even though its all-time record started bad.
    old = [_rec("case", True, f"2025-01-{i + 1:02d}T00:00:00+00:00") for i in range(5)]
    clean = [_rec("case", False, f"2025-02-{i + 1:02d}T00:00:00+00:00") for i in range(6)]
    assert gr.flaky_ids(history=old + clean) == []


def test_freshly_missing_sorts_first():
    # Both flags flaky; the freshest final-miss id leads the list.
    recs = (
        [_rec("older", True, f"2026-01-0{i}T00:00:00+00:00") for i in range(1, 4)]
        + [_rec("older", False, "2026-01-04T00:00:00+00:00")]
        + [_rec("fresher", True, f"2026-02-0{i}T00:00:00+00:00") for i in range(1, 4)]
    )
    assert gr.flaky_ids(history=recs)[0] == "fresher"


def test_manual_flaky_file_only_when_explicit(tmp_path):
    flaky = tmp_path / "FLAKY.txt"
    flaky.write_text("# comment\nmanual-case\n\n", encoding="utf8")
    # No history at all: without explicit_manual the manual list is ignored.
    assert gr.flaky_ids(history=[], manual_file=flaky) == []
    assert gr.flaky_ids(history=[], manual_file=flaky, explicit_manual=True) == ["manual-case"]


def test_manual_and_derived_deduped_derived_first(tmp_path):
    flaky = tmp_path / "FLAKY.txt"
    flaky.write_text("aaa\nzzz\n# note\n", encoding="utf8")
    recs = [_rec("aaa", True, f"2026-01-0{i}T00:00:00+00:00") for i in range(1, 3)]
    out = gr.flaky_ids(history=recs, manual_file=flaky, explicit_manual=True)
    assert out == ["aaa", "zzz"]  # 'aaa' not duplicated; derived first


# ---------- miss_stats ----------


def test_miss_stats_share_and_record_count():
    recs = [_rec("b", True, f"2026-01-0{i}T00:00:00+00:00") for i in range(1, 4)] + [
        _rec("a", i < 11, f"2026-01-0{i}T00:00:00+00:00") for i in range(10, 12)
    ]
    rows = {gid: (share, total) for gid, share, total in gr.miss_stats(recs)}
    assert rows["b"][0] == 1.0  # 3/3 recent misses
    assert rows["a"][0] == 0.5  # 1 of 2 recent misses
    assert rows["b"][1] == 3  # total records kept for context


def test_miss_stats_sorted_worst_first():
    recs = [
        _rec("b", True, "2026-01-01T00:00:00+00:00"),
        _rec("a", True, "2026-01-02T00:00:00+00:00"),
        _rec("b", True, "2026-01-03T00:00:00+00:00"),
        _rec("a", False, "2026-01-04T00:00:00+00:00"),
    ]
    ids = [gid for gid, _share, _n in gr.miss_stats(recs)]
    assert ids[0] == "b"  # share 1.0 beats 0.5


# ---------- run_cases (check_golden monkeypatched, no servers) ----------


class _FakeCheck:
    def __init__(self, sequence):
        self.sequence = sequence
        self.calls = 0

    def __call__(self, golden, trace=None):
        self.calls += 1
        if callable(self.sequence):
            return (
                {"answer": "", "query_type": "entity", "documents": []},
                self.sequence(golden),
                "",
            )
        return ({"answer": "", "query_type": "entity", "documents": []}, self.sequence, "")


def _persist_to(tmp_path, monkeypatch):
    """Point History persistence at a tmp ledger; keeps run_cases honest."""
    hist = tmp_path / "history.jsonl"
    monkeypatch.setattr(gr, "HISTORY_FILE", hist)
    monkeypatch.setattr(
        gr, "record_history", lambda outcome, path=None: gr.record_history(outcome, path=hist)
    )
    return hist


def test_run_cases_persists_attempt_count(tmp_path, monkeypatch):
    hist = tmp_path / "history.jsonl"
    monkeypatch.setattr(gr, "check_golden", _FakeCheck([(["probe-fact"], "probe-fact")]))
    golden = {"id": "c", "question": "q", "type": "entity", "facts": [["probe-fact"]]}
    code = gr.run_cases(
        [("c.json", golden)], max_attempts=2, diagnose=False, trace=False, history_path=hist
    )
    assert code == 1
    rec = json.loads(hist.read_text(encoding="utf8").strip())
    assert rec["attempt_n"] == 2 and rec["missing"] is True


def test_run_cases_pass_breaks_retry_loop(tmp_path, monkeypatch):
    # First attempt PASSES -> attempt_n == 1 persisted, no blown retry budget.
    hist = tmp_path / "history.jsonl"
    fake = _FakeCheck([])  # no misses
    monkeypatch.setattr(gr, "check_golden", fake)
    golden = {"id": "c", "question": "q", "type": "entity", "facts": [["x"]]}
    code = gr.run_cases(
        [("c.json", golden)], max_attempts=3, diagnose=False, trace=False, history_path=hist
    )
    assert code == 0
    assert fake.calls == 1
    rec = json.loads(hist.read_text(encoding="utf8").strip())
    assert rec["attempt_n"] == 1


def test_run_cases_xfail_miss_is_not_a_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gr, "check_golden", _FakeCheck([(["probe-fact"], "probe-fact")]))
    golden = {
        "id": "known-gap",
        "question": "q",
        "type": "general",
        "facts": [["probe-fact"]],
        "xfail": True,
    }
    code = gr.run_cases(
        [("known-gap.json", golden)],
        max_attempts=1,
        diagnose=False,
        trace=False,
        history_path=tmp_path / "history.jsonl",
    )
    assert code == 0  # known gap persists -> not a failure
    assert "[KNOWN-GAP]" in capsys.readouterr().out


def test_run_cases_all_attempts_error_is_a_failure(tmp_path, monkeypatch, capsys):
    """Contract: if check_golden raises on EVERY attempt (e.g. a service is
    down), the case counts as a miss — never a false PASS. Regression guard
    for the embed-down run that reported false PASSes (2026-10-02)."""
    monkeypatch.setattr(gr, "HISTORY_FILE", tmp_path / "history.jsonl")

    def _raise(golden, trace=None):
        raise ConnectionError("Cannot connect to embedding server")

    monkeypatch.setattr(gr, "check_golden", _raise)
    golden = {"id": "c", "question": "q", "type": "general", "facts": [["x"]]}
    hist = tmp_path / "history.jsonl"
    code = gr.run_cases(
        [(tmp_path / "c.json", golden)],
        max_attempts=2,
        diagnose=False,
        trace=False,
        history_path=hist,
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "[FAIL] c (harness error every attempt" in out
    rec = json.loads(hist.read_text(encoding="utf8").strip())
    assert rec["missing"] is True
    assert rec["attempt_n"] == 2


def test_run_cases_xpass_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "check_golden", _FakeCheck([]))
    golden = {
        "id": "known-gap",
        "question": "q",
        "type": "general",
        "facts": [["probe-fact"]],
        "xfail": True,
    }
    assert (
        gr.run_cases(
            [("known-gap.json", golden)],
            max_attempts=1,
            diagnose=False,
            trace=False,
            history_path=tmp_path / "history.jsonl",
        )
        == 1
    )


# ---------- resolve_ids --type / --max-facts filters (offline) ----------


def _seed_golden_dir(tmp_path, monkeypatch):
    """4 canned goldens covering two types x two fact counts."""
    gdir = tmp_path / "golden"
    gdir.mkdir()
    specs = [
        ("recipe-two.json", {"id": "recipe-two", "type": "recipe", "facts": [["a"], ["b"]]}),
        ("recipe-five.json", {"id": "recipe-five", "type": "recipe", "facts": [["a"]] * 5}),
        ("entity-two.json", {"id": "entity-two", "type": "entity", "facts": [["a"], ["b"]]}),
        ("entity-five.json", {"id": "entity-five", "type": "entity", "facts": [["a"]] * 5}),
    ]
    for name, g in specs:
        (gdir / name).write_text(json.dumps(g), encoding="utf8")
    monkeypatch.setattr(gr, "GOLDEN_DIR", gdir)
    return gdir


def test_resolve_ids_type_filter(tmp_path, monkeypatch):
    _seed_golden_dir(tmp_path, monkeypatch)
    cases = gr.resolve_ids(_Args(type="recipe", max_facts=None, selection="all"))
    assert [p.stem for p, _ in cases] == ["recipe-five", "recipe-two"]


def test_resolve_ids_max_facts_filter(tmp_path, monkeypatch):
    _seed_golden_dir(tmp_path, monkeypatch)
    cases = gr.resolve_ids(_Args(type=None, max_facts=2, selection="all"))
    assert [p.stem for p, _ in cases] == ["entity-two", "recipe-two"]


def test_resolve_ids_filters_intersect(tmp_path, monkeypatch):
    _seed_golden_dir(tmp_path, monkeypatch)
    cases = gr.resolve_ids(_Args(type="entity", max_facts=2, selection="all"))
    assert [p.stem for p, _ in cases] == ["entity-two"]


def test_resolve_ids_type_excludes_explicit_ids(tmp_path, monkeypatch, capsys):
    _seed_golden_dir(tmp_path, monkeypatch)
    cases = gr.resolve_ids(_Args(type="recipe", max_facts=None, ids="entity-two"))
    assert cases == []
    assert "no goldens match --type recipe" in capsys.readouterr().out


def test_resolve_ids_all_branch_empty_match_prints(tmp_path, monkeypatch, capsys):
    _seed_golden_dir(tmp_path, monkeypatch)
    cases = gr.resolve_ids(_Args(type="general", max_facts=None, selection="all"))
    assert cases == []
    assert "no goldens match --type general" in capsys.readouterr().out


class _Args:
    """Namespace stand-in with the fields resolve_ids reads."""

    def __init__(self, type=None, max_facts=None, ids=None, selection=None):
        self.type = type
        self.max_facts = max_facts
        self.ids = ids
        self.selection = selection
