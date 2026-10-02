"""Offline, deterministic integrity validation for the whole pg-rag pipeline.

Layered checks feed two buckets, returned by validate_all:

  warnings  — upstream/transient states reachable by a normal partial or
              mid-cycle workflow, whose only remedy is another command. \
              Reported with a remediation pointer but never gate (exit 0).
              Includes: missing/empty sources, a missing documents.json,
              a stale DOCUMENTS_VERSION (docs regenerated, not yet indexed),
              and wiki .meta.json drift (orphans/missing — cleanup stays owned
              by download-wiki). validate has no side effects on these.
  issues    — corruption a valid build never emits (atomic os.replace means a
              malformed or schema-breaking documents.json is a regression),
              plus the index-internal Chroma consistency check.

  sources    — CDN JSON tables and wiki page dumps exist and are non-empty.
  documents  — documents.json parses, is a non-empty list, has unique non-empty
               ids, holds the id/metadata.source/metadata.table identity
               contract, and is not stale w.r.t. DOCUMENTS_VERSION.
  wiki_meta  — data/wiki/.meta.json parses, every tracked filename exists on
               disk, and no untracked .txt files are left behind. Strictly
               read-only: never mutates .meta.json or page dumps.
  index      — vectorstore health_check(): Chroma count/dimension/hash/ID-set
               consistency against documents.json.

No servers are required: pure local-file + index integrity checking.
"""

import json
from pathlib import Path

from pgrag.config import (
    CDN_DIR,
    DOCUMENTS_VERSION,
    DOCUMENTS_VERSION_FILE,
    WIKI_DIR,
)
from pgrag.vectorstore.health_check import (
    CHROMA_PATH,
    COLLECTION_NAME,
    health_check,
)

DEFAULT_DOCUMENTS_PATH = "data/documents.json"

# Values of doc["metadata"]["source"] emitted by the document builders.
KNOWN_SOURCES = {"cdn", "wiki", "computed", "curated"}


def _check_sources(cdn_dir, wiki_dir, warnings):
    """Report missing/empty sources as warnings: remedy is download-cdn /
    download-wiki, and a partially-downloaded tree is a legit transient."""
    cdn_json = sorted(Path(cdn_dir).glob("*.json"))
    if not cdn_json:
        warnings.append("No CDN JSON tables found in data/cdn/ — run `pgrag download-cdn`")
    else:
        for path in cdn_json:
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except OSError, ValueError:
                warnings.append(
                    f"CDN {path.name} is unreadable/invalid JSON — run `pgrag download-cdn`"
                )
                continue
            if not data:
                warnings.append(f"CDN {path.name} is empty — run `pgrag download-cdn`")

    wiki_txt = sorted(Path(wiki_dir).glob("*.txt"))
    if not wiki_txt:
        warnings.append("No wiki page dumps found in data/wiki/ — run `pgrag download-wiki`")
    else:
        empty = [p.name for p in wiki_txt if p.stat().st_size == 0]
        if empty:
            warnings.append(f"Empty wiki page dumps: {empty[:5]} — run `pgrag download-wiki`")


def _check_documents(documents_path, version_file, warnings, issues):
    """The served corpus. Missing file and stale version warn (mid-cycle);
    malformed or schema-breaking content is corruption and fails."""
    try:
        with open(documents_path, encoding="utf-8") as f:
            docs = json.load(f)
    except FileNotFoundError:
        warnings.append(
            "data/documents.json not found — run `pgrag build-documents` "
            "(`mise generate-docs` or a `mise sync-*` task)"
        )
        return
    except (OSError, ValueError) as e:
        issues.append(f"data/documents.json is unreadable/invalid JSON: {e}")
        return

    if not isinstance(docs, list):
        issues.append("data/documents.json is not a list")
        return
    if not docs:
        issues.append("data/documents.json is empty (a build always emits docs)")
        return

    try:
        meta = json.loads(Path(version_file).read_text(encoding="utf-8"))
        stored = meta.get("version")
    except OSError, ValueError:
        stored = None
    if stored != DOCUMENTS_VERSION:
        # Legit mid-cycle: docs regenerated, not yet indexed. Warn, don't gate —
        # a hard fail would fire the mise rebuild fallback (which then refuses
        # the stale version) and lock validate out of a normal workflow.
        warnings.append(
            f"data/documents.json is stale (stored generator v{stored!r}, "
            f"expected v{DOCUMENTS_VERSION}) — run `mise sync-wiki`/`mise sync` "
            "(or `pgrag build-documents` then `pgrag build-index`) to converge"
        )

    seen = set()
    for doc in docs:
        if not isinstance(doc, dict):
            issues.append("document entry is not an object")
            continue
        doc_id = doc.get("id")
        if not isinstance(doc_id, str) or not doc_id:
            issues.append("document with missing/empty 'id'")
        elif doc_id in seen:
            issues.append(f"duplicate document id: {doc_id!r}")
        seen.add(doc_id)

        meta_ = doc.get("metadata")
        if not isinstance(meta_, dict):
            issues.append(f"document {doc_id!r}: 'metadata' missing or not an object")
        else:
            src = meta_.get("source")
            if src not in KNOWN_SOURCES:
                issues.append(f"document {doc_id!r}: unknown metadata.source {src!r}")
            if not isinstance(meta_.get("table"), str) or not meta_.get("table"):
                issues.append(f"document {doc_id!r}: missing/empty metadata.table")
        if not isinstance(doc.get("text"), str) or not doc.get("text", "").strip():
            issues.append(f"document {doc_id!r}: missing/empty 'text'")


def _check_wiki_meta(wiki_dir, warnings):
    """Strictly read-only on .meta.json and page dumps. Report drift as
    warnings; cleanup ownership stays with download-wiki."""
    meta_path = Path(wiki_dir) / ".meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        warnings.append(".meta.json not found in data/wiki/ — run `pgrag download-wiki`")
        return
    except (OSError, ValueError) as e:
        warnings.append(f".meta.json is unreadable/invalid JSON: {e}")
        return

    pages = meta.get("pages", {})
    tracked = {
        info.get("filename")
        for info in pages.values()
        if isinstance(info, dict) and info.get("filename")
    }
    if not tracked:
        warnings.append(".meta.json tracks no wiki page filenames — run `pgrag download-wiki`")

    missing = sorted(f for f in tracked if not (Path(wiki_dir) / f).exists())
    if missing:
        warnings.append(
            f"wiki pages referenced in .meta.json but missing on disk: "
            f"{missing[:5]} — run `pgrag download-wiki`"
        )

    on_disk = {p.name for p in Path(wiki_dir).glob("*.txt")}
    orphans = sorted(on_disk - tracked)
    if orphans:
        # Not deleted: orphan cleanup is owned by download-wiki (remove_orphan_files).
        warnings.append(
            f"orphan wiki .txt files not tracked in .meta.json "
            f"({len(orphans)}): {orphans[:5]} — run `pgrag download-wiki` to clean"
        )


def validate_all(
    cdn_dir=None,
    wiki_dir=None,
    documents_path=DEFAULT_DOCUMENTS_PATH,
    version_file=None,
    chroma_path=None,
    collection_name=None,
):
    """Run every offline integrity layer; return 0 healthy or warnings-only,
    1 only on corruption or index-internal inconsistency (actionable in-place).

    The index layer reuses vectorstore.health_check(), which prints its own
    report. Warnings print as advisories without changing the exit code.
    """
    cdn_dir = cdn_dir or CDN_DIR
    wiki_dir = wiki_dir or WIKI_DIR
    version_file = version_file or DOCUMENTS_VERSION_FILE

    warnings = []
    issues = []
    _check_sources(cdn_dir, wiki_dir, warnings)
    _check_documents(documents_path, version_file, warnings, issues)
    _check_wiki_meta(wiki_dir, warnings)

    if warnings:
        print("Warnings (upstream states — no side effects taken):")
        for warning in warnings:
            print(f"  ! {warning}")
        print()

    if issues:
        print("Corruption found:")
        for issue in issues:
            print(f"  - {issue}")
        print()

    index_rc = health_check(
        chroma_path=chroma_path or CHROMA_PATH,
        collection_name=collection_name or COLLECTION_NAME,
        documents_path=documents_path,
    )

    if issues or index_rc != 0:
        if issues:
            print(f"FAIL: {len(issues)} corruption issue(s) in documents.json")
        if index_rc != 0:
            print("FAIL: index integrity check reported issues")
        return 1

    print("OK: all pipeline integrity checks passed")
    return 0
