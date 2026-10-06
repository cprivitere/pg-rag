# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "huggingface-hub==1.31.0",
#     "marimo[mcp]>=0.24.0",
#     "requests>=2.34.2",
# ]
# ///

import marimo

__generated_with = "0.24.0"
app = marimo.App(width="medium", auto_download=["html"])


@app.cell
def imports():
    # --- stdlib + huggingface_hub only: this notebook never imports torch ---
    # Inference is the vLLM sidecar's job, so there is no env repair, no
    # transformers, no 22 GB in-process fallback and no "restart the session"
    # step. The agentic loop's source arrives as a tarball and needs requests
    # (PEP 723) plus stdlib.
    import hashlib
    import json
    import os
    import shutil
    import sqlite3
    import subprocess
    import sys
    import tarfile
    import time

    from huggingface_hub import HfFileSystem

    return (HfFileSystem, hashlib, json, os, shutil, sqlite3, subprocess, sys, tarfile, time)


@app.cell
def store_token():
    # --- HF auth for the private store bucket ---
    # The sandbox's own molab account token is tried first (/marimo/.env holds
    # HF_TOKEN — the same token the sidecar launcher reads to download the FP8
    # checkpoint); the password widget below overrides it. No token literal is
    # ever written into a cell: notebook cells are exported to the public GitHub
    # repo.
    #
    # The widget lives in its OWN cell because marimo forbids reading a
    # UIElement's value in the cell that created it (RuntimeError: "Accessing the
    # value of a UIElement in the cell that created it is not allowed"). The next
    # cell reads it.
    import os as _os

    import marimo as _mo

    def _sandbox_token():
        _tok = (_os.environ.get("HF_TOKEN") or "").strip()
        if _tok:
            return _tok
        try:
            with open("/marimo/.env", encoding="utf-8") as _f:
                for _line in _f:
                    if _line.startswith("HF_TOKEN="):
                        return _line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        return ""

    TOKEN_FORM = _mo.ui.text(kind="password", label="HF read token (private store bucket)")
    AUTO_TOKEN = _sandbox_token()
    _note = (
        "using the sandbox's `/marimo/.env` token — paste one below to override it "
        "if that token cannot read the private bucket."
        if AUTO_TOKEN
        else "no sandbox token found — paste a read token for the private bucket."
    )
    _mo.vstack([_mo.md(f"**HF auth** {_note}"), TOKEN_FORM])  # noqa: B018 -- display
    return AUTO_TOKEN, TOKEN_FORM


@app.cell
def store_access(AUTO_TOKEN, TOKEN_FORM, json):
    # --- Private store bucket: manifest probe ---
    # A typed token wins over the sandbox one, so pasting a better token actually
    # takes effect (typing into the widget re-runs this cell).
    import marimo as _mo
    from huggingface_hub import HfApi as _HfApi, HfFileSystem as _HfFS

    STORE_BUCKET = "Nubula/paddock-private"
    STORE_MANIFEST_JSON = "store/manifest.json"
    STORE_TOKEN = TOKEN_FORM.value or AUTO_TOKEN

    def _probe(_token):
        """(ok, message | (summary, manifest)) for this token."""
        if not _token:
            return False, "no token"
        try:
            _who = _HfApi(token=_token).whoami().get("name")
            with _HfFS(token=_token).open(
                f"hf://buckets/{STORE_BUCKET}/{STORE_MANIFEST_JSON}"
            ) as _f:
                _man = json.load(_f)
            assert _man.get("bucket") == STORE_BUCKET, (
                f"manifest bucket mismatch: {_man.get('bucket')}"
            )
            return True, (f"token {_who} -> manifest ok (published {_man['published_at']})", _man)
        except Exception as _exc:
            return False, f"{type(_exc).__name__}: {_exc}"

    STORE_OK, _res = _probe(STORE_TOKEN)
    _out = (
        _mo.md(f"**[store] ok** {_res[0]}")
        if STORE_OK
        else _mo.md(
            f"**[store] unavailable** {_res}\n\n"
            "Publish with `mise upload-store`; paste an HF read token in the cell above "
            "if the sandbox token is not enough. This notebook is store-only: nothing "
            "works until the snapshot resolves."
        )
    )
    _out  # noqa: B018 -- cell must return the widget for display
    return STORE_BUCKET, STORE_MANIFEST_JSON, STORE_OK, STORE_TOKEN


@app.cell
def store_snapshot(
    HfFileSystem, STORE_BUCKET, STORE_MANIFEST_JSON, STORE_OK, STORE_TOKEN, hashlib, json, os, shutil, sqlite3, time
):
    # --- Download + verify the store snapshot ---
    # The published artifact is a VACUUM INTO snapshot (the live local db is WAL),
    # so size + sha256 come from the manifest and are checked BEFORE the file is
    # promoted into place: a partial store must never be left where tools.py
    # would read it. Cache key is (size, sha of the last verified download) — a
    # re-published store with an identical byte size still invalidates, and the
    # 938 MiB file is never re-hashed.
    STORE_PATH = "data/sqlite_gorgon.db"
    STORE_MANIFEST = {"published_at": "?", "store": {"path": "store/sqlite_gorgon.db"}, "tables": {}}
    if not STORE_OK:
        print("[store] skipped: no verified store token (see the [store] cell above)")
    else:
        _fs = HfFileSystem(token=STORE_TOKEN)
        with _fs.open(f"hf://buckets/{STORE_BUCKET}/{STORE_MANIFEST_JSON}") as _f:
            STORE_MANIFEST = json.load(_f)
        _entry = STORE_MANIFEST["store"]
        _part = STORE_PATH + ".part"
        _marker = STORE_PATH + ".sha256"
        _marker_sha = ""
        if os.path.exists(_marker):
            with open(_marker, encoding="utf-8") as _f:
                _marker_sha = _f.read().strip()
        _cached = (
            os.path.exists(STORE_PATH)
            and os.path.getsize(STORE_PATH) == _entry["bytes"]
            and _marker_sha == _entry["sha256"]
        )
        if _cached:
            print(
                f"[store] cached {_entry['bytes']:,} bytes (published {STORE_MANIFEST['published_at']})"
            )
        else:
            os.makedirs(os.path.dirname(STORE_PATH) or ".", exist_ok=True)
            if os.path.exists(_part):
                os.remove(_part)
            _digest = hashlib.sha256()
            _done = 0
            _mark = 200 * 1024 * 1024
            _t_prev = time.time()
            with _fs.open(f"hf://buckets/{STORE_BUCKET}/{_entry['path']}", "rb") as _fin, open(
                _part, "wb"
            ) as _fout:
                while True:
                    _chunk = _fin.read(4 * 1024 * 1024)
                    if not _chunk:
                        break
                    _fout.write(_chunk)
                    _digest.update(_chunk)
                    _done += len(_chunk)
                    if _done >= _mark:
                        print(
                            f"[store] {_done / 2**20:,.0f} MiB / {_entry['bytes'] / 2**20:,.0f} MiB "
                            f"({_done / 2**20 / max(time.time() - _t_prev, 1e-6):.1f} MiB/s)"
                        )
                        _mark += 200 * 1024 * 1024
            _size_ok = _done == _entry["bytes"]
            _sha_ok = _digest.hexdigest() == _entry["sha256"]
            if not (_size_ok and _sha_ok):
                os.remove(_part)
                raise RuntimeError(
                    f"[store] snapshot mismatch (size_ok={_size_ok}, sha_ok={_sha_ok}); "
                    f"removed {_part} — re-run `mise upload-store` if the bucket is stale"
                )
            os.replace(_part, STORE_PATH)
            with open(_marker, "w", encoding="utf-8") as _f:
                _f.write(_entry["sha256"])
            print(f"[store] downloaded {_done:,} bytes, sha256 ok")

        _uri = "file:" + os.path.abspath(STORE_PATH).replace("\\", "/") + "?mode=ro"
        _conn = sqlite3.connect(_uri, uri=True)
        try:
            _check = _conn.execute("PRAGMA quick_check").fetchone()[0]
            print(f"[store] quick_check: {_check}")
            for _table in ("wiki_pages", "chat_events", "stall_events"):
                _want = (STORE_MANIFEST.get("tables") or {}).get(_table)
                if _want is None:
                    continue
                _got = _conn.execute(f"SELECT COUNT(*) FROM {_table}").fetchone()[0]
                print(f"[store] {_table}: {_got:,} vs manifest {_want:,}")
        finally:
            _conn.close()
        if _check != "ok":
            raise RuntimeError(f"[store] quick_check failed: {_check}")
    return STORE_MANIFEST, STORE_PATH


@app.cell
def corpus_docs(HfFileSystem, json, os, shutil):
    # --- Public corpus: only data/documents.json is needed here ---
    # corpus_search reads data/tool_documents.json + tool_bm25.pkl, which the
    # next cell builds FROM this file with the tarball's own
    # scripts/build_tool_corpus.py. The in-kernel lexical index lives in the
    # corpus-chat notebook (notebooks/molab-mirror) and is not built here.
    # Reading the file back as JSON is the integrity check (a truncated download
    # cannot parse) — the public bucket carries no manifest.
    _DOCS_SRC = "buckets/Nubula/paddock/documents.json"
    _LOCAL_DOCS = "data/documents.json"
    DOC_COUNT = 0
    if os.path.exists(_LOCAL_DOCS):
        with open(_LOCAL_DOCS, encoding="utf-8") as _f:
            DOC_COUNT = len(json.load(_f))
        print(f"[corpus] cached {DOC_COUNT:,} docs ({os.path.getsize(_LOCAL_DOCS):,} B)")
    else:
        os.makedirs("data", exist_ok=True)
        with HfFileSystem().open(f"hf://{_DOCS_SRC}", "rb") as _fin, open(_LOCAL_DOCS, "wb") as _fout:
            shutil.copyfileobj(_fin, _fout)
        with open(_LOCAL_DOCS, encoding="utf-8") as _f:
            DOC_COUNT = len(json.load(_f))
        print(f"[corpus] downloaded {DOC_COUNT:,} docs ({os.path.getsize(_LOCAL_DOCS):,} B)")
    DOCS_OK = DOC_COUNT > 0
    return DOC_COUNT, DOCS_OK


@app.cell
def pgrag_src(
    DOCS_OK, HfFileSystem, STORE_BUCKET, STORE_MANIFEST, STORE_OK, STORE_TOKEN, hashlib, json, os, shutil, subprocess, sys, tarfile, time
):
    # --- pgrag source + tool corpus, in-sandbox ---
    # molab imports this notebook file from GitHub: there is no repo checkout and
    # pgrag is not installed, so the loop source ships in the private bucket and
    # is imported straight off the extracted tree. It needs only stdlib +
    # requests — and the sandbox kernel is Python 3.13, which is why
    # src/pgrag/agentic/tools.py keeps parenthesized `except` clauses (bare
    # `except A, B:` is 3.14-only and made the loop unimportable here).
    SRC_DIR = "pg-rag-src"
    _TARBALL = "pgrag-src.tar.gz"
    _DOCS = "data/tool_documents.json"
    _PKL = "data/tool_bm25.pkl"
    run_loop = None
    if not STORE_OK:
        print("[src] skipped: no verified store token (see the [store] cell above)")
    elif not DOCS_OK:
        print("[src] skipped: no data/documents.json to build the tool corpus from")
    else:
        _tar_entry = STORE_MANIFEST["source_tar"]
        _fs = HfFileSystem(token=STORE_TOKEN)
        if not (os.path.exists(_TARBALL) and os.path.getsize(_TARBALL) == _tar_entry["bytes"]):
            with _fs.open(f"hf://buckets/{STORE_BUCKET}/{_tar_entry['path']}", "rb") as _fin, open(
                _TARBALL, "wb"
            ) as _fout:
                shutil.copyfileobj(_fin, _fout)
        with open(_TARBALL, "rb") as _f:
            _tar_sha = hashlib.sha256(_f.read()).hexdigest()
        if _tar_sha != _tar_entry["sha256"]:
            raise RuntimeError("[src] source tarball sha256 mismatch — re-run `mise upload-store`")
        # Extraction is keyed on the tarball's sha, so a re-publish inside one
        # sandbox session re-extracts instead of importing the stale tree.
        _sha_file = os.path.join(SRC_DIR, ".src-sha256")
        _extracted_sha = ""
        if os.path.exists(_sha_file):
            with open(_sha_file, encoding="utf-8") as _f:
                _extracted_sha = _f.read().strip()
        if _extracted_sha != _tar_sha or not os.path.exists(os.path.join(SRC_DIR, "src", "pgrag")):
            shutil.rmtree(SRC_DIR, ignore_errors=True)
            with tarfile.open(_TARBALL) as _tf:
                _tf.extractall(SRC_DIR, filter="data")
            with open(_sha_file, "w", encoding="utf-8") as _f:
                _f.write(_tar_sha)
            for _mod in [m for m in sys.modules if m == "pgrag" or m.startswith("pgrag.")]:
                del sys.modules[_mod]
        sys.path.insert(0, os.path.abspath(os.path.join(SRC_DIR, "src")))
        import pgrag.agentic.loop as _loop_mod

        assert _loop_mod.DEFAULT_MAX_ROUNDS == 6
        print(
            f"[src] pgrag {_tar_entry['git_head']} imported "
            f"(worktree_dirty={_tar_entry.get('worktree_dirty')})"
        )
        if not (os.path.exists(_DOCS) and os.path.exists(_PKL)):
            _t0 = time.time()
            print("[tools] building the tool-corpus BM25 in-sandbox (data/documents.json)")
            subprocess.run(
                [sys.executable, os.path.join(SRC_DIR, "scripts", "build_tool_corpus.py")],
                check=True,
            )
            print(f"[tools] built in {time.time() - _t0:.0f}s")
        with open(_DOCS, encoding="utf-8") as _f:
            print(f"[tools] corpus {len(json.load(_f)):,} docs kept -> {_PKL}")
        run_loop = _loop_mod.run_loop
    return SRC_DIR, run_loop


@app.cell
def sidecar_llm(SRC_DIR, STORE_OK, json, os, run_loop, shutil, subprocess, time):
    # --- vLLM sidecar: mandatory here, auto-started when absent ---
    # This notebook has no torch, so the sidecar is the only engine. When it is
    # not already answering at :8000 the launcher that ships in the extracted
    # tarball is used (`pg-rag-src/scripts/molab_vllm_launch.sh`: isolated venv +
    # FP8 checkpoint + serve, ~6 min on a cold sandbox, one bounded block as
    # molab requires). It also passes --max-model-len 24576 and
    # --enable-auto-tool-choice with a tool-call parser discovered in-sandbox —
    # see docs/MOLAB_OPS.md "Tool-loop sidecar config".
    import re as _re
    import urllib.request as _urlreq

    LLM_URL = "http://127.0.0.1:8000/v1"
    _LAUNCH_LOG = "/tmp/launch.log"
    _WAIT_S = 900  # the launcher blocks on the 30.9 GB checkpoint download
    _POLL_S = 5

    def _server_model():
        try:
            with _urlreq.urlopen(LLM_URL + "/models", timeout=3) as _resp:
                return json.loads(_resp.read().decode())["data"][0]["id"]
        except Exception:
            return None

    def _gpu_present():
        # molab's default sandbox is CPU-only (no /dev/nvidia*, no nvidia-smi);
        # launching the sidecar there would burn the whole 15-min wait before
        # saying so. Attaching a GPU RECREATES the sandbox at a new URL.
        return bool(shutil.which("nvidia-smi")) or os.path.exists("/dev/nvidia0")

    SERVER_MODEL = _server_model()
    if SERVER_MODEL:
        print(f"[sidecar] already up at {LLM_URL} (model: {SERVER_MODEL})")
    elif not (STORE_OK and run_loop and SRC_DIR):
        print("[sidecar] not started: the store + source tarball must resolve first")
    elif not _gpu_present():
        print(
            "[sidecar] no GPU in this sandbox (no nvidia-smi, no /dev/nvidia0) -> not "
            "launching. Attach one via the notebook specs button first; that recreates "
            "the sandbox at a NEW url, so re-open this notebook there and run-all."
        )
    else:
        _launcher = os.path.join(SRC_DIR, "scripts", "molab_vllm_launch.sh")
        print(f"[sidecar] down -> launching {_launcher} (~6 min)")
        subprocess.run(["bash", "-lc", f"bash {_launcher} > {_LAUNCH_LOG} 2>&1"], check=False)
        try:
            with open(_LAUNCH_LOG, encoding="utf-8", errors="replace") as _f:
                _tail = _f.read().splitlines()[-12:]
            print(f"[sidecar] launcher log tail ({_LAUNCH_LOG}):")
            for _line in _tail:
                print(f"  {_line}")
        except OSError as _exc:
            print(f"[sidecar] no launcher log ({_exc})")
        _deadline = time.time() + _WAIT_S
        while not SERVER_MODEL and time.time() < _deadline:
            SERVER_MODEL = _server_model()
            if not SERVER_MODEL:
                time.sleep(_POLL_S)
        if SERVER_MODEL:
            print(f"[sidecar] model: {SERVER_MODEL}")
        else:
            # A bare tail is not enough: vLLM's fatal line is usually
            # "Engine core initialization failed ... See root cause above", and the
            # root cause is an EARLIER block in the same file. So print the first
            # traceback region, every error-ish line, and only then the tail.
            print(f"[sidecar] still not answering after {_WAIT_S}s — digesting /tmp/vllm.log:")
            try:
                with open("/tmp/vllm.log", encoding="utf-8", errors="replace") as _f:
                    _lines = _f.read().splitlines()
                _pat = _re.compile(
                    r"(?i)error|exception|traceback|out of memory|\boom\b|no available memory|"
                    r"failed|refused|invalid|unsupported|not enough|too large|assert"
                )
                print(f"  log: {len(_lines)} lines")
                _start = next((i for i, _l in enumerate(_lines) if "Traceback" in _l), None)
                if _start is not None:
                    print(f"  first traceback (line {_start + 1}) — the cause is usually in this block:")
                    for _line in _lines[_start : _start + 18]:
                        print(f"    {_line[:200]}")
                _hits = [_l for _l in _lines if _pat.search(_l)]
                print(f"  error-ish lines ({len(_hits)}):")
                for _line in _hits[:20]:
                    print(f"    {_line[:200]}")
                print("  tail:")
                for _line in _lines[-12:]:
                    print(f"    {_line[:200]}")
            except OSError as _exc:
                print(f"  (no /tmp/vllm.log: {_exc})")
            print(
                "  Retry knobs (env vars, no file edit needed): PGRAG_VLLM_MAXLEN=16384 "
                "and/or PGRAG_VLLM_MTP=0 for OOM; PGRAG_VLLM_EAGER=1 for the shortest "
                "boot (no torch.compile / no graph capture). Set them in this kernel "
                "(os.environ) or prefix the launcher call, then re-run this cell. "
                "Full log: /tmp/vllm.log"
            )
    return LLM_URL, SERVER_MODEL


@app.cell
def tools_protocol(LLM_URL, SERVER_MODEL, json):
    # --- Decide the tool-call protocol empirically, never by guesswork ---
    # vLLM accepts a `tools=` payload only when started with
    # --enable-auto-tool-choice plus a matching --tool-call-parser (the launcher
    # discovers the parser name in-sandbox and passes both). When the probe gets
    # no tool_calls back, or the request is rejected, the loop falls back to its
    # fenced ```tool text protocol (PGRAG_LLM_NATIVE_TOOLS=0, set by the chat
    # cell from this result).
    import urllib.request as _urlreq

    NATIVE_TOOLS = "0"
    if not SERVER_MODEL:
        print("[loop] no sidecar model -> protocol probe skipped")
    else:
        _payload = json.dumps(
            {
                "model": SERVER_MODEL,
                "messages": [{"role": "user", "content": "What is 2+2? Use the echo tool."}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "description": "Echo the given text back.",
                            "parameters": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                            },
                        },
                    }
                ],
                "max_tokens": 64,
                "chat_template_kwargs": {"enable_thinking": False},
            }
        ).encode()
        _req = _urlreq.Request(
            LLM_URL + "/chat/completions",
            data=_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _urlreq.urlopen(_req, timeout=120) as _resp:
                _msg = json.loads(_resp.read().decode())["choices"][0]["message"]
            _calls = _msg.get("tool_calls") or []
            if _calls:
                NATIVE_TOOLS = "1"
                print(f"[loop] native tool calls OK ({_calls[0].get('function', {}).get('name')})")
            else:
                print(
                    "[loop] probe returned no tool_calls -> falling back to the fenced ```tool "
                    "text protocol"
                )
        except Exception as _exc:
            print(
                f"[loop] tools probe failed ({type(_exc).__name__}: {_exc}) -> falling back to the "
                "fenced ```tool text protocol"
            )
    return (NATIVE_TOOLS,)


@app.cell
def chat(LLM_URL, NATIVE_TOOLS, SERVER_MODEL, STORE_PATH, STORE_OK, run_loop):
    # --- Chat: every question goes through the production tool loop ---
    # run_loop is pgrag.agentic.loop.run_loop imported from the published source
    # tarball; the endpoint/model/protocol are handed to it per call (loop._post
    # reads those env vars at call time), so re-runs and re-publishes need no
    # reload.
    import os as _os
    import time as _time

    import marimo as mo

    def generate(messages, config):
        _missing = [
            _what
            for _what, _ok in (
                ("the private store snapshot", STORE_OK),
                ("the pgrag source tarball", run_loop is not None),
                (
                    "the vLLM sidecar (auto-launched above; logs /tmp/launch.log, /tmp/vllm.log)",
                    bool(SERVER_MODEL),
                ),
            )
            if not _ok
        ]
        if _missing:
            yield (
                "Tool-loop chat needs "
                + ", ".join(_missing)
                + ". Check the cell output above, then re-run the cells in order."
            )
            return
        _os.environ["PGRAG_LLM_URL"] = LLM_URL + "/chat/completions"
        _os.environ["PGRAG_LLM_MODEL"] = SERVER_MODEL
        _os.environ["PGRAG_LLM_NATIVE_TOOLS"] = NATIVE_TOOLS
        _question = messages[-1].content
        _history = [{"role": _m.role, "content": _m.content} for _m in messages[:-1]]
        _trace = {}
        _t0 = _time.time()
        _res = run_loop(
            _question,
            history=_history or None,
            corpus="tool",
            store_path=STORE_PATH,
            trace=_trace,
        )
        _dt = _time.time() - _t0
        print(f"[loop] rounds={_res['rounds']} {_dt:.1f}s trace={_res['trace_rounds']}")
        yield f"{_res['answer']}\n\n_(tool loop: {_res['rounds']} round(s), {_dt:.1f}s)_"

    chat = mo.ui.chat(generate, show_configuration_controls=False, max_height=600)
    chat  # noqa: B018 -- cell must return the widget for display
    return


if __name__ == "__main__":
    app.run()
