"""Golden quick-rerun + flaky-focus tool.

Two jobs in one place, both built on `golden_check.check_golden` (the same
fact-presence harness the pytest golden tests use):

1. Rerun named golden case(s) without sitting through a whole tier (~1-2 min
   per case instead of ~7 min for the 9-file short tier):

       uv run python scripts/golden_rerun.py --id fireball-ability
       uv run python scripts/golden_rerun.py --id fireball-ability,cheesemaking-leveling
       uv run python scripts/golden_rerun.py --all-short     # the short tier

2. Focus on historically troublesome cases. Every run (rerun or full batch)
   appends one JSONL record per golden case to data/golden/history.jsonl
   ({ts, id, missing, query_type, attempt_n}). Non-xfail MISSes get counted;
   `history.jsonl` is the "failed a lot over time" ledger:

       uv run python scripts/golden_rerun.py --selection flaky

   selects ids whose *recent* miss ratio (>50% of last 10 harness-run records)
   past a threshold, or ids listed manually in data/golden/FLAKY.txt (one id
   per line, # comments allowed), then reruns them with a higher attempt
   budget and prints a per-id miss-count table after.

Also a shared quality check: `--selection list` shows every id with its
computed recent-miss stats, so the flaky adjudication is transparent before
you freeze it into FLAKY.txt (or delete it).

Requires the LLM (:8080) + embed (:8081) servers, like the golden pytest.
Offline contract tests cover the selection/history logic without servers.
"""

import argparse
import collections
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from golden_check import (
    GOLDEN_DIR,
    TRACE_DIR,
    check_golden,
    normalize,
)

HISTORY_FILE = GOLDEN_DIR / "history.jsonl"
FLAKY_FILE = GOLDEN_DIR / "FLAKY.txt"

# How many of the most recent records per id the flaky ratio considers.
_RECENT_WINDOW = 10
# An id is "troublesome" if it missed in more than this share of its recent
# harness-run records (>50% = a persistent fact-extraction miss, not LLM
# variance).
_FLAKY_MISSING_SHARE = 0.5
# --selection flaky reruns each case up to this many attempts (vs the
# harness standard 2), because flaky hunters rerun the same pain repeatedly.
_FLAKY_MAX_ATTEMPTS = 3


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def load_history(path: Path = HISTORY_FILE) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # truncated mid-write tail — skip, do not fail
        if isinstance(rec, dict) and rec.get("id"):
            records.append(rec)
    return records


def record_history(outcome: dict, path: Path = HISTORY_FILE) -> None:
    """Append one record per golden case. outcome: {id, missing: bool,
    missing_facts: [..'], query_type, attempt_n}."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf8") as fh:
        fh.write(json.dumps({**outcome, "ts": _utc_now()}, ensure_ascii=False) + "\n")


def flaky_ids(
    history: list[dict] | None = None,
    manual_file: Path = FLAKY_FILE,
    window: int = _RECENT_WINDOW,
    threshold: float = _FLAKY_MISSING_SHARE,
    explicit_manual: bool = False,
) -> list[str]:
    """Ids that keep missing. Two sources, unioned:

    - derived: of the LAST `window` records per id, share missing > threshold.
    - manual: ids listed in data/golden/FLAKY.txt (optional, comment lines
      start '#'). Only consulted when `explicit_manual` is True — otherwise
      manual picks would surprise.

    Sorts derived before manual; both id order stable by recency (their most
    recent record's ts desc — freshest pain first).
    """
    history = load_history() if history is None else history
    by_id: dict[str, list[dict]] = collections.defaultdict(list)
    for rec in history:
        by_id[rec["id"]].append(rec)

    derived = []
    for gid, recs in by_id.items():
        recent = recs[-window:]
        if not recent:
            continue
        missing = sum(1 for r in recent if r.get("missing"))
        share = missing / len(recent)
        if share > threshold:
            derived.append((recent[-1].get("ts", ""), gid))
    derived.sort(reverse=True)  # freshest-missing first
    derived_ids = [gid for _, gid in derived]

    manual_ids: list[str] = []
    if explicit_manual and manual_file.exists():
        for line in manual_file.read_text(encoding="utf8").splitlines():
            gid = line.strip()
            if gid and not gid.startswith("#"):
                manual_ids.append(gid)

    seen: set[str] = set()
    out: list[str] = []
    for gid in derived_ids + manual_ids:
        if gid in seen:
            continue
        seen.add(gid)
        out.append(gid)
    return out


def miss_stats(history: list[dict] | None = None) -> list[tuple]:
    """(id, missing_share_last10, total_records) over all history."""
    history = load_history() if history is None else history
    by_id = collections.defaultdict(list)
    for rec in history:
        by_id[rec["id"]].append(rec)
    rows = []
    for gid, recs in by_id.items():
        recent = recs[-_RECENT_WINDOW:]
        missing = sum(1 for r in recent if r.get("missing"))
        rows.append((gid, missing / len(recent) if recent else 0.0, len(recs)))
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows


def resolve_ids(args) -> list[tuple[Path, dict]]:
    """(golden_path, golden_dict) list for the requested selection.

    Honors `args.type` / `args.max_facts` filters on every branch; a
    selection that was non-empty before filtering but empty after prints
    `no goldens match` and returns []."""
    all_golden = {p.stem: p for p in sorted(GOLDEN_DIR.glob("*.json"))}

    def _matches(g: dict) -> bool:
        if g.get("store"):
            # Store-only case: facts live in the agentic SQLite store, not the
            # RAG corpus — the pipeline harness can never produce them.
            return False
        return (args.type is None or g.get("type") == args.type) and (
            args.max_facts is None or len(g["facts"]) <= args.max_facts
        )

    def _filter_label() -> str:
        parts = [
            f"--type {args.type}" if args.type else None,
            f"--max-facts {args.max_facts}" if args.max_facts is not None else None,
        ]
        return " ".join(p for p in parts if p)

    def _load_filtered(paths: list[Path], prefilter_count: int) -> list[tuple[Path, dict]]:
        pairs = []
        for p in paths:
            g = json.loads(p.read_text(encoding="utf8"))
            if _matches(g):
                pairs.append((p, g))
        if not pairs and prefilter_count:
            print(f"no goldens match {_filter_label()}")
        return pairs

    if args.selection == "flaky":
        ids = flaky_ids(explicit_manual=True)
        if not ids:
            print(
                "No flaky ids (history + FLAKY.txt empty / ratios below "
                f"{_FLAKY_MISSING_SHARE:.0%}). Run --selection list to audit."
            )
            return []
        print(f"Flaky selection: {', '.join(ids)}")
        present = [all_golden[g] for g in ids if g in all_golden]
        return _load_filtered(present, len(present))
    if args.selection == "all":
        return _load_filtered(list(all_golden.values()), len(all_golden))
    # explicit --id list
    wanted = [s.strip() for s in (args.ids or "").split(",") if s.strip()]
    missing = [w for w in wanted if w not in all_golden]
    if missing:
        print(f"Unknown golden id(s): {missing}")
        print(f"Valid ids: {sorted(all_golden)}")
        sys.exit(2)
    store_only = [
        w for w in wanted if json.loads(all_golden[w].read_text(encoding="utf-8")).get("store")
    ]
    if store_only:
        print(
            "Store-only golden id(s) cannot run here — facts live in the agentic"
            " SQLite store, not the RAG corpus:"
        )
        for w in store_only:
            print(f"  {w}: run via mise agentic-eval (or agentic_chat.py --eval)")
        sys.exit(2)
    return _load_filtered([all_golden[w] for w in wanted], len(wanted))


def run_cases(
    cases, max_attempts: int, diagnose: bool, trace: bool, history_path: Path | None = None
) -> int:
    """Run golden cases and persist one history record per case.

    history_path: explicit ledger override (main passes None -> module
    default; tests pass a tmp path). Returns the process exit code.
    """

    def _persist(outcome: dict) -> None:
        record_history(outcome, path=history_path or HISTORY_FILE)

    total_miss = 0
    xfail_gap_closed = 0
    errored_cases = []
    for path, golden in cases:
        misses = []
        attempt = 0
        result = None
        context = ""
        # contract: attempt_n = the attempt on which the final check ran
        # (max_attempts if every attempt errored and none produced a result);
        # the loop reverts `attempt` from the original `for attempt in
        # range(...)` name — B007 intended no rename.
        for attempt in range(1, max_attempts + 1):  # noqa: B007 - attempt is the loop-carried attempt counter
            rec_trace = {} if trace else None
            try:
                result, misses, context = check_golden(golden, trace=rec_trace)
            except Exception as exc:
                print(f"[ERROR] {path.stem}: {exc}", file=sys.stderr)
                result = None
                continue
            if trace and rec_trace:
                TRACE_DIR.mkdir(parents=True, exist_ok=True)
                try:
                    (TRACE_DIR / f"{path.stem}.json").write_text(
                        json.dumps(rec_trace, indent=2, ensure_ascii=False), encoding="utf8"
                    )
                except OSError as exc:
                    print(f"    (trace write failed: {exc})")
            if not misses:
                break
        if result is None:
            # Every attempt errored (e.g. a service is down): the case was
            # never actually checked — record a miss, never a false PASS.
            errored_cases.append(path.stem)
            missing = True
            query_type = "?"
            _persist(
                {
                    "id": golden["id"],
                    "missing": True,
                    "missing_facts": ["<harness-error: all attempts failed>"],
                    "query_type": query_type,
                    "attempt_n": attempt,
                }
            )
            total_miss += 1
            print(f"[FAIL] {golden['id']} (harness error every attempt — check services)")
            continue
        missing = bool(misses)
        query_type = (result or {}).get("query_type", "?")
        _persist(
            {
                "id": golden["id"],
                "missing": missing,
                "missing_facts": [first for _, first in misses] if misses else [],
                "query_type": query_type,
                "attempt_n": attempt,
            }
        )
        if golden.get("xfail"):
            if misses:
                print(
                    f"[KNOWN-GAP] {golden['id']} ({golden['type']}, {query_type}) after {attempt} attempt(s)"
                )
                continue
            print(f"[XPASS] {golden['id']} - gap closed; remove xfail flag")
            xfail_gap_closed += 1
            continue
        status = "PASS" if not misses else f"FAIL (after {attempt} attempt(s))"
        print(f"[{status}] {golden['id']} ({golden['type']}, {query_type})")
        for variants, first in misses:
            in_ctx = diagnose and any(normalize(v) in context for v in variants)
            tag = " [GEN-SIDE: fact in context, LLM dropped it]" if in_ctx else ""
            print(f"    MISSING: {first}{tag}")
        total_miss += len(misses)

    print()
    if total_miss or xfail_gap_closed:
        if total_miss:
            print(f"FAIL: {total_miss} fact(s) missing")
        if xfail_gap_closed:
            print(f"FAIL: {xfail_gap_closed} known-gap probe(s) now pass - remove xfail flag(s)")
        return 1
    print("OK: all requested golden facts present")
    return 0


def main() -> int | None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sel = parser.add_mutually_exclusive_group(required=True)
    sel.add_argument(
        "--id", dest="ids", help="comma-separated golden id(s) (file stems under data/golden/)"
    )
    sel.add_argument(
        "--selection",
        choices=["flaky", "all", "list"],
        help="flaky: only historically-troublesome ids; "
        "all: full set; list: print miss statistics and exit",
    )
    parser.add_argument(
        "--attempts", type=int, default=2, help="max attempts per case (default: 2, harness parity)"
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="tag each MISS as GEN-side (fact in context) or RET-side",
    )
    parser.add_argument(
        "--trace", action="store_true", help="write retrieval traces to data/retrieval_traces/"
    )
    parser.add_argument(
        "--type",
        choices=["entity", "recipe", "comparison", "general"],
        help="run only goldens whose JSON `type` matches",
    )
    parser.add_argument(
        "--max-facts",
        type=int,
        default=None,
        metavar="N",
        help="run only goldens with <= N fact groups (weight cap)",
    )
    args = parser.parse_args()

    if args.selection == "list":
        history = load_history()
        if not history:
            print(
                "No history yet (data/golden/history.jsonl absent or empty). "
                "Records accrue automatically on every golden rerun here or "
                "via tests/test_golden_check.py hook when misses occur."
            )
            return 0
        print(f"{'id':40s} {'recent-miss%':>12s} {'records':>8s}")
        for gid, share, n in miss_stats(history):
            flag = "  <-- flaky" if share > _FLAKY_MISSING_SHARE else ""
            print(f"{gid:40s} {share:14.0%} {n:8d}{flag}")
        return 0

    cases = resolve_ids(args)
    if not cases:
        return 0
    return run_cases(cases, args.attempts, args.diagnose, args.trace)


if __name__ == "__main__":
    raise SystemExit(main())
