# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "accelerate==1.14.0",
#     "huggingface-hub==1.31.0",
#     "kernels==0.16.1",
#     "kernels-data==0.16.1",
#     "marimo[mcp]>=0.24.0",
#     "mcp>=1",
#     "peft==0.21.0",
#     "pydantic>=2",
#     "python-lsp-ruff==2.3.3",
#     "python-lsp-server==1.15.0",
#     "requests>=2.34.2",
#     "ruff==0.16.7",
#     "safetensors==0.8.0",
#     "sigstore==4.5.0",
#     "sigstore-models==0.0.6",
#     "sigstore-rekor-types==0.0.18",
#     "sse-starlette==3.4.11",
#     "starlette==1.6.0",
#     "tokenizers==0.23.2",
#     "torch==2.14.0",
#     "tqdm==4.70.0",
#     "transformers==5.17.0",
#     "urllib3==2.7.0",
#     "websockets==17.1",
#     "xxhash==4.0.1",
#     "yarl==1.24.5",
# ]
# ///

import marimo

__generated_with = "0.24.0"
app = marimo.App(width="medium", auto_download=["html"])

with app.setup(hide_code=True):
    import subprocess as _subprocess

    # ---- Environment repair (setup runs FIRST, before any torch-importing cell) ----
    # molab sandboxes rotate; the image torch may be stale vs the driver
    # (driver CUDA 13.2 here). Probe in a SUBPROCESS so the kernel never imports a
    # broken torch (which would poison sys.modules for the whole session).
    # kernels CONSTRAINED <0.17 (re-verified): transformers 5.17.0 hard-rejects
    # kernels>=0.17 at set_use_kernels. einops kept: the kernels 0.16.x mamba-ssm
    # hub path needs it. torchvision MUST match venv torch ABI-wise: transformers
    # imports it at model-load time, and the IMAGE torchvision (compiled for image
    # torch) dies with "operator torchvision::nms does not exist" once venv torch
    # shadows it (seen live).
    _PROBE_CODE = "import torch; assert torch.cuda.is_available(); print(torch.__version__ + '_' + torch.version.cuda.replace('.', ''))"
    _r = _subprocess.run(
        ["/tmp/uv-venv/bin/python", "-c", _PROBE_CODE],
        capture_output=True,
        text=True,
        timeout=120,
    )
    _out = (_r.stdout or "").strip()
    _REPAIR_CMD = (
        "uv pip install -U "
        "transformers 'kernels<0.17' marimo[recommended] huggingface-hub transformers torch torchvision einops peft "
        "--torch-backend=auto -p /tmp/uv-venv/bin/python"
    )
    if _r.returncode == 0 and _out.startswith("2.14.0+cu132_"):
        print("[ENV] torch 2.14.0+cu132 OK - no repair needed")
    else:
        _why = (
            "torch not importable (rc=%s)" % _r.returncode
            if _r.returncode != 0
            else "torch=%r" % _out
        )
        print("[ENV] env mismatch (%s) -> repairing" % _why)
        _rr = _subprocess.run(
            _REPAIR_CMD,
            shell=True,
            capture_output=True,
            text=True,
            timeout=900,
        )
        print("[ENV] repair rc=%s" % _rr.returncode)
        if _rr.returncode != 0:
            print("[ENV] repair stderr tail:", (_rr.stderr or "")[-300:])
        print(
            "[ENV] NOTE: after repair, restart the session (UI) so no stale torch "
            "sits in this kernel's sys.modules"
        )
    print("[ENV] env-check complete")


@app.cell
def imports():
    import collections, hashlib, json, math, os, re, shutil, sqlite3
    import subprocess, sys, tarfile, time, torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from huggingface_hub import HfFileSystem

    # Please leave this comment
    # uv pip install -U "transformers kernels<0.17.0" marimo[recommended] huggingface-hub transformers torch --torch-backend=auto
    return (HfFileSystem, collections, hashlib, json, math, os, re, shutil, sqlite3, subprocess, sys, tarfile, time, torch)


@app.cell
def rag_index(HfFileSystem, collections, json, math, re):
    # --- Retrieval over the Project Gorgon corpus (BM25-style lexical scoring) ---
    # Corpus is the same documents.json the local pipeline builds; downloaded from the
    # HF bucket so molab never needs the local repo.
    _BUCKET = "buckets/Nubula/paddock/documents.json"
    _LOCAL_DOCS = "data/documents.json"

    def _load_corpus():
        import os as _os

        if _os.path.exists(_LOCAL_DOCS):
            with open(_LOCAL_DOCS) as _f:
                raw = json.load(_f)
            print(
                f"[corpus] loaded {len(raw):,} docs from local {_LOCAL_DOCS}"
            )
            return raw
        _os.makedirs(_LOCAL_DOCS.rsplit("/", 1)[0], exist_ok=True)
        fs = HfFileSystem()
        with fs.open(f"hf://{_BUCKET}", "rb") as _f:
            raw = json.load(_f)
        with open(_LOCAL_DOCS, "w") as _f:
            json.dump(raw, _f)
        print(
            f"[corpus] downloaded {len(raw):,} docs from HF bucket (cached to {_LOCAL_DOCS})"
        )
        return raw

    _STOP = set(
        (
            "the a an and or of to in on at for with from by as is are was were be been it its this that these"
            " those you your i me my we our he she they them what which who whom whose when where why how if"
            " then than so such not no can will would could should may might must do does did have has had use"
            " uses using used get gets getting got"
        ).split()
    )

    _TYPE_PRIOR = {
        "summary": 6,
        "skill": 5,
        "ability": 5,
        "recipe": 5,
        "skillprofile": 5,
        "leveling": 5,
        "lorebook": 4,
        "curated": 4,
        "item": 4,
        "npc": 4,
        "quest": 4,
        "effect": 4,
        "combatxp": 4,
        "xptable": 4,
        "mechanic": 4,
        "schema": 3,
        "enum": 3,
        "area": 3,
        "attribute": 3,
        "directedgoal": 3,
        "itemuse": 3,
        "abilitykeyword": 3,
        "advancementtable": 3,
        "ai": 3,
        "landmark": 3,
        "source": 2,
        "title": 2,
        "tsys": 2,
        "vault": 2,
        "wiki": 1,
    }

    def _is_wiki_fragment(name):
        n = (name or "").lower()
        return any(
            x in n
            for x in (
                "_table_",
                "_coverage",
                "_uses",
                " row_",
                " row ",
                "_row",
            )
        )

    def _tok(s):
        return [
            t
            for t in re.findall(r"[a-z0-9]+", (s or "").lower())
            if len(t) >= 2 and t not in _STOP
        ]

    _raw_docs = _load_corpus()
    _docs = [
        (
            d["id"],
            d.get("type", ""),
            (d.get("metadata") or {}).get("name", ""),
            d.get("text", ""),
        )
        for d in _raw_docs
    ]
    _name_sets = []
    _priors = []
    _index = collections.defaultdict(set)
    for _i, (_did, _dt, _name, _text) in enumerate(_docs):
        _ns = frozenset(_tok(_name))
        _name_sets.append(_ns)
        _p = _TYPE_PRIOR.get(_dt, 1)
        if _is_wiki_fragment(_name):
            _p *= 0.15
        _priors.append(_p)
        for _t in _tok(_name + " " + _text[:600]):
            _index[_t].add(_i)

    print(f"[corpus] indexed {len(_docs):,} docs")

    def retrieve(question, K=10, max_chars=1200, budget=16000):
        N = len(_docs)
        qtoks = list(dict.fromkeys(_tok(question)))
        df = {t: len(_index.get(t, ())) for t in qtoks}
        cand = set()
        for t in qtoks:
            cand.update(_index.get(t, ()))
        scored = []
        for i in cand:
            ns = _name_sets[i]
            s = 0.0
            for t in qtoks:
                dft = df.get(t, 0)
                if not dft:
                    continue
                idf = math.log(1.0 + (N - dft + 0.5) / (dft + 0.5))
                tfscore = 3.0 if t in ns else 1.0
                s += idf * tfscore
            s *= _priors[i]
            scored.append((s, i))
        scored.sort(key=lambda x: -x[0])
        parts = []
        for s, i in scored[:K]:
            did, dt, name, text = _docs[i]
            snip = (
                text if len(text) <= max_chars else text[:max_chars] + " ..."
            )
            parts.append(f"[{name} ({dt})]\n{snip}")
        return "\n\n".join(parts)[:budget]

    return (retrieve,)


@app.cell
def store_access(json):
    # --- Private store bucket: auth + manifest probe (tool-loop mode) ---
    # The agentic tool-loop store (the user's own chat logs / session history)
    # lives in a PRIVATE HF bucket, never the public corpus bucket. The
    # sandbox's own molab account token is tried first (/marimo/.env holds
    # HF_TOKEN — the same token molab uses to download the FP8 checkpoint);
    # a password widget is the fallback. No token literal is ever written into
    # a cell: notebook cells are exported to the public GitHub repo.
    import marimo as _mo
    import os as _os

    from huggingface_hub import HfApi as _HfApi, HfFileSystem as _HfFS

    STORE_BUCKET = "Nubula/paddock-private"
    STORE_MANIFEST_JSON = "store/manifest.json"

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

    _tok_form = _mo.ui.text(kind="password", label="HF read token (private store bucket)")
    STORE_TOKEN = _sandbox_token() or _tok_form.value

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

    _ok, _res = _probe(STORE_TOKEN)
    if not _ok:
        STORE_TOKEN = _tok_form.value
        _ok, _res = _probe(STORE_TOKEN)
    STORE_OK = _ok
    _out = (
        _mo.md(f"**[store] ok** {_res[0]}")
        if _ok
        else _mo.vstack(
            [
                _mo.md(
                    f"**[store] unavailable** {_res}\n\n"
                    "Publish with `mise upload-store`; paste an HF read token above if the "
                    "sandbox token is not enough. Tool-loop mode stays disabled, corpus "
                    "chat keeps working."
                ),
                _tok_form,
            ]
        )
    )
    _out  # noqa: B018 -- cell must return the widget for display
    return STORE_BUCKET, STORE_MANIFEST_JSON, STORE_OK, STORE_TOKEN


@app.cell
def store_snapshot(HfFileSystem, STORE_MANIFEST_JSON, STORE_BUCKET, STORE_OK, STORE_TOKEN, hashlib, json, os, shutil, sqlite3, time):
    # --- Download + verify the private store snapshot (tool-loop mode needs it) ---
    # The published artifact is a VACUUM INTO snapshot: the live local db is WAL,
    # so size/sha256 come from the manifest and are checked BEFORE the store is
    # promoted into place — a partial file must never be left where tools.py
    # would read it. No mo.stop here: this cell always reaches its return, so the
    # corpus-chat path keeps working when the store is unavailable.
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
        # Cache key is (size, sha from the last verified download): a re-published
        # store with the same byte size must still invalidate, and re-hashing the
        # 938 MiB file on every run is exactly the cost the marker avoids.
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
def pgrag_src(HfFileSystem, STORE_MANIFEST, STORE_BUCKET, STORE_OK, STORE_TOKEN, hashlib, json, os, retrieve, shutil, subprocess, sys, tarfile, time):
    # --- pgrag source + tool corpus, in-sandbox ---
    # molab loads THIS notebook from public GitHub: there is no repo checkout and
    # pgrag is not installed, so the loop source ships in the private bucket and
    # is imported straight off the extracted tree. It needs only stdlib +
    # requests — and the sandbox kernel is Python 3.13, so agentic/tools.py keeps
    # parenthesized `except` clauses (bare `except A, B:` is 3.14-only and made
    # the whole loop unimportable here).
    _SRC_DIR = "pg-rag-src"
    _TARBALL = "pgrag-src.tar.gz"
    _DOCS = "data/tool_documents.json"
    _PKL = "data/tool_bm25.pkl"
    run_loop = None
    if not STORE_OK:
        print("[src] skipped: no verified store token (see the [store] cell above)")
    else:
        # Ordering dependency, not decoration: rag_index writes data/documents.json,
        # which the tool-corpus build below reads.
        assert callable(retrieve), "rag_index must run before pgrag_src"
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
        _sha_file = os.path.join(_SRC_DIR, ".src-sha256")
        _extracted_sha = ""
        if os.path.exists(_sha_file):
            with open(_sha_file, encoding="utf-8") as _f:
                _extracted_sha = _f.read().strip()
        if _extracted_sha != _tar_sha or not os.path.exists(os.path.join(_SRC_DIR, "src", "pgrag")):
            shutil.rmtree(_SRC_DIR, ignore_errors=True)
            with tarfile.open(_TARBALL) as _tf:
                _tf.extractall(_SRC_DIR, filter="data")
            with open(_sha_file, "w", encoding="utf-8") as _f:
                _f.write(_tar_sha)
            for _mod in [m for m in sys.modules if m == "pgrag" or m.startswith("pgrag.")]:
                del sys.modules[_mod]
        sys.path.insert(0, os.path.abspath(os.path.join(_SRC_DIR, "src")))
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
                [sys.executable, os.path.join(_SRC_DIR, "scripts", "build_tool_corpus.py")],
                check=True,
            )
            print(f"[tools] built in {time.time() - _t0:.0f}s")
        with open(_DOCS, encoding="utf-8") as _f:
            print(f"[tools] corpus {len(json.load(_f)):,} docs kept -> {_PKL}")
        run_loop = _loop_mod.run_loop
    return (run_loop,)


@app.cell
def model_load():
    # --- Engine: prefer the local vLLM sidecar (:8000); fall back to transformers ---
    # vLLM (FP8, CUDA graphs, paged attention, prefix cache) decodes ~3-6x faster
    # than eager transformers on the same GPU. The sidecar is OPTIONAL: when it is
    # not running, this cell loads the bf16 model in-process exactly as before.
    import json as _json
    import urllib.request as _urlreq

    import torch as _torch
    from transformers import (
        AutoModelForCausalLM as _AutoModel,
        AutoTokenizer as _AutoTok,
    )

    _MODEL_NAME = "Qwen/Qwen3.8-27B"
    _FALLBACK_MODEL_NAME = "unsloth/Qwen3.8-27B-unsloth-bnb-4bit"
    _LLM_URL = "http://127.0.0.1:8000/v1"

    def _probe_server(url, timeout=2.0):
        try:
            with _urlreq.urlopen(url + "/models", timeout=timeout) as _resp:
                if _resp.status == 200:
                    _data = _json.loads(_resp.read().decode())
                    return _data["data"][0]["id"]
        except Exception:
            return None
        return None

    server_model = _probe_server(_LLM_URL)
    model = None
    tokenizer = None

    if server_model:
        print(f"[Engine] vLLM sidecar UP at {_LLM_URL} (model: {server_model})")
        tokenizer = _AutoTok.from_pretrained(_MODEL_NAME)
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token
    else:
        # Non-sidecar production config: unsloth-bnb-4bit NF4 in-process.
        # Same eager speed as bf16 (15.0-15.3 vs 15.4-17.3 tok/s measured),
        # 21.7 GiB vs 55.6 GiB weights -> 2.5x smaller download+load, which
        # matters because molab currently kills sandboxes during long loads.
        from transformers import BitsAndBytesConfig as _BNB

        print(
            f"[Engine] no vLLM sidecar at {_LLM_URL} - loading {_FALLBACK_MODEL_NAME} (NF4) in-process"
        )
        _free0, _tot = _torch.cuda.mem_get_info()
        print(f"[Model Load] free before: {_free0 / 2**30:.1f} GiB")
        model = _AutoModel.from_pretrained(
            _FALLBACK_MODEL_NAME,
            attn_implementation="sdpa",
            device_map="cuda:0",
            use_kernels=True,
            quantization_config=_BNB(load_in_4bit=True),
        )
        model.eval()
        tokenizer = _AutoTok.from_pretrained(_MODEL_NAME)
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token
        _free1, _ = _torch.cuda.mem_get_info()
        print(
            f"[Model] {model.num_parameters():,} params loaded | free after: {_free1 / 2**30:.1f} GiB"
        )

    return model, tokenizer, server_model, _LLM_URL


@app.cell
def sidecar_tools_probe(_LLM_URL, json, server_model):
    # --- Decide the tool-call protocol empirically, never by guesswork ---
    # vLLM accepts a `tools=` payload only when it was started with
    # --enable-auto-tool-choice plus a matching --tool-call-parser
    # (scripts/molab_vllm_launch.sh discovers the parser name in-sandbox and
    # passes both). When the probe gets no tool_calls back, or the request is
    # rejected, the loop runs its fenced ```tool text protocol instead
    # (PGRAG_LLM_NATIVE_TOOLS=0, set by the chat cell from this result).
    import urllib.request as _urlreq

    _ECHO_TOOL = [
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
    ]
    NATIVE_TOOLS = "0"
    if not server_model:
        print("[loop] no vLLM sidecar: start it (scripts/molab_vllm_launch.sh) for tool-loop mode")
    else:
        _payload = json.dumps(
            {
                "model": server_model,
                "messages": [{"role": "user", "content": "What is 2+2? Use the echo tool."}],
                "tools": _ECHO_TOOL,
                "max_tokens": 64,
                "chat_template_kwargs": {"enable_thinking": False},
            }
        ).encode()
        _req = _urlreq.Request(
            _LLM_URL + "/chat/completions",
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
def chat(_LLM_URL, NATIVE_TOOLS, run_loop, STORE_OK, STORE_PATH, model, re, retrieve, server_model, tokenizer, torch):
    # --- Chat: RAG over the corpus, answered by the vLLM sidecar (or local model) ---
    import json as _json
    import urllib.request as _urlreq

    import marimo as mo

    SYSTEM_PROMPT = (
        "You are a Project Gorgon game assistant.\n"
        "Answer the user's question using the provided context.\n\n"
        "Rules:\n"
        "- Figure out what the user is really asking, and assemble the answer from the context.\n"
        "- Reason from the context: connect information across documents, compare and rank options, and draw conclusions that follow from the stated facts.\n"
        "- Include relevant names, skills, levels, ingredients, and quantities when available.\n"
        "- If multiple answers exist, list them.\n"
        "- If the context contains PARTIAL information, answer with exactly what is present and state what is missing.\n"
        "- NEVER fabricate facts, names, values, or mechanics not present in the context."
    )

    _BASE_PROMPT_CHARS = 4096  # matches the tokenizer truncation budget

    def _chat_completion_stream(prompt, max_tokens=512):
        # Server path: SSE generator for mo.ui.chat. vLLM streams the first
        # content delta in ~0.1 s, so text appears progressively instead of
        # after the full blocking decode.
        payload = _json.dumps(
            {
                "model": server_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": max_tokens,
                "stream": True,
                "chat_template_kwargs": {"enable_thinking": False},
            }
        ).encode()
        req = _urlreq.Request(
            _LLM_URL + "/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _urlreq.urlopen(req, timeout=600) as resp:
            for raw in resp:
                line = raw.strip()
                # skip blank/keep-alive/comment lines, [DONE], and the
                # role-only first chunk (no "content" key)
                if (
                    not line.startswith(b"data: ")
                    or line == b"data: [DONE]"
                ):
                    continue
                chunk = _json.loads(line[6:])
                delta = chunk["choices"][0]["delta"].get("content")
                if delta:
                    yield delta

    def _local_generate(prompt, max_tokens=512):
        text = tokenizer.apply_chat_template(
            [{"role": "user", "content": prompt}],
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
        enc = tokenizer(
            text, return_tensors="pt", truncation=True, max_length=4096
        )
        inp = enc["input_ids"].to("cuda")
        with torch.no_grad():
            out = model.generate(
                inp,
                max_new_tokens=max_tokens,
                do_sample=False,
                eos_token_id=tokenizer.eos_token_id,
                pad_token_id=tokenizer.pad_token_id,
            )
        answer = tokenizer.decode(
            out[0][inp.shape[1] :], skip_special_tokens=True
        ).strip()
        return re.sub(
            r"^\s*(answer|response)\s*[:\uff1a]\s*", "", answer, flags=re.I
        )

    def generate(messages, config):
        question = messages[-1].content
        if _mode.value == "tool loop":
            if not (STORE_OK and server_model and run_loop):
                yield (
                    "Tool-loop mode needs the private store snapshot and the vLLM sidecar "
                    "(scripts/molab_vllm_launch.sh); see the [store]/[src] cell output above. "
                    "Switch back to 'corpus chat' for the single-shot path."
                )
                return
            import os as _os
            import time as _time

            # Read at call time by loop._post, so re-runs and mode switches need
            # no reload: the endpoint/model/protocol are this cell's business.
            _os.environ["PGRAG_LLM_URL"] = _LLM_URL + "/chat/completions"
            _os.environ["PGRAG_LLM_MODEL"] = server_model
            _os.environ["PGRAG_LLM_NATIVE_TOOLS"] = NATIVE_TOOLS
            _history = [{"role": _m.role, "content": _m.content} for _m in messages[:-1]]
            _trace = {}
            _t0 = _time.time()
            _res = run_loop(
                question,
                history=_history or None,
                corpus="tool",
                store_path=STORE_PATH,
                trace=_trace,
            )
            _dt = _time.time() - _t0
            print(f"[loop] rounds={_res['rounds']} {_dt:.1f}s trace={_res['trace_rounds']}")
            yield f"{_res['answer']}\n\n_(tool loop: {_res['rounds']} round(s), {_dt:.1f}s)_"
            return
        context = retrieve(question)
        prompt = (
            SYSTEM_PROMPT
            + "\n\nContext:\n"
            + context
            + "\n\nQuestion: "
            + question
        )
        if server_model:
            yield from _chat_completion_stream(prompt)
        else:
            # local fallback: one chunk, same cleanup as before
            yield _local_generate(prompt)

    _mode = mo.ui.radio(
        options=["corpus chat", "tool loop"], value="corpus chat", label="Mode"
    )
    chat = mo.ui.chat(
        generate, show_configuration_controls=False, max_height=600
    )
    mo.vstack([_mode, chat])  # noqa: B018 -- cell must return the widgets for display
    return


if __name__ == "__main__":
    app.run()
