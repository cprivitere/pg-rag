import argparse
import json
import os
import re
import sys
from pathlib import Path

from pgrag.rag.pipeline import _fit_context, ask

GOLDEN_DIR = Path("data/golden")
TRACE_DIR = Path("data/retrieval_traces")

# Deterministic generation for reproducible golden runs: temperature 0 with a
# fixed seed makes generation best-effort repeatable (llama.cpp may still vary).
GENERATION = {"temperature": 0, "seed": 0}


def normalize(text):
    """Case/punctuation-insensitive text for fact-presence matching.

    Apostrophes and single quotes are DELETED (not spaced) before the general
    punctuation mangling, so a contraction in the source or answer ("don't",
    "don\u2019t") normalizes to the same token as a contraction-stripped golden
    fact variant ("dont") -- the golden files author variants that way (e.g.
    "curses dont wear off"). Other punctuation still collapses to a space
    ("Blacksmithing: 25" -> "blacksmithing 25")."""
    text = (text or "").lower()
    text = re.sub(r"[\u2018\u2019\u201a\u201b`']", "", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def check_golden(golden, trace=None):
    """Run one golden case. Returns (result, misses, context) where misses is a
    list of (variants, first_variant) tuples and context is the normalized text
    of the exact documents the LLM received."""
    result = ask(golden["question"], generation=GENERATION, trace=trace)
    answer = normalize(result["answer"])
    # Mirror the pipeline's _fit_context trim so a truncated (never-sent) doc
    # is not mislabeled GEN-side; entity dossiers are already ≤ budget, so
    # this is a no-op there.
    fitted = _fit_context(result.get("documents") or [])
    context = normalize("\n\n---\n\n".join(fitted))
    misses = []
    for variants in golden["facts"]:
        if not any(normalize(v) in answer for v in variants):
            misses.append((variants, variants[0]))
    return result, misses, context


def main():
    parser = argparse.ArgumentParser(description="Run the fact-presence golden eval.")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="for each FAIL, report whether the missing fact is present in the "
        "retrieved context (GEN-side extraction gap) or absent (RET-side "
        "retrieval gap), using the exact context the LLM received.",
    )
    parser.add_argument(
        "--export",
        nargs="?",
        const="data/golden/golden_context_bundle.jsonl",
        default=None,
        metavar="PATH",
        help="also write, per case, the exact fitted context the LLM received "
        "(post-gap-fill/synthesis) plus question/query_type/facts/local answer "
        "to a JSONL bundle for oracle replay on a stronger reader. Bare "
        "--export writes data/golden/golden_context_bundle.jsonl.",
    )
    parser.add_argument(
        "--type",
        choices=["entity", "recipe", "comparison", "general"],
        help="run only goldens whose JSON `type` matches (repeatable "
        "selections are not supported here; use one value)",
    )
    parser.add_argument(
        "--max-facts",
        type=int,
        default=None,
        metavar="N",
        help="run only goldens with <= N fact groups (weight cap)",
    )
    args = parser.parse_args()

    def _matches(golden):
        return (args.type is None or golden.get("type") == args.type) and (
            args.max_facts is None or len(golden["facts"]) <= args.max_facts
        )

    total_miss = 0
    export_records = []
    capture_trace = os.environ.get("PGRAG_TRACE") == "1"
    total_xpass = 0
    ran_any = False
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        golden = json.loads(path.read_text(encoding="utf-8"))
        if golden.get("store"):
            # Store-only case: facts live in the agentic SQLite store, not the
            # RAG corpus — pipeline.ask can never produce them. Runs via
            # `mise agentic-eval` instead.
            continue
        if not _matches(golden):
            continue
        ran_any = True
        trace = {} if capture_trace else None
        result, misses, context = check_golden(golden, trace=trace)
        if args.export:
            export_records.append(
                {
                    "id": golden["id"],
                    "question": golden["question"],
                    "type": golden.get("type"),
                    "query_type": result.get("query_type"),
                    "xfail": bool(golden.get("xfail")),
                    "fitted_docs": _fit_context(result.get("documents") or []),
                    "facts": golden["facts"],
                    "local_misses": [first for _variants, first in misses],
                    "local_answer": result.get("answer"),
                }
            )
        if capture_trace:
            TRACE_DIR.mkdir(parents=True, exist_ok=True)
            try:
                trace_path = TRACE_DIR / f"{golden['id']}.json"
                trace_path.write_text(
                    json.dumps(trace, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError as exc:
                print(f"    (trace write failed: {exc})")
        if golden.get("xfail"):
            if misses:
                print(f"[KNOWN-GAP] {golden['id']} ({golden['type']}, {result['query_type']})")
                continue
            print(f"[XPASS] {golden['id']} — gap closed; remove the 'xfail' flag")
            total_xpass += 1
            continue
        status = "PASS" if not misses else "FAIL"
        print(f"[{status}] {golden['id']} ({golden['type']}, {result['query_type']})")
        for variants, first in misses:
            if args.diagnose:
                in_ctx = any(normalize(v) in context for v in variants)
                tag = (
                    "GEN-SIDE (fact in context but not extracted)"
                    if in_ctx
                    else "RET-SIDE (fact not in retrieved context)"
                )
                print(f"    MISSING: {first}  [{tag}]")
            else:
                print(f"    MISSING: {first}")
        total_miss += len(misses)

    if not ran_any:
        filters = [
            f"--type {args.type}" if args.type else None,
            f"--max-facts {args.max_facts}" if args.max_facts is not None else None,
        ]
        print("no goldens match " + " ".join(f for f in filters if f))
        return

    if args.export:
        out = Path(args.export)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            for rec in export_records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"EXPORTED {len(export_records)} cases -> {out}")

    print()
    if total_miss or total_xpass:
        if total_miss:
            print(f"FAIL: {total_miss} fact(s) missing")
        if total_xpass:
            print(f"FAIL: {total_xpass} known-gap probe(s) now pass — remove 'xfail' flag(s)")
        sys.exit(1)
    print("OK: all golden facts present")


if __name__ == "__main__":
    main()
