"""Tests scripts.golden_check.check_golden against the golden JSON files in
GOLDEN_DIR; requires the LLM (:8080) and embedding (:8081) servers and retries
sampled LLM answers to damp nondeterminism.

Tiered by marker: -m short (8 representative queries, ~3-5 min) or
-m long (remaining 39, ~11-23 min). Default (no -m) runs all 47 pipeline
files — store-only cases ("store": true in their JSON, glogger play-history
facts) are excluded here; they run in the agentic eval (`mise agentic-eval`,
12 cases incl. 8 short-tier ids)."""

import json

import pytest

from scripts.golden_check import GOLDEN_DIR, check_golden, normalize

_ALL_FILES = sorted(GOLDEN_DIR.glob("*.json"))

_SHORT_FILES = [
    GOLDEN_DIR / "bacon-for-joeh.json",  # entity: simplest, 3 facts
    GOLDEN_DIR / "fireball-ability.json",  # entity: variant normalization
    GOLDEN_DIR / "moonstone-item.json",  # entity: 2 facts, quick
    GOLDEN_DIR / "blacksmithing-leveling-25-30.json",  # entity: XP ranges
    GOLDEN_DIR / "fireball-vs-fire-breath-damage.json",  # comparison: damage numbers
    GOLDEN_DIR / "healing-potion-omega.json",  # recipe: crafting/effects
    GOLDEN_DIR / "cheesemaking-leveling.json",  # entity: arithmetic, 8 facts, hardest
    GOLDEN_DIR / "grow-field-mushrooms.json",  # general: 5 facts, medium
    # NOTE: store-only glogger cases ("store": true in their JSON) are NOT in
    # the pipeline tiers — they run in the agentic eval (`mise agentic-eval`),
    # which appends them to the short tier automatically.
]

_LONG_FILES = [
    f
    for f in sorted(GOLDEN_DIR.glob("*.json"))
    if f not in _SHORT_FILES and not json.loads(f.read_text(encoding="utf-8")).get("store")
]
# Store-only cases (top-level "store": true): facts live in the agentic SQLite
# store (glogger play history), not in the RAG corpus — the pipeline harness
# can never produce them. They run in the agentic eval (`mise agentic-eval`).


def test_normalize_contractions_strip_not_space():
    """Contractions normalize to their stripped form so a source "don't"/"don't"
    matches a contraction-stripped golden variant "dont"; other punctuation
    still collapses to a single space."""
    assert normalize("Curses don't wear off on their own.") == "curses dont wear off on their own"
    assert normalize("Curses don\u2019t wear off") == "curses dont wear off"
    assert normalize("curses dont wear off") == "curses dont wear off"
    assert normalize("Blacksmithing: 25") == "blacksmithing 25"


def _servers_up():
    import requests

    for port in (8080, 8081):
        try:
            requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
        except Exception:
            return False
    return True


@pytest.fixture()
def require_servers():
    """Runtime skip: golden checks need the LLM (:8080) + embedding (:8081)
    servers. Checked per-test so a server coming up/down mid-run doesn't make
    the suite depend on collection-time state."""
    if not _servers_up():
        pytest.skip("V38: golden check needs LLM (:8080) + embedding (:8081) servers")


def _check_golden_file(path):
    """Run check_golden against one golden file with retry logic and xfail
    handling. Shared by both short and long tier tests."""
    golden = json.loads(path.read_text(encoding="utf-8"))
    # LLM answers are sampled: retry once to damp nondeterminism. A real
    # regression still fails both attempts.
    for _attempt in range(2):
        _, misses, _ = check_golden(golden)
        if not misses:
            break
    if golden.get("xfail"):
        # Known corpus gap (tracked via 'xfail: true' in the golden JSON):
        # a persistent miss is expected; the moment it clears, FAIL
        # to force unflagging(strict-xfail parity with scripts/golden_check.py).
        if not misses:
            pytest.fail(
                f"xfail golden {path.stem} now passes — gap closed; "
                "remove the 'xfail' flag from its golden JSON"
            )
        return
    assert misses == [], f"missing facts: {misses}"


@pytest.mark.short
@pytest.mark.parametrize("path", _SHORT_FILES, ids=lambda p: p.stem)
def test_golden_facts_short(path, require_servers):
    """Short golden tier: 8 representative queries covering all types
    (entity, comparison, recipe, general). ~3-5 min. Filter: -m short."""
    _check_golden_file(path)


@pytest.mark.long
@pytest.mark.parametrize("path", _LONG_FILES, ids=lambda p: p.stem)
def test_golden_facts_long(path, require_servers):
    """Long golden tier: remaining 39 queries. ~11-23 min. Filter: -m long.
    Default (no -m) runs both tiers = all 47 pipeline files exactly once
    (store-only cases excluded; see module docstring)."""
    _check_golden_file(path)
