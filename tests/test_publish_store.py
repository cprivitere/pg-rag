"""Tests scripts.publish_store's branch routing and its two privacy guards.

`--docs` (`mise upload-docs`) added a second publish path, so both directions now
need pinning: the docs path must reach the PUBLIC corpus bucket and must never
open the store, and the two paths must never collapse onto one bucket. Everything
here is offline — the HF client is replaced by a fake module."""
# contract: docs mode publishes data/documents.json to the PUBLIC bucket and
# reads no store data; store mode refuses the public bucket before any HF call.

import json
import sys
import types

import pytest

from scripts import publish_store as pub


class _FakeWriter:
    def __init__(self, objects: dict[str, int], dest: str):
        self._objects = objects
        self._dest = dest
        self._size = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._objects[self._dest] = self._size
        return False

    def write(self, data) -> int:
        self._size += len(data)
        return len(data)


class _FakeFs:
    def __init__(self, objects: dict[str, int]):
        self._objects = objects

    def open(self, dest, mode):
        return _FakeWriter(self._objects, dest)

    def info(self, dest):
        return {"size": self._objects[dest]}


def _fake_hub(monkeypatch, objects: dict[str, int]) -> None:
    """Install a stub `huggingface_hub` (imported inside the functions, so
    sys.modules is the seam) recording uploads as {uri: bytes}."""
    module = types.ModuleType("huggingface_hub")

    class _Api:
        def __init__(self, token=None):
            self.token = token

        def whoami(self):
            return {"name": "tester"}

    module.HfApi = _Api
    module.HfFileSystem = lambda token=None: _FakeFs(objects)
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)


def _write_corpus(tmp_path, monkeypatch, *, stamp: int | None = None, payload: str | None = None):
    """A tmp corpus + its version stamp, wired into the module."""
    docs = tmp_path / "documents.json"
    docs.write_text(payload if payload is not None else '[{"id": "fireball-ability"}]')
    stamp_file = tmp_path / "documents_version.json"
    stamp_file.write_text(json.dumps({"version": pub.DOCUMENTS_VERSION if stamp is None else stamp}))
    monkeypatch.setattr(pub, "DOCS", docs)
    monkeypatch.setattr(pub, "DOCS_STAMP", stamp_file)
    return docs


def test_docs_mode_publishes_to_the_public_bucket(monkeypatch, tmp_path):
    docs = _write_corpus(tmp_path, monkeypatch)
    probed: list[str] = []
    monkeypatch.setattr(pub, "_auth_probe", lambda bucket=pub.BUCKET: probed.append(bucket))
    # The store-only steps must not run in docs mode. `_source_tarball` matters
    # most: it writes real repo artifacts (`data/.publish-src.tar.gz`,
    # `.git/omp-publish-index`), so an accidental call would dirty the tree.
    for name in ("_snapshot", "_table_counts", "_source_tarball", "_ensure_private_bucket"):
        monkeypatch.setattr(pub, name, lambda *a, _n=name, **k: pytest.fail(f"docs mode ran {_n}"))
    objects: dict[str, int] = {}
    _fake_hub(monkeypatch, objects)

    assert pub.main(["--docs"]) == 0
    assert probed == [pub.PUBLIC_BUCKET]
    # The corpus AND its stamp land in the public bucket the molab notebook
    # reads: the sandbox's tool-corpus build refuses without the stamp.
    assert objects == {
        f"hf://buckets/{pub.PUBLIC_BUCKET}/{pub.REMOTE_DOCS}": docs.stat().st_size,
        f"hf://buckets/{pub.PUBLIC_BUCKET}/{pub.REMOTE_DOCS_STAMP}": (tmp_path / "documents_version.json").stat().st_size,
    }


def test_docs_mode_validates_the_corpus_before_uploading(monkeypatch, tmp_path):
    """The remote-size check is self-consistent, so a truncated or wrong-shape
    local file would publish clean: validation happens before the first byte."""
    _write_corpus(tmp_path, monkeypatch, payload='{"documents": []}')
    monkeypatch.setattr(pub, "_auth_probe", lambda *a, **k: pytest.fail("validated too late"))
    with pytest.raises(SystemExit) as exc:
        pub.main(["--docs"])
    assert "non-empty list" in str(exc.value)


def test_docs_mode_refuses_a_stale_stamp(monkeypatch, tmp_path):
    """A stamp older than the generator in this checkout means the corpus would
    be shipped against the wrong shape — the sandbox's own refusal is the last
    line of defence, this is the first."""
    _write_corpus(tmp_path, monkeypatch, stamp=pub.DOCUMENTS_VERSION - 1)
    monkeypatch.setattr(pub, "_auth_probe", lambda *a, **k: pytest.fail("validated too late"))
    with pytest.raises(SystemExit) as exc:
        pub.main(["--docs"])
    assert "expected" in str(exc.value)


def test_docs_mode_needs_the_version_stamp(monkeypatch, tmp_path):
    _write_corpus(tmp_path, monkeypatch)
    monkeypatch.setattr(pub, "DOCS_STAMP", tmp_path / "absent_stamp.json")
    with pytest.raises(SystemExit) as exc:
        pub.main(["--docs"])
    assert "generate-docs" in str(exc.value)


def test_docs_mode_refuses_the_store_bucket(monkeypatch, tmp_path):
    """The mirror of the store-path refusal: if the two targets ever collapse,
    neither path may run (otherwise the corpus silently lands in the private
    store bucket, or store data in the public one)."""
    monkeypatch.setattr(pub, "DOCS", tmp_path / "documents.json")
    monkeypatch.setattr(pub, "DOCS_BUCKET", pub.BUCKET)
    with pytest.raises(SystemExit) as exc:
        pub.main(["--docs"])
    assert "private store bucket" in str(exc.value)


def test_docs_mode_needs_the_corpus_file(monkeypatch, tmp_path):
    monkeypatch.setattr(pub, "DOCS", tmp_path / "absent.json")
    with pytest.raises(SystemExit) as exc:
        pub.main(["--docs"])
    assert "generate-docs" in str(exc.value)


def test_store_mode_refuses_the_public_bucket_before_any_hf_call(monkeypatch):
    monkeypatch.setattr(pub, "BUCKET", pub.PUBLIC_BUCKET)
    monkeypatch.setattr(pub, "_auth_probe", lambda *a, **k: pytest.fail("touched the Hub"))
    with pytest.raises(SystemExit) as exc:
        pub.main([])
    assert "must not leak" in str(exc.value)
