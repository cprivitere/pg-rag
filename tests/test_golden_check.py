"""Golden fact-presence gate — every case runs through the agentic tool loop.

Parametrized over the golden JSON files in ``data/golden/``. Each case is
answered by ``pgrag.agentic.loop.run_loop`` (SQLite store + FTS
``corpus_search``), so the gate needs the LLM server (:8080) and nothing else
— there is no embedding or reranker service to be up.

Tiers come from ``pgrag.agentic.eval`` (the single home for tier membership,
normalization and the reasoning-noise retry) so the pytest gate, the CLI
harness (``scripts/golden_check.py``) and the tuning sweeps cannot drift:

- ``-m short``: 8 representative cases.
- ``-m long``: every remaining case, including the store-only play-history
  cases — they are ordinary cases now that the loop is the only path.
- Both tiers are also marked ``slow``, so the default offline run (``addopts =
  -m "not slow"``) deselects them: each case is a multi-round reasoning
  generation against a live LLM (minutes for the hard ones). Select them
  explicitly with ``-m short`` / ``-m long``.
"""

import pytest

from pgrag.agentic import eval as agentic_eval

_ALL_IDS = agentic_eval.golden_ids()
_SHORT_IDS = agentic_eval.case_ids("short")
_LONG_IDS = [i for i in _ALL_IDS if i not in set(_SHORT_IDS)]

normalize = agentic_eval.normalize


def test_normalize_contractions_strip_not_space():
    """Contractions normalize to their stripped form so a source "don't"/"don't"
    matches a contraction-stripped golden variant "dont"; other punctuation
    still collapses to a single space."""
    assert normalize("Curses don't wear off on their own.") == "curses dont wear off on their own"
    assert normalize("Curses don\u2019t wear off") == "curses dont wear off"
    assert normalize("curses dont wear off") == "curses dont wear off"
    assert normalize("Blacksmithing: 25") == "blacksmithing 25"


def _llm_up():
    import requests

    try:
        requests.get("http://127.0.0.1:8080/health", timeout=2)
    except Exception:
        return False
    return True


@pytest.fixture()
def require_llm():
    """Runtime skip: the golden gate needs the LLM server (:8080). Checked
    per-test so a server coming up/down mid-run doesn't make the suite depend
    on collection-time state."""
    if not _llm_up():
        pytest.skip("golden gate needs the LLM server (:8080)")


def _check_case(case_id: str):
    """Answer one golden case through the tool loop and assert every fact is
    present. The retry that damps reasoning-ON sampling noise lives in
    ``agentic_eval.run_case``; a real regression still fails both attempts."""
    record = agentic_eval.run_case(agentic_eval.load_case(case_id))
    status = record["status"]
    if status == "ERROR":
        pytest.fail(f"LLM server error on {case_id}: {record.get('error')}")
    if status == "XPASS":
        pytest.fail(
            f"xfail golden {case_id} now passes — gap closed; remove the "
            "'xfail' flag from its golden JSON"
        )
    assert status in ("PASS", "KNOWN-GAP"), f"missing facts: {record['missing']}"


def test_run_case_reports_server_error_as_record(monkeypatch):
    """A server failure during a golden case must surface as an ERROR record
    carrying the message — not as an exception that kills a long batch (the
    loop's HTTP 500 path is what produced the batch-aborting traceback)."""
    from pgrag.rag.llm import LLMServerError

    def boom(*_args, **_kwargs):
        raise LLMServerError("LLM server at http://localhost:8080 returned HTTP 500: boom")

    monkeypatch.setattr(agentic_eval, "run_loop", boom)
    record = agentic_eval.run_case({"id": "probe", "question": "q", "facts": [["a"]]}, retry=False)
    assert record["status"] == "ERROR"
    assert "HTTP 500" in record["error"]
    assert record["missing"] == []


@pytest.mark.slow
@pytest.mark.short
@pytest.mark.parametrize("case_id", _SHORT_IDS)
def test_golden_facts_short(case_id, require_llm):
    """Short golden tier: 8 representative queries covering all types
    (entity, comparison, recipe, general). Filter: -m short (needs the LLM)."""
    _check_case(case_id)


@pytest.mark.slow
@pytest.mark.long
@pytest.mark.parametrize("case_id", _LONG_IDS)
def test_golden_facts_long(case_id, require_llm):
    """Long golden tier: every remaining case, store-only play-history cases
    included. Filter: -m long (needs the LLM)."""
    _check_case(case_id)
