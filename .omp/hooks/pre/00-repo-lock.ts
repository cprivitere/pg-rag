// Cross-instance corpus-build lock probe for pg-rag.
//
// Why a file lock instead of the launch broker: a pre-hook is an extension
// module (pi.on("tool_call")) and cannot speak the broker's private socket
// protocol, so a `hub start` daemon can't arbitrate from inside a hook.
// Every omp instance on this repo shares the same filesystem, so an atomic
// lock file is the only primitive that genuinely coordinates across
// instances.
//
// WHO OWNS THE LOCK: the corpus writers themselves — `pgrag build-documents`
// (`src/pgrag/cli.py`), the BM25 persist/rebuild (`src/pgrag/rag/bm25.py`) and
// `scripts/build_tool_corpus.py`, all through `pgrag.buildlock.corpus_lock`.
// They hold it for the duration of the build and release it in a `finally`.
//
// Why the lock is NOT taken here: a hook's lifetime is one tool call, and a
// build outlives it — a command that trips the ~60 s foreground threshold is
// auto-backgrounded and keeps running after `tool_result` fires, and
// `omp://hooks.md` has no job-completion event, so a hook-held lock was
// released mid-build. So this hook only *probes*: it blocks a corpus command
// while a live, non-stale owner holds the lock, and otherwise stays out of the
// way. A spelling the matcher below misses is therefore not a hole — the
// writer locks regardless of how it was invoked (mise task, alias,
// `mise run --force …`, `… ::: syf`, direct CLI or direct script).
//
// Scope of the probe: the destructive corpus writers (build-documents /
// generate-docs / BM25 / tool corpus, and the mise tasks and aliases that run
// exactly those: sync-fast|sy, sync-full|syf, sync-wiki|syw, generate-docs|docs,
// build-bm25|b25, build-tool-corpus|btc). `mise sql-store` | `sq` is
// deliberately NOT guarded: the store is a private additive artifact
// (scripts/build_sqlstore.py states it), not shared corpus state. `mise
// upload-docs` / `upload-store` only publish, so they take no lock either — a
// publish racing a rebuild can still upload a torn documents.json, and the
// post-upload remote-size check is the only defence. Bare `mise sync` is
// mise's own tool-manager subcommand, not ours, and is deliberately not
// matched. A read that merely names a build task (`grep -rn build-bm25 .`,
// `git commit -m 'fix build-documents stamp'`) passes untouched: the probe is
// only reached for commands that could rewrite corpus state. Every hook
// runtime error is swallowed silently — this hook never blocks a command it
// cannot reason about.

import type { HookAPI } from "@oh-my-pi/pi-coding-agent/extensibility/hooks";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

const LOCK_REL = join("data", ".omp-build.lock");
// Staleness horizon: an owner older than this is ignored even while its pid is
// still alive — the recovery path for a lock leaked by a writer killed between
// acquire and release (a 3 h build is far outside this repo's build times).
// Mirrors TTL_S in `src/pgrag/buildlock.py`.
const TTL_MS = 3 * 60 * 60 * 1000;

// Destructive corpus commands, matched on the token that actually rewrites
// shared build state. Purely a cheap pre-filter for the probe below: the
// writers own the lock, so a miss here degrades to "no early warning", not to
// concurrency.
// The build-* alternation covers the mise tasks (build-documents, build-bm25,
// build-tool-corpus) and the `pgrag build-` CLI prefix; the direct
// `python -m pgrag.rag.bm25` and `python scripts/build_tool_corpus.py`
// invocations are guarded separately because they do not carry a `pgrag
// build-*` token (the mise tasks run exactly those commands).
// The `mise` clause names the corpus tasks and their aliases (`mise sy`/`syf`/
// `syw`, `docs`, `b25`/`btc`), plus the `mise r <task>` shorthand, the `run`
// subcommand's own flags (`mise run --force syf`), and any global flags before
// the task (`mise -C . syf`). A `:::`-separated chain element (`mise run
// golden ::: syf`) is matched by the trailing bare-alias clause. `sq`/
// `sql-store` is deliberately absent (see the header).
const CORPUS_TASKS = "sync-(?:fast|full|wiki)|generate-docs|build-bm25|build-tool-corpus|syf|syw|docs|b25|btc|sy";
const DESTRUCTIVE = new RegExp(
  "(?:\\b|_)(build-documents|build-bm25|build-tool-corpus|generate-docs)(?:\\b|\\s|\\s-|$)" +
    "|mise\\s+(?:-{1,2}[A-Za-z][\\w-]*(?:=\\S+|\\s+\\S+)?\\s+)*(?:(?:run|r)\\s+(?:-{1,2}[A-Za-z][\\w-]*(?:=\\S+|\\s+\\S+)?\\s+)*)?" +
    `(?:${CORPUS_TASKS})\\b` +
    `|:::\\s*(?:${CORPUS_TASKS})\\b` +
    "|\\b(?:uv\\s+run\\s+)?pgrag\\s+build-" +
    "|-m\\s*pgrag\\.rag\\.bm25\\b" +
    "|scripts[\\\\/]build_tool_corpus\\.py",
);

// Head of a segment that cannot rewrite corpus state. A lookup that merely
// *mentions* a task name (`grep -rn build-documents .`) — or a commit message
// that names one — must not be probed, let alone blocked. Heuristic by
// construction: a deny-list of non-corpus heads plus a chaining/redirection
// disqualifier, not a shell parser. `git` is allowed wholesale because no git
// subcommand rewrites `data/*.json`; a chained corpus command still fails the
// disqualifier below (`git commit … && mise syf`).
const READ_ONLY_HEAD =
  /^\s*(?:git|rg|grep|egrep|fgrep|findstr|Select-String|Get-ChildItem|Get-Content|Get-Item|Get-Command|Test-Path|gci|gc|awk|sed\s+-n|cat|bat|head|tail|less|more|wc|sort|uniq|jq|file|stat|du|ls|dir|type|where|echo|printf)\b/;

// True when every segment of the command is a pure reader. Chaining (`&&`, `;`,
// `&`, backticks, `$(…)`, subshells) and output redirection always disqualify:
// `grep x && mise sync-fast` is a writer. Quote-aware, so
// `grep -E "sync|build"` is not read as a pipeline of two commands.
function isReadOnly(command: string): boolean {
  let cmd = command.trim();
  for (;;) {
    // leading `cd <dir> &&` hops do not touch corpus state
    const hop = cmd.match(/^cd\s+(?:"[^"]*"|'[^']*'|\S+)\s*&&\s*/);
    if (!hop) break;
    cmd = cmd.slice(hop[0].length);
  }
  const segments: string[] = [];
  let current = "";
  let quote: string | null = null;
  for (const ch of cmd) {
    if (quote !== null) {
      if (ch === quote) quote = null;
      current += ch;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      current += ch;
      continue;
    }
    if (ch === "|") {
      segments.push(current);
      current = "";
      continue;
    }
    if (
      ch === "&" ||
      ch === ";" ||
      ch === "`" ||
      ch === "(" ||
      ch === ")" ||
      ch === "\n" ||
      ch === ">"
    ) {
      return false;
    }
    current += ch;
  }
  segments.push(current);
  return segments.every((segment) => READ_ONLY_HEAD.test(segment));
}

function projectRoot(cwd: string): string {
  // walk up to the repo root (.git present); cwd is normally already it.
  let dir = cwd;
  for (let i = 0; i < 8; i++) {
    try {
      if (existsSync(join(dir, ".git")) || existsSync(join(dir, ".omp", "RULES.md"))) return dir;
    } catch {
      /* keep walking */
    }
    // dirname, not string slicing: a backslash cwd (`F:\repo\src\pgrag`) has no
    // `/` to cut at, and slicing would chop one character per hop.
    const parent = dirname(dir);
    if (!parent || parent === dir) break;
    dir = parent;
  }
  return cwd;
}

function pidAlive(pid: number): boolean {
  if (!pid || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (e: unknown) {
    // ESRCH = not running; EPERM = exists but not ours (alive). Any other
    // unknown -> assume alive (conservative: don't ignore a live build).
    const code = e && typeof e === "object" && "code" in e ? e.code : undefined;
    return code !== "ESRCH";
  }
}

type Owner = { pid: number; ageMs: number };

// The live, non-stale owner of `path`, or null when there is no owner worth
// deferring to (absent, unreadable, dead pid, or past the staleness horizon) —
// in which case the writer that follows steals or takes it.
function liveOwner(path: string): Owner | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null; // absent or malformed -> nothing to defer to
  }
  if (!parsed || typeof parsed !== "object" || !("pid" in parsed)) return null;
  const pid = typeof parsed.pid === "number" ? parsed.pid : -1;
  const startedAt = "startedAt" in parsed && typeof parsed.startedAt === "number" ? parsed.startedAt : 0;
  if (!pidAlive(pid)) return null;
  const ageMs = startedAt > 0 ? Date.now() - startedAt : 0;
  if (startedAt > 0 && ageMs > TTL_MS) return null;
  return { pid, ageMs: Math.max(0, ageMs) };
}

export default function (pi: HookAPI): void {
  pi.on("tool_call", async (event, ctx) => {
    if (event.toolName !== "bash") return;
    const command =
      event.input && typeof event.input === "object" && "command" in event.input
        ? String(event.input.command)
        : "";
    if (!DESTRUCTIVE.test(command)) return;
    // A command that only names a build task takes no lock and is never denied.
    if (isReadOnly(command)) return;
    try {
      const root = projectRoot(ctx.cwd ?? process.cwd());
      const lockPath = join(root, LOCK_REL);
      const owner = liveOwner(lockPath);
      if (!owner) return; // free, stale, or unreadable: the writer will take it
      const held = owner.ageMs > 0 ? ` for ${Math.round(owner.ageMs / 1000)}s` : "";
      return {
        block: true,
        reason:
          `blocked by the repo corpus-build lock: pid ${owner.pid} has held ${lockPath}${held}. ` +
          "Wait for that build to finish, or if it crashed, remove the lock file and retry.",
      };
    } catch {
      // never fail a legit command because the hook itself broke
    }
    return;
  });
}
