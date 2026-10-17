"""Fact-presence golden eval over the agentic tool loop.

Runs golden cases from ``data/golden/*.json`` through
``pgrag.agentic.loop.run_loop`` — the tool loop over the SQLite store plus
BM25 ``corpus_search`` — and checks that each golden fact appears in the
answer. Needs the LLM server (:8080) and nothing else.

Usage:
    mise golden                  # every case, exit 1 on any missing fact
    mise golden-short            # the 8 fast-tier list cases only
    mise golden-one -- fireball-ability
    uv run python scripts/golden_check.py --tier short --diagnose
    uv run python scripts/golden_check.py --ids bacon-for-joeh --type entity --max-facts 3

``--diagnose`` labels each miss GEN-side (fact was in the context the model
received) or RET-side (never retrieved) using the exact text the loop fed the
model — that is the line between "the loop must fetch better" and "the model
must extract better".
"""

import argparse
import json
import os
import sys
from pathlib import Path

from pgrag.agentic import eval as agentic_eval
from pgrag.agentic.loop import DEFAULT_MAX_ROUNDS, DEFAULT_STORE


def _matches(golden: dict, args) -> bool:
    return (args.type is None or golden.get("type") == args.type) and (
        args.max_facts is None or len(golden["facts"]) <= args.max_facts
    )


def _print_case(record: dict, golden: dict, diagnose: bool) -> None:
    rounds = record["rounds"]
    status = record["status"]
    if status == "KNOWN-GAP":
        print(f"[KNOWN-GAP] {record['id']} ({golden.get('type')}, {rounds} rounds)")
    elif status == "ERROR":
        print(f"[ERROR] {record['id']}: {record.get('error')}")
    elif status == "XPASS":
        print(f"[XPASS] {record['id']} — gap closed; remove the 'xfail' flag")
    else:
        print(f"[{status}] {record['id']} ({golden.get('type')}, {rounds} rounds)")
    if not record["missing"]:
        return
    context = agentic_eval.normalize("\n\n---\n\n".join(record.get("documents") or []))
    for fact in record["missing"]:
        if not diagnose:
            print(f"    MISSING: {fact}")
            continue
        variants = next(v for v in golden["facts"] if v[0] == fact)
        in_ctx = any(agentic_eval.normalize(v) in context for v in variants)
        tag = (
            "GEN-SIDE (fact in context but not extracted)"
            if in_ctx
            else "RET-SIDE (fact not in retrieved context)"
        )
        print(f"    MISSING: {fact}  [{tag}]")


def _write_traces(report: dict) -> None:
    trace_dir = Path("data/retrieval_traces")
    trace_dir.mkdir(parents=True, exist_ok=True)
    for record in report["cases"]:
        try:
            (trace_dir / f"{record['id']}.json").write_text(
                json.dumps(
                    {
                        "rounds": record["rounds"],
                        "trace_rounds": record.get("trace_rounds") or [],
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"    (trace write failed for {record['id']}: {exc})")


def _write_export(report: dict, goldens: dict, path: str) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for record in report["cases"]:
            golden = goldens[record["id"]]
            fh.write(
                json.dumps(
                    {
                        "id": record["id"],
                        "question": golden["question"],
                        "type": golden.get("type"),
                        "xfail": bool(golden.get("xfail")),
                        "context": record.get("documents") or [],
                        "facts": golden["facts"],
                        "local_misses": record["missing"],
                        "local_answer": record["answer"],
                        "trace_rounds": record.get("trace_rounds") or [],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    print(f"EXPORTED {len(report['cases'])} cases -> {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the fact-presence golden eval.")
    parser.add_argument(
        "--tier",
        choices=["short", "all"],
        default="all",
        help="short = the 8 fast-tier list cases (eval.SHORT_TIER_IDS); all = every golden",
    )
    parser.add_argument(
        "--ids",
        default=None,
        help="comma-separated golden ids (overrides the tier selection)",
    )
    parser.add_argument(
        "--corpus",
        choices=["full", "tool"],
        default="full",
        help="corpus_search document store: generated corpus (full) or tool variant",
    )
    parser.add_argument(
        "--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS, help="tool rounds per case"
    )
    parser.add_argument("--store", default=DEFAULT_STORE, help="SQLite store path")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="for each FAIL, report whether the missing fact was in the context the "
        "model received (GEN-side) or never retrieved (RET-side)",
    )
    parser.add_argument(
        "--export",
        nargs="?",
        const="data/golden/golden_context_bundle.jsonl",
        default=None,
        metavar="PATH",
        help="also write, per case, the exact context the model received plus "
        "question/type/facts/local answer to a JSONL bundle for oracle replay "
        "on a stronger reader. Bare --export writes "
        "data/golden/golden_context_bundle.jsonl.",
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
    parser.add_argument(
        "--report",
        default=str(agentic_eval.REPORT_PATH),
        help="where to write the JSON report (default data/agentic_eval_report.json)",
    )
    args = parser.parse_args()

    ids = agentic_eval.case_ids(args.tier, args.ids)
    goldens = {i: agentic_eval.load_case(i) for i in ids}
    goldens = {i: g for i, g in goldens.items() if _matches(g, args)}
    if not goldens:
        print("no goldens match the filter")
        return

    report = agentic_eval.run_eval(
        list(goldens),
        tier=args.tier,
        corpus=args.corpus,
        max_rounds=args.max_rounds,
        store=args.store,
        on_case=lambda record: _print_case(record, goldens[record["id"]], args.diagnose),
    )
    report_path = agentic_eval.write_report(report, Path(args.report))
    if os.environ.get("PGRAG_TRACE") == "1":
        _write_traces(report)
    if args.export:
        _write_export(report, goldens, args.export)

    print()
    print(f"{len(report['cases'])} case(s), {report['elapsed_s']:.0f}s, report -> {report_path}")
    if report["errors"]:
        print(f"FAIL: {report['errors']} case(s) hit an LLM server error")
    total_xpass = report["xpass"]
    if report["facts_missing"] or total_xpass:
        if report["facts_missing"]:
            print(f"FAIL: {report['facts_missing']} fact(s) missing")
        if total_xpass:
            print(f"FAIL: {total_xpass} known-gap probe(s) now pass — remove 'xfail' flag(s)")
    if report["facts_missing"] or total_xpass or report["errors"]:
        sys.exit(1)
    print("OK: all golden facts present")


if __name__ == "__main__":
    main()
