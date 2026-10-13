"""Golden fact-presence evaluation for the agentic tool loop.

Single home for the eval surface shared by ``scripts/golden_check.py``,
``scripts/agentic_chat.py --eval`` and ``scripts/agentic_tune.py``: text
normalization, tier membership, per-case execution and report assembly.

Answers come from :func:`pgrag.agentic.loop.run_loop` — the only retrieval
path — so the harness needs the LLM server (:8080) and nothing else. Tier
membership lives here (not in the test file) so the pytest gate, the CLI
harness and the tuning sweeps cannot drift apart.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path

from pgrag.agentic.loop import DEFAULT_MAX_ROUNDS, run_loop
from pgrag.rag.llm import LLMServerError

GOLDEN_DIR = Path("data/golden")
REPORT_PATH = Path("data/agentic_eval_report.json")

# Deterministic generation: temperature 0 + fixed seed makes generation
# best-effort repeatable. Reasoning-ON decoding is not greedy-pinned even
# then (the server's thinking trajectory varies), so a failed case is retried
# once and the better attempt wins — see AGENTS.md ("reasoning-ON production
# runs remain single-sample noisy").
EVAL_GENERATION = {"temperature": 0, "seed": 0}

# Fast tier: 8 representative cases across all four query types
# (~4-5 min at ~30 s/case). The full set is every file in GOLDEN_DIR.
SHORT_TIER_IDS = [
    "bacon-for-joeh",  # entity: simplest, 3 facts
    "fireball-ability",  # entity: variant normalization
    "moonstone-item",  # entity: item description + its one crafting use
    "beginners-arrow-recipe",  # recipe: ingredients + skill/level
    "fireball-vs-fire-breath-damage",  # comparison: damage numbers
    "healing-potion-omega",  # recipe: ingredients + effect
    "cheesemaking-leveling",  # entity: arithmetic, 8 facts, hardest
    "field-mushroom-locations",  # general: wiki location list
]


def normalize(text: str) -> str:
    """Case/punctuation-insensitive text for fact-presence matching.

    Apostrophes and single quotes are DELETED (not spaced) before the general
    punctuation mangling, so a contraction in the source or answer ("don't",
    "don't") normalizes to the same token as a contraction-stripped golden
    fact variant ("dont") -- the golden files author variants that way (e.g.
    "curses dont wear off"). Other punctuation still collapses to a space
    ("Blacksmithing: 25" -> "blacksmithing 25")."""
    text = (text or "").lower()
    text = re.sub(r"[\u2018\u2019\u201a\u201b`']", "", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def golden_ids() -> list[str]:
    """Every golden case id, in file order."""
    return [p.stem for p in sorted(GOLDEN_DIR.glob("*.json"))]


def case_ids(tier: str, ids_arg: str | None = None) -> list[str]:
    """Resolve ``--tier short|all`` (plus an optional comma-separated id list).

    ``short`` is the representative fast tier; ``all`` is every golden case,
    including the store-only play-history cases.
    """
    every = golden_ids()
    chosen = [i for i in SHORT_TIER_IDS if i in every] if tier == "short" else every
    if ids_arg:
        wanted = [s.strip() for s in ids_arg.split(",") if s.strip()]
        chosen = [i for i in chosen if i in set(wanted)] or [
            i for i in wanted if i in every
        ]
    return chosen


def load_case(case_id: str) -> dict:
    path = GOLDEN_DIR / f"{case_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def missing_facts(golden: dict, answer: str) -> list[str]:
    """Golden facts (first author variant) absent from ``answer``."""
    return [
        variants[0]
        for variants in golden["facts"]
        if not any(normalize(v) in answer for v in variants)
    ]


def run_case(
    golden: dict,
    *,
    corpus: str = "full",
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    store: str = "data/sqlite_gorgon.db",
    generation: dict | None = None,
    retry: bool = True,
) -> dict:
    """Answer one golden case through the tool loop and score it.

    Returns ``{id, status, rounds, missing, answer, documents, trace_rounds}``
    where ``status`` is PASS / FAIL / KNOWN-GAP (``xfail``) / XPASS / ERROR.
    An LLM server failure is reported as an ERROR record (with ``error``) so a
    long batch keeps going instead of dying on one bad round.
    """
    gen = dict(EVAL_GENERATION if generation is None else generation)
    started = time.time()
    try:
        result = run_loop(
            golden["question"],
            corpus=corpus,
            max_rounds=max_rounds,
            generation=gen,
            store_path=store,
        )
        answer = result["answer"]
        misses = missing_facts(golden, normalize(answer))
        if retry and not golden.get("xfail") and (misses or not normalize(answer).strip()):
            result2 = run_loop(
                golden["question"],
                corpus=corpus,
                max_rounds=max_rounds,
                generation=gen,
                store_path=store,
            )
            answer2 = result2["answer"]
            misses2 = missing_facts(golden, normalize(answer2))
            if len(misses2) < len(misses):
                result, answer, misses = result2, answer2, misses2
    except LLMServerError as exc:
        return {
            "id": golden["id"],
            "status": "ERROR",
            "rounds": 0,
            "missing": [],
            "answer": "",
            "documents": [],
            "trace_rounds": [],
            "error": str(exc),
        }
    if golden.get("xfail"):
        status = "KNOWN-GAP" if misses else "XPASS"
    else:
        status = "FAIL" if misses else "PASS"
    return {
        "id": golden["id"],
        "status": status,
        "rounds": result.get("rounds", 0),
        "elapsed_s": round(time.time() - started, 1),
        "missing": misses,
        "answer": answer,
        "documents": result.get("documents") or [],
        "trace_rounds": result.get("trace_rounds") or [],
    }


def run_eval(
    ids: list[str],
    *,
    tier: str = "all",
    corpus: str = "full",
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    store: str = "data/sqlite_gorgon.db",
    on_case: Callable[[dict], None] | None = None,
) -> dict:
    """Run every id and return the report dict (no printing, no exit codes)."""
    records: list[dict] = []
    total_miss = 0
    total_xpass = 0
    started = time.time()
    for case_id in ids:
        record = run_case(
            load_case(case_id),
            corpus=corpus,
            max_rounds=max_rounds,
            store=store,
        )
        if record["status"] in ("KNOWN-GAP", "ERROR"):
            records.append(record)
            if on_case:
                on_case(record)
            continue
        total_miss += len(record["missing"])
        if record["status"] == "XPASS":
            total_xpass += 1
        records.append(record)
        if on_case:
            on_case(record)
    report = {
        "tier": tier,
        "corpus": corpus,
        "max_rounds": max_rounds,
        "store": store,
        "elapsed_s": round(time.time() - started, 1),
        "cases": records,
        "facts_missing": total_miss,
        "errors": sum(1 for r in records if r["status"] == "ERROR"),
        "xfail_still_failing": sum(1 for r in records if r["status"] == "KNOWN-GAP"),
        "xpass": total_xpass,
    }
    return report


def write_report(report: dict, path: Path = REPORT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
