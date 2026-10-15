"""Golden quick-rerun + flaky-focus tool.

Two jobs in one place, both driving ``pgrag.agentic.eval.run_case`` — the same
tool-loop fact-presence harness the pytest golden gate uses:

1. Rerun named golden case(s) without sitting through a whole tier (~30 s per
   case instead of minutes for a tier):

       uv run python scripts/golden_rerun.py --id fireball-ability
       uv run python scripts/golden_rerun.py --id fireball-ability,cheesemaking-leveling
       uv run python scripts/golden_rerun.py --selection all --type recipe

2. Focus on historically troublesome cases. Every run (rerun or full batch)
   appends one JSONL record per golden case to data/golden/history.jsonl
   ({ts, id, missing, missing_facts, query_type, attempt_n}):

       uv run python scripts/golden_rerun.py --selection flaky

   selects ids whose *recent* miss ratio (>50% of last 10 records) is past a
   threshold, or ids listed manually in data/golden/FLAKY.txt (one id per
   line, # comments allowed), then reruns them with a higher attempt budget
   and prints a per-id miss-count table after.

Also a shared quality check: `--selection list` shows every id with its
computed recent-miss stats, so the flaky adjudication is transparent before
you freeze it into FLAKY.txt (or delete it).

Needs the LLM (:8080) and nothing else, like the golden pytest.
"""

import argparse
import collections
import datetime as dt
import json
import sys
from pathlib import Path

from pgrag.agentic.eval import GOLDEN_DIR, load_case, normalize, run_case
from pgrag.agentic.loop import DEFAULT_MAX_ROUNDS

HISTORY_FILE = GOLDEN_DIR / "history.jsonl"
FLAKY_FILE = GOLDEN_DIR / "FLAKY.txt"
TRACE_DIR = Path("data/retrieval_traces")

# How many of the most recent records per id the flaky ratio considers.
_RECENT_WINDOW = 10
# An id is "troublesome" if it missed in more than this share of its recent
# records (>50% = a persistent extraction/retrieval miss, not LLM variance).
_FLAKY_MISSING_SHARE = 0.5
# --selection flaky reruns each case up to this many attempts (vs the default
# 2), because flaky hunters rerun the same pain repeatedly.
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
    missing_facts: [..], query_type, attempt_n}."""
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

    Ids with no `data/golden/<id>.json` are dropped from both sources: the
    ledger and FLAKY.txt outlive deleted goldens, and a deleted id must not
    print (or read) as flaky.

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

    have = set(all_golden_ids())
    seen: set[str] = set()
    out: list[str] = []
    for gid in derived_ids + manual_ids:
        if gid in seen or gid not in have:
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


def audit_rows(history: list[dict] | None = None) -> list[tuple]:
    """`miss_stats` rows restricted to ids that still have a golden file.

    `data/golden/history.jsonl` is an append-only ledger, so deleted ids
    (`animal-handling-ability-audit`, `royal-jelly-gathering`, …) would
    otherwise keep showing up (and, for a busy id, flagging) as flaky.
    """
    have = set(all_golden_ids())
    return [row for row in miss_stats(history) if row[0] in have]


def all_golden_ids() -> list[str]:
    return [p.stem for p in sorted(GOLDEN_DIR.glob("*.json"))]


def _matches(golden: dict, args) -> bool:
    return (args.type is None or golden.get("type") == args.type) and (
        args.max_facts is None or len(golden["facts"]) <= args.max_facts
    )


def _filter_label(args) -> str:
    parts = [
        f"--type {args.type}" if args.type else None,
        f"--max-facts {args.max_facts}" if args.max_facts is not None else None,
    ]
    return " ".join(p for p in parts if p)


def resolve_cases(args) -> list[dict]:
    """Golden dicts for the requested selection, honoring --type/--max-facts.

    A selection that was non-empty before filtering but empty after prints
    `no goldens match <filter>` and returns []."""
    every = all_golden_ids()

    if args.selection == "flaky":
        ids = flaky_ids(explicit_manual=True)
        if not ids:
            print(
                "No flaky ids (history + FLAKY.txt empty / ratios below "
                f"{_FLAKY_MISSING_SHARE:.0%}). Run --selection list to audit."
            )
            return []
        print(f"Flaky selection: {', '.join(ids)}")
        wanted = [g for g in ids if g in set(every)]
    elif args.selection == "all":
        wanted = every
    else:  # explicit --id list
        wanted = [s.strip() for s in (args.ids or "").split(",") if s.strip()]
        unknown = [w for w in wanted if w not in set(every)]
        if unknown:
            print(f"Unknown golden id(s): {unknown}")
            print(f"Valid ids: {every}")
            sys.exit(2)

    cases = [load_case(gid) for gid in wanted]
    filtered = [g for g in cases if _matches(g, args)]
    if not filtered and cases:
        print(f"no goldens match {_filter_label(args)}")
    return filtered


def _write_trace(case_id: str, record: dict) -> None:
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (TRACE_DIR / f"{case_id}.json").write_text(
            json.dumps(
                {
                    "rounds": record["rounds"],
                    "trace_rounds": record.get("trace_rounds") or [],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf8",
        )
    except OSError as exc:
        print(f"    (trace write failed: {exc})")


def run_cases(
    cases,
    max_attempts: int,
    diagnose: bool,
    trace: bool,
    max_rounds: int,
    history_path: Path | None = None,
) -> int:
    """Run golden cases and persist one history record per case.

    history_path: explicit ledger override (main passes None -> module
    default; tests pass a tmp path). Returns the process exit code.
    """

    def _persist(outcome: dict) -> None:
        record_history(outcome, path=history_path or HISTORY_FILE)

    total_miss = 0
    xfail_gap_closed = 0
    for golden in cases:
        misses: list[str] = []
        result = None
        # contract: attempt_n = the attempt on which the final check ran
        # (max_attempts if every attempt errored and none produced a result).
        for attempt in range(1, max_attempts + 1):  # noqa: B007 - attempt is the loop-carried counter
            try:
                # retry=False: the attempt loop above is the retry budget here.
                result = run_case(golden, max_rounds=max_rounds, retry=False)
            except Exception as exc:
                print(f"[ERROR] {golden['id']}: {exc}", file=sys.stderr)
                result = None
                continue
            misses = result["missing"]
            if trace:
                _write_trace(golden["id"], result)
            if not misses:
                break
        # query_type comes from the golden's authored `type` — the loop has no
        # classifier, so the case category is the honest label here.
        query_type = golden.get("type", "?")
        if result is None:
            # Every attempt errored (e.g. the LLM is down): the case was never
            # actually checked — record a miss, never a false PASS.
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
            print(f"[FAIL] {golden['id']} (harness error every attempt — check the LLM server)")
            continue
        missing = bool(misses)
        _persist(
            {
                "id": golden["id"],
                "missing": missing,
                "missing_facts": list(misses),
                "query_type": query_type,
                "attempt_n": attempt,
            }
        )
        if golden.get("xfail"):
            if misses:
                print(f"[KNOWN-GAP] {golden['id']} ({golden['type']}, {query_type})")
            else:
                print(f"[XPASS] {golden['id']} - gap closed; remove xfail flag")
                xfail_gap_closed += 1
            continue
        status = "PASS" if not misses else f"FAIL (after {attempt} attempt(s))"
        print(f"[{status}] {golden['id']} ({golden['type']}, {query_type})")
        for fact in misses:
            in_ctx = False
            if diagnose:
                context = normalize("\n\n---\n\n".join(result.get("documents") or []))
                variants = next(v for v in golden["facts"] if v[0] == fact)
                in_ctx = any(normalize(v) in context for v in variants)
            tag = " [GEN-SIDE: fact in context, LLM dropped it]" if in_ctx else ""
            print(f"    MISSING: {fact}{tag}")
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
        "--attempts", type=int, default=2, help="max attempts per case (default: 2, one retry)"
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=DEFAULT_MAX_ROUNDS,
        help="agentic tool rounds per attempt (default: harness default)",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="tag each MISS as GEN-side (fact in retrieved context) or RET-side",
    )
    parser.add_argument(
        "--trace", action="store_true", help="write loop traces to data/retrieval_traces/"
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
                "Records accrue automatically on every golden rerun here."
            )
            return 0
        print(f"{'id':40s} {'recent-miss%':>12s} {'records':>8s}")
        for gid, share, n in audit_rows(history):
            flag = "  <-- flaky" if share > _FLAKY_MISSING_SHARE else ""
            print(f"{gid:40s} {share:14.0%} {n:8d}{flag}")
        return 0

    cases = resolve_cases(args)
    if not cases:
        return 0
    attempts = _FLAKY_MAX_ATTEMPTS if args.selection == "flaky" else args.attempts
    return run_cases(cases, attempts, args.diagnose, args.trace, args.max_rounds)


if __name__ == "__main__":
    raise SystemExit(main())
