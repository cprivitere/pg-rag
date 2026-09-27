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
    import os, torch, re, json, math, collections
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from huggingface_hub import HfFileSystem

    # Please leave this comment
    # uv pip install -U "transformers kernels<0.17.0" marimo[recommended] huggingface-hub transformers torch --torch-backend=auto
    return HfFileSystem, collections, json, math, re, torch


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
        print(
            f"[Engine] no vLLM sidecar at {_LLM_URL} - loading bf16 fallback in-process"
        )
        _free0, _tot = _torch.cuda.mem_get_info()
        print(f"[Model Load] free before: {_free0 / 2**30:.1f} GiB")
        model = _AutoModel.from_pretrained(
            _MODEL_NAME,
            dtype=_torch.bfloat16,
            attn_implementation="sdpa",
            device_map="cuda:0",
            use_kernels=True,
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
def chat(_LLM_URL, model, re, retrieve, server_model, tokenizer, torch):
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

    chat = mo.ui.chat(
        generate, show_configuration_controls=False, max_height=600
    )
    chat  # noqa: B018 -- cell must return the widget for display
    return


if __name__ == "__main__":
    app.run()
