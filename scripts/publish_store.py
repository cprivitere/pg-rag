r"""Publish the agentic SQLite store to the PRIVATE HF bucket the molab
notebook reads for tool-loop mode (`mise upload-store`).

Three objects under ``store/`` in ``Nubula/paddock-private``:

- ``sqlite_gorgon.db``  — a consistent ``VACUUM INTO`` snapshot of
  ``data/sqlite_gorgon.db`` (the live db is WAL; a plain file copy can serve a
  pre-checkpoint state, a snapshot cannot).
- ``manifest.json``     — sizes + sha256 + table row counts, so the sandbox
  verifies what it downloaded before trusting it.
- ``pgrag-src.tar.gz``  — ``git archive`` of ``src/`` + ``scripts/`` as they are
  in the WORKING TREE (temp index, not plain HEAD): molab loads the notebook
  from public GitHub but has no repo of its own, so the loop source ships
  alongside the store — and it must be the source that was verified locally,
  including uncommitted fixes.

The store holds the user's own play history (chat logs, stall sales, kills),
so a public leak is the failure mode this script exists to prevent: it refuses
to run at all if ``BUCKET`` equals the public corpus bucket, and it asserts
``bucket_info(...).private`` before the first byte is uploaded.

Run ``mise sql-store`` first so the snapshot reflects current sources. Re-runs
are idempotent (snapshot + tarball are rebuilt from the current db and HEAD).

``--docs`` (`mise upload-docs`) instead publishes ``data/documents.json`` **and
its version stamp** (``data/derived/documents_version.json``) to the PUBLIC
corpus bucket ``Nubula/paddock`` for the molab notebook — the sandbox's own
tool-corpus build refuses to run without that stamp. Docs mode reads only those
two files; the store is never opened, so it carries no privacy guard, while the
store mode below keeps its ``BUCKET == PUBLIC_BUCKET`` refusal and
``_ensure_private_bucket``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from pgrag.config import DOCUMENTS_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]
os.chdir(REPO_ROOT)

BUCKET = "Nubula/paddock-private"
PUBLIC_BUCKET = "Nubula/paddock"

DB = Path("data/sqlite_gorgon.db")
SNAPSHOT = Path("data/.publish-snapshot.db")
MANIFEST = Path("data/.publish-manifest.json")
TARBALL = Path("data/.publish-src.tar.gz")

REMOTE_STORE = "store/sqlite_gorgon.db"
REMOTE_MANIFEST = "store/manifest.json"
REMOTE_TARBALL = "store/pgrag-src.tar.gz"

DOCS = Path("data/documents.json")
DOCS_STAMP = Path("data/derived/documents_version.json")
REMOTE_DOCS = "documents.json"
REMOTE_DOCS_STAMP = "derived/documents_version.json"
DOCS_BUCKET = PUBLIC_BUCKET


def _uri(path: str, bucket: str = BUCKET) -> str:
    """Bucket URIs need the explicit ``buckets/`` segment: bare
    ``hf://<ns>/<name>/...`` is resolved as a *model repo* and 404s."""
    return f"hf://buckets/{bucket}/{path}"


def _token() -> str | None:
    return os.environ.get("HF_TOKEN") or None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _auth_probe(bucket: str = BUCKET):
    """Fail before any work if there is no usable write token."""
    from huggingface_hub import HfApi

    api = HfApi(token=_token())
    try:
        who = api.whoami()
    except Exception as exc:  # any HF failure is fatal here
        raise SystemExit(f"no HF write auth: {exc}") from exc
    print(f"authenticated as {who.get('name')} (bucket {bucket})")
    return api


def _snapshot() -> None:
    if not DB.exists():
        raise SystemExit(f"{DB} not found — run `mise sql-store` first.")
    live = DB.stat().st_size
    SNAPSHOT.unlink(missing_ok=True)
    uri = "file:" + DB.resolve().as_posix() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        con.execute("VACUUM INTO ?", (str(SNAPSHOT),))
    finally:
        con.close()
    print(f"snapshot {live:,} B live -> {SNAPSHOT.stat().st_size:,} B compacted ({SNAPSHOT})")


def _table_counts() -> dict[str, int]:
    """Row counts read from the SNAPSHOT: what the sandbox will actually get."""
    uri = "file:" + SNAPSHOT.resolve().as_posix() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        names = [
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'entity_names_%' "
                "ORDER BY name"
            )
        ]
        counts = {}
        for name in names:
            counts[name] = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        return counts
    finally:
        con.close()


def _git(*args: str, env: dict | None = None) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _source_tarball() -> tuple[str, list[str]]:
    """Tarball the sandbox extracts and imports into its 3.13 kernel.

    Built from the WORKING TREE through a throwaway index, not from ``HEAD``:
    publishing HEAD while uncommitted fixes still sit in the tree ships source
    that does not match the verified local pipeline. Seen live — the agentic
    loop needed a Python-3.13 syntax fix (PEP 758 ``except A, B:`` is 3.14-only)
    and the HEAD-only tarball was unimportable in the sandbox.

    The archive is taken from a temporary commit (fixed identity + HEAD's date)
    rather than from the bare tree object: `git archive <tree>` stamps every
    member with the current time, so two runs over identical content produced
    different bytes and the published sha256 could not be compared against
    anything. From a commit the archive is reproducible.
    """
    TARBALL.unlink(missing_ok=True)
    head = _git("rev-parse", "--short", "HEAD")
    head_date = _git("show", "-s", "--format=%cI", "HEAD")
    dirty = _git("status", "--porcelain").splitlines()
    index = REPO_ROOT / ".git" / "omp-publish-index"
    index.unlink(missing_ok=True)
    env = {
        **os.environ,
        "GIT_INDEX_FILE": str(index),
        "GIT_AUTHOR_NAME": "pg-rag publish",
        "GIT_AUTHOR_EMAIL": "publish@pg-rag.invalid",
        "GIT_AUTHOR_DATE": head_date,
        "GIT_COMMITTER_NAME": "pg-rag publish",
        "GIT_COMMITTER_EMAIL": "publish@pg-rag.invalid",
        "GIT_COMMITTER_DATE": head_date,
    }
    try:
        _git("read-tree", "HEAD", env=env)
        _git("add", "-A", "--", "src", "scripts", env=env)
        tree = _git("write-tree", env=env)
        commit = _git("commit-tree", tree, "-p", "HEAD", "-m", f"publish {head}", env=env)
        subprocess.run(
            ["git", "archive", "--format=tar.gz", "-o", str(TARBALL), commit, "src", "scripts"],
            check=True,
        )
    finally:
        index.unlink(missing_ok=True)
    if dirty:
        print(f"NOTE: tarball = worktree of {head} + {len(dirty)} uncommitted change(s)")
        for line in dirty[:10]:
            print(f"  {line}")
        if len(dirty) > 10:
            print(f"  ... {len(dirty) - 10} more")
    print(f"source tarball -> {TARBALL} ({TARBALL.stat().st_size:,} B, HEAD {head})")
    return head, dirty


def _ensure_private_bucket(api) -> None:
    """Create if absent, then FORCE private — never upload without asserting it."""
    try:
        api.create_bucket(BUCKET, private=True, exist_ok=True)
    except Exception as exc:
        raise SystemExit(f"cannot create/verify bucket {BUCKET}: {exc}") from exc
    info = api.bucket_info(BUCKET)
    if not info.private:
        print(f"bucket {BUCKET} is public — flipping to private")
        api.update_bucket_settings(BUCKET, private=True)
        info = api.bucket_info(BUCKET)
    if not info.private:
        raise SystemExit("refusing to upload: bucket is not private")
    print(f"bucket private: {info.private} ({info.total_files} files, {info.size:,} B on the Hub)")


def _upload(fs, local: Path, remote: str, bucket: str = BUCKET) -> None:
    dest = _uri(remote, bucket)
    with local.open("rb") as fin, fs.open(dest, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    local_bytes = local.stat().st_size
    remote_bytes = fs.info(dest)["size"]
    print(f"  {remote}: remote {remote_bytes:,} B / local {local_bytes:,} B")
    if remote_bytes != local_bytes:
        raise SystemExit(f"size mismatch after upload: {dest} — re-run the upload")


def _publish_docs() -> int:
    """Publish the corpus documents + their version stamp to the PUBLIC bucket
    (mise upload-docs).

    The stamp is not optional: the molab sandbox derives `tool_documents.json`
    with the tarball's own `scripts/build_tool_corpus.py`, which refuses to run
    without `data/derived/documents_version.json` — the corpus alone left a fresh
    sandbox unable to build the index `corpus_search` needs.

    Everything is validated before the first byte: the remote-size check is
    self-consistent, so a truncated or stale-shape local file would otherwise
    overwrite the good public copy cleanly.
    """
    if DOCS_BUCKET == BUCKET:
        # The mirror of the store-path refusal: the two paths must never collapse
        # onto one bucket, or a misrouted corpus publish would silently land in
        # the private store bucket (or, worse, a store publish in the public one).
        raise SystemExit(f"refusing: docs target {DOCS_BUCKET} equals the private store bucket")
    if not DOCS.exists():
        raise SystemExit(f"{DOCS} not found — run `mise generate-docs` first.")
    if not DOCS_STAMP.exists():
        raise SystemExit(
            f"{DOCS_STAMP} not found — run `mise generate-docs` first (it stamps the version "
            "the sandbox's tool-corpus build checks)."
        )
    try:
        docs = json.loads(DOCS.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"{DOCS} is not valid JSON: {exc}") from exc
    if not isinstance(docs, list) or not docs:
        raise SystemExit(f"{DOCS} does not hold a non-empty list of documents")
    stamp = json.loads(DOCS_STAMP.read_text(encoding="utf-8")).get("version")
    if stamp != DOCUMENTS_VERSION:
        raise SystemExit(
            f"{DOCS_STAMP} says version {stamp}, expected {DOCUMENTS_VERSION} — "
            "run `mise generate-docs` first."
        )
    _auth_probe(DOCS_BUCKET)

    from huggingface_hub import HfFileSystem

    mb = DOCS.stat().st_size / (1024 * 1024)
    print(f"uploading {DOCS} ({mb:.1f} MB) + {DOCS_STAMP} -> hf://buckets/{DOCS_BUCKET}/")
    fs = HfFileSystem(token=_token())
    _upload(fs, DOCS, REMOTE_DOCS, DOCS_BUCKET)
    _upload(fs, DOCS_STAMP, REMOTE_DOCS_STAMP, DOCS_BUCKET)
    print(
        f"OK: hf://buckets/{DOCS_BUCKET}/{REMOTE_DOCS} matches local {mb:.1f} MB "
        f"(stamp v{stamp}). Next molab sandbox boot picks it up."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="publish_store.py",
        description=(
            "Publish pg-rag data to HF buckets: the private store (default, "
            "mise upload-store) or the public corpus documents (--docs, mise upload-docs)."
        ),
    )
    parser.add_argument(
        "--docs",
        action="store_true",
        help="publish data/documents.json to the PUBLIC corpus bucket instead of the store",
    )
    args = parser.parse_args(argv)
    if args.docs:
        return _publish_docs()
    if BUCKET == PUBLIC_BUCKET:
        raise SystemExit(
            f"refusing: {BUCKET} is the PUBLIC corpus bucket (store data must not leak)"
        )

    api = _auth_probe()

    _snapshot()
    tables = _table_counts()
    print(f"tables ({len(tables)}):")
    for name, count in tables.items():
        print(f"  {name}: {count:,}")

    git_head, dirty = _source_tarball()

    # Privacy is enforced before any byte leaves this host.
    _ensure_private_bucket(api)

    manifest = {
        "bucket": BUCKET,
        "published_at": datetime.now(UTC).isoformat(),
        "store": {
            "path": REMOTE_STORE,
            "bytes": SNAPSHOT.stat().st_size,
            "sha256": _sha256(SNAPSHOT),
            "source_mtime": datetime.fromtimestamp(DB.stat().st_mtime).isoformat(),
        },
        "source_tar": {
            "path": REMOTE_TARBALL,
            "bytes": TARBALL.stat().st_size,
            "sha256": _sha256(TARBALL),
            "git_head": git_head,
            "worktree_dirty": bool(dirty),
        },
        "tables": tables,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"manifest -> {MANIFEST} (sha256 store {manifest['store']['sha256'][:12]}…)")

    from huggingface_hub import HfFileSystem

    fs = HfFileSystem(token=_token())
    print(f"uploading to {_uri('')}")
    _upload(fs, SNAPSHOT, REMOTE_STORE)
    _upload(fs, TARBALL, REMOTE_TARBALL)
    _upload(fs, MANIFEST, REMOTE_MANIFEST)

    for temp in (SNAPSHOT, TARBALL, MANIFEST):
        temp.unlink(missing_ok=True)
    print(f"OK: {_uri('store/')} verified; temps removed.")
    print(f"bucket URL: https://huggingface.co/buckets/{BUCKET}")
    print(
        "Next molab sandbox run-all picks up the new snapshot (notebook store_access/store_snapshot)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
