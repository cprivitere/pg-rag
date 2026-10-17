"""Tests scripts.publish_store's branch routing and its two privacy guards.

`--docs` (`mise upload-docs`) added a second publish path, so both directions now
need pinning: the docs path must reach the PUBLIC corpus bucket and must never
open the store, and the two paths must never collapse onto one bucket. Everything
here is offline — the HF client is replaced by a fake module."""
# contract: docs mode publishes data/documents.json to the PUBLIC bucket and
# reads no store data; store mode refuses the public bucket before any HF call.

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


def test_docs_mode_publishes_to_the_public_bucket(monkeypatch, tmp_path):
    docs = tmp_path / "documents.json"
    docs.write_text('[{"id": "fireball-ability"}]')
    monkeypatch.setattr(pub, "DOCS", docs)
    probed: list[str] = []
    monkeypatch.setattr(pub, "_auth_probe", lambda bucket=pub.BUCKET: probed.append(bucket))
    monkeypatch.setattr(
        pub, "_snapshot", lambda: pytest.fail("docs mode opened the store snapshot")
    )
    objects: dict[str, int] = {}
    _fake_hub(monkeypatch, objects)

    assert pub.main(["--docs"]) == 0
    assert probed == [pub.PUBLIC_BUCKET]
    # The upload landed in the public corpus bucket the molab notebook reads,
    # and the recorded size is the local file's (the post-upload check).
    assert objects == {f"hf://buckets/{pub.PUBLIC_BUCKET}/{pub.REMOTE_DOCS}": docs.stat().st_size}


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
