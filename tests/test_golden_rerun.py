"""`scripts/golden_rerun.py` selection filters (`--selection list` / `flaky`).

contract: both filters report only ids that still have a golden file —
`data/golden/history.jsonl` is append-only and `FLAKY.txt` outlives deletions,
so a deleted id must not read (or print) as flaky; history tests use tmp paths
only (TEST_CONTRACTS L6).
"""

import scripts.golden_rerun as golden_rerun


def test_audit_rows_skips_ids_without_a_golden_file(tmp_path, monkeypatch):
    monkeypatch.setattr(golden_rerun, "GOLDEN_DIR", tmp_path)
    (tmp_path / "kept.json").write_text("{}", encoding="utf8")
    history = [
        {"id": "kept", "missing": True, "ts": "2026-01-01T00:00:00+00:00"},
        {"id": "deleted", "missing": True, "ts": "2026-01-02T00:00:00+00:00"},
    ]
    assert [row[0] for row in golden_rerun.audit_rows(history)] == ["kept"]


def test_flaky_ids_skips_ids_without_a_golden_file(tmp_path, monkeypatch):
    monkeypatch.setattr(golden_rerun, "GOLDEN_DIR", tmp_path)
    (tmp_path / "kept.json").write_text("{}", encoding="utf8")
    manual = tmp_path / "FLAKY.txt"
    manual.write_text("deleted\n", encoding="utf8")
    history = [
        {"id": "kept", "missing": True, "ts": "2026-01-01T00:00:00+00:00"},
        {"id": "deleted", "missing": True, "ts": "2026-01-02T00:00:00+00:00"},
    ]
    ids = golden_rerun.flaky_ids(history, manual_file=manual, explicit_manual=True)
    assert ids == ["kept"]
