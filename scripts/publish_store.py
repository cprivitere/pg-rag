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
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

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


def _uri(path: str) -> str:
    """Bucket URIs need the explicit ``buckets/`` segment: bare
    ``hf://<ns>/<name>/...`` is resolved as a *model repo* and 404s."""
    return f"hf://buckets/{BUCKET}/{path}"


def _token() -> str | None:
    return os.environ.get("HF_TOKEN") or None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _auth_probe():
    """Fail before any work if there is no usable write token."""
    from huggingface_hub import HfApi

    api = HfApi(token=_token())
    try:
        who = api.whoami()
    except Exception as exc:  # any HF failure is fatal here
        raise SystemExit(f"no HF write auth: {exc}") from exc
    print(f"authenticated as {who.get('name')} (bucket {BUCKET})")
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


def _upload(fs, local: Path, remote: str) -> None:
    dest = _uri(remote)
    with local.open("rb") as fin, fs.open(dest, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    local_bytes = local.stat().st_size
    remote_bytes = fs.info(dest)["size"]
    print(f"  {remote}: remote {remote_bytes:,} B / local {local_bytes:,} B")
    if remote_bytes != local_bytes:
        raise SystemExit(f"size mismatch after upload: {dest} — re-run the upload")


def main() -> int:
    if BUCKET == PUBLIC_BUCKET:
        raise SystemExit(f"refusing: {BUCKET} is the PUBLIC corpus bucket (store data must not leak)")

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
    print("Next molab sandbox run-all picks up the new snapshot (notebook store_access/store_snapshot).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
