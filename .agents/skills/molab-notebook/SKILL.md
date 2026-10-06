---
name: molab-notebook
description: >-
  Pair on a molab-hosted marimo notebook (sb-*.sb.molab.run) without
  re-pasting connection details: caches per-sandbox URL + token, delegates
  execution to the bundled execute-code.ps1 script, and handles sandbox
  moves (GPU attach recreates the sandbox at a new URL). Use this INSTEAD
  OF generic marimo-pair skills whenever the notebook URL is sb-*.sb.molab.run
  (a molab sandbox) — a pasted "Connect to the notebook at: https://sb-…"
  block is this skill's trigger.
allowed-tools: Bash(powershell **/scripts/molab-connect.ps1 *), Read
---

# molab-notebook

Pair on a molab-hosted marimo notebook without re-pasting connection details.

molab sandboxes issue a per-sandbox URL (`https://sb-XXXX.sb.molab.run/`) and a
64-hex-char bearer token. The URL changes whenever molab recreates the
sandbox (GPU attach, sandbox restart), and the token regenerates with it.

Connection info is cached per sandbox id in
`%USERPROFILE%\.marimo\molab-connections.json`.

All code execution goes through `execute-code.ps1` in this skill's scripts
dir; `molab-connect.ps1` is a thin cache + delegation layer.

## When to use
- The user asks to pair on / inspect / edit a marimo notebook hosted on
  `sb-*.sb.molab.run`.
- The user pastes a "connect to the notebook at ... / auth token ..." block.

## First connect on a fresh sandbox
The user's first message typically contains both the URL and the token:

```
Connect to the notebook at: https://sb-1a2b3c.sb.molab.run/
Use this auth token when calling execute-code.sh: ... --token <64 hex chars>
```

Parse and cache that block, then delegate all execution through it:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File <skill>/scripts/molab-connect.ps1 -Pasteline "<the pasted block>" -Save
```

## Subsequent calls (cache hit, no paste needed)

```powershell
# Run code in the notebook (most recent cached sandbox):
powershell -NoProfile -ExecutionPolicy Bypass -File <skill>/scripts/molab-connect.ps1 -Code "1 + 1"

# Explicit sandbox:
... molab-connect.ps1 -SandboxId sb-1a2b3c -Code "1 + 1"

# Show cached url/token + live kernel probe:
... molab-connect.ps1 -Info

# List all cached sandboxes:
... molab-connect.ps1 -Cache

# Manual upsert (e.g. token regenerated but URL same):
... molab-connect.ps1 -Set -Url https://sb-1a2b3c.sb.molab.run/ -Token <64 hex>
```

Stdin piping works as in execute-code.ps1:

```powershell
printf '%s\n' "import marimo as mo" "mo.status.toast(title='hi', description='pair ready', kind='success')" | powershell -NoProfile -ExecutionPolicy Bypass -File <skill>/scripts/molab-connect.ps1
```

## Connection protocol (once connected)

1. **Mandatory first kernel call** - the code-mode API is internal and
   undocumented; read its help before editing cells:
   ```
   import marimo._code_mode as cm; help(cm)
   ```
2. **Send a toast** so the user knows you're in:
   ```python
   import marimo as mo
   mo.status.toast(title='Pair mode: ON', description='...', kind='success')
   ```
3. **Inspect state** before editing:
   ```python
   import marimo._code_mode as cm
   ctx = cm.get_context()
   for c in ctx.cells.values():
       print(c.id, '|', c.name, '|', c.status)
   ```
   Cell statuses: `idle` (ran clean), `stale` (edited, needs re-run),
   `exception` (errored), `cancelled` (dep failed).

## Sandbox moves (410 Gone)

Attaching/detaching a GPU or restarting a sandbox **recreates it at a new
URL** (and a new token). If a call fails with `410 Gone`:

1. Ask the user for the new URL / connect block.
2. Cache it with `-Pasteline "<block>" -Save` (upserts by sandbox id).
3. Reconnect and re-verify.

Do NOT retry the old URL; it is permanently gone.

## GPU sandboxes

molab sandboxes are CPU-only by default (20 vCPU / 160 GB RAM, no CUDA).
Running models requires the user to attach a GPU via the notebook specs
button in the UI. After attach, the sandbox is recreated:

- New URL, new token → re-paste → cache upsert.
- Fresh CUDA image with **CPU torch** (`2.11.0+cu130` etc.) - the notebook's
  own `setup` cell handles the repair to `torch==2.14.0+cu132`; after repair,
  the **session must be restarted in the UI** so the kernel re-imports torch.
- `nvidia-smi -L` and `/tmp/uv-venv/bin/python -c "import torch;
  torch.cuda.is_available()"` are the probes to run after any sandbox change.
- The notebook's tool-loop mode also needs the vLLM sidecar and the private store
  snapshot — see `docs/MOLAB_OPS.md` "Private store bucket" / "Tool-loop sidecar
  config" before pairing on it. Remember the sandbox has **no repo checkout**
  (molab imports only the notebook file): pushing
  `scripts/molab_vllm_launch.sh` is the agent's job, or point the user at the
  copy inside the extracted `pg-rag-src/` tarball.

## Reference
- `scripts/execute-code.ps1` — the execution client (plain PowerShell +
  .NET HttpClient; talks to `POST /api/kernel/execute` and streams SSE).
  Piping Python via stdin works: `printf '%s\n' "code" | ... molab-connect.ps1`.
- Cached credentials live at `%USERPROFILE%\.marimo\molab-connections.json`
  (plaintext; per-machine).
