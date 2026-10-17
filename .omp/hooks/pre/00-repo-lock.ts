// Cross-instance corpus-build lock for pg-rag.
//
// Why a file lock instead of the launch broker: a pre-hook is an extension
// module (pi.on("tool_call")) and cannot speak the broker's private socket
// protocol, so a `hub start` daemon can't arbitrate from inside a hook.
// Every omp instance on this repo shares the same filesystem, so an atomic
// O_EXCL lock file under the gitignored `data/` dir is the only primitive
// that genuinely coordinates across instances.
//
// Scope: block destructive corpus mutations (build-documents / generate-docs /
// build-bm25 / build-tool-corpus, and the mise tasks/aliases that run exactly
// those: sync-fast|sy, sync-full|syf, sync-wiki|syw, generate-docs|docs,
// build-bm25|b25, build-tool-corpus|btc) when another live instance holds the
// lock. `mise sql-store` | `sq` is deliberately NOT guarded: the store is a
// private additive artifact (scripts/build_sqlstore.py states it), not shared
// corpus state. `mise upload-docs` / `upload-store` only publish, so they take
// no lock either — a publish racing a rebuild can still upload a torn
// documents.json, and the post-upload remote-size check is the only defence.
// All other commands pass through untouched. Fail-closed only on proven
// contention: a non-EEXIST filesystem error falls through, and every hook
// runtime error is swallowed silently — this hook never blocks a command it
// cannot reason about.

import type { HookAPI } from "@oh-my-pi/pi-coding-agent/extensibility/hooks";
import { openSync, closeSync, unlinkSync, existsSync, readFileSync, writeSync } from "node:fs";
import { dirname, join } from "node:path";

const LOCK_REL = join("data", ".omp-build.lock");
// Staleness horizon: an owner older than this is stolen even while its pid is
// still alive — the recovery path for a lock leaked by a holder that died
// between acquiring and firing tool_result (a 3 h build is far outside this
// repo's build times).
const TTL_MS = 3 * 60 * 60 * 1000;

// Destructive corpus commands. Match on the token that actually rewrites
// shared build state.
// The build-* alternation covers the mise tasks (build-documents, build-bm25,
// build-tool-corpus) and the `pgrag build-` CLI prefix; the direct
// `python -m pgrag.rag.bm25` and `python scripts/build_tool_corpus.py`
// invocations are guarded separately because they do not carry a `pgrag
// build-*` token (the mise tasks run exactly those commands).
// The `mise` clause names the corpus tasks and their aliases (`mise sy`/`syf`/
// `syw`, `docs`, `b25`/`btc`), plus the `mise r <task>` shorthand and any
// global flags before the task (`mise -C . syf`) — every spelling that reaches
// the same artifacts must lock. `sq`/`sql-store` is deliberately absent (see
// the header). Bare `mise sync` is mise's own tool-manager subcommand, not
// ours, and is deliberately NOT matched.
const DESTRUCTIVE = /(?:\b|_)(build-documents|build-bm25|build-tool-corpus|generate-docs)(?:\b|\s|\s-|$)|mise\s+(?:-{1,2}[A-Za-z][\w-]*(?:=\S+|\s+\S+)?\s+)*(?:(?:run|r)\s+)?(?:sync-(?:fast|full|wiki)|generate-docs|build-bm25|build-tool-corpus|syf|syw|docs|b25|btc|sy)\b|\b(?:uv\s+run\s+)?pgrag\s+build-|-m\s+pgrag\.rag\.bm25\b|scripts[\\/]build_tool_corpus\.py/;

// Head of a segment that cannot rewrite corpus state. A lookup that merely
// *mentions* a task name (`grep -rn build-documents .`) must not take the lock
// — the old anywhere-match denied those reads with a "corpus build" reason.
// Heuristic by construction: it is a deny-list of reader heads plus a
// chaining/redirection disqualifier, not a shell parser.
const READ_ONLY_HEAD =
  /^\s*(?:git\s+(?:grep|log|show|diff|blame|ls-files)|rg|grep|egrep|fgrep|findstr|Select-String|awk|sed\s+-n|cat|bat|head|tail|less|more|wc|sort|uniq|jq|file|stat|du|ls|dir|type|echo|printf)\b/;

// True when every segment of the command is a pure reader. Chaining (`&&`, `;`,
// `&`, backticks, `$(…)`, subshells) and output redirection always disqualify:
// `grep x && mise sync-fast` is a writer, and `printf '' > data/tool_documents.json`
// rewrites a corpus artifact through a reader head. Quote-aware, so
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

type Owner = { pid: number; startedAt: number };
const EMPTY: Owner = { pid: -1, startedAt: 0 };

// The single lock path this instance holds, once acquired. The agent runs
// tools sequentially, so a destructive command's tool_call acquires and its
// own tool_result releases; we never hold more than one.
let heldPath: string | null = null;

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
    // unknown -> assume alive (conservative: don't steal a live build).
    const code = e && typeof e === "object" && "code" in e ? e.code : undefined;
    return code !== "ESRCH";
  }
}

function readOwner(path: string): Owner {
  try {
    const parsed: unknown = JSON.parse(readFileSync(path, "utf8"));
    if (parsed && typeof parsed === "object" && "pid" in parsed && typeof parsed.pid === "number") {
      return {
        pid: parsed.pid,
        startedAt: typeof parsed.startedAt === "number" ? parsed.startedAt : 0,
      };
    }
  } catch {
    /* malformed/stale -> EMPTY */
  }
  return EMPTY;
}

function removeLock(path: string) {
  try {
    unlinkSync(path);
  } catch {
    /* already gone */
  }
}

function tryGrab(lockPath: string): "acquired" | "self" | "contended" | "error" {
  let fd: number | null = null;
  try {
    fd = openSync(lockPath, "wx"); // O_CREAT|O_EXCL — atomic across instances
    try {
      writeSync(fd, JSON.stringify({ pid: process.pid, startedAt: Date.now() }));
    } catch {
      // A created-but-unwritten lock reads as contended (unreadable owner) to
      // every later caller: drop it and report the failure instead.
      removeLock(lockPath);
      return "error";
    }
    return "acquired";
  } catch (e: unknown) {
    const isEEXIST = e !== null && typeof e === "object" && "code" in e && e.code === "EEXIST";
    if (isEEXIST) {
      const owner = readOwner(lockPath);
      if (owner.pid === process.pid) return "self"; // re-entrant, same instance
      if (!pidAlive(owner.pid) || Date.now() - owner.startedAt > TTL_MS) {
        removeLock(lockPath);
        return tryGrab(lockPath);
      }
      return "contended";
    }
    // ENOENT/EACCES/EROFS/ENOSPC/… is a filesystem problem, not a competing
    // build: report it so the caller lets the command through.
    return "error";
  } finally {
    if (fd !== null) closeSync(fd);
  }
}

export default function (pi: HookAPI): void {
  pi.on("tool_call", async (event, ctx) => {
    if (event.toolName !== "bash") return;
    const command =
      event.input && typeof event.input === "object" && "command" in event.input
        ? String(event.input.command)
        : "";
    if (!DESTRUCTIVE.test(command)) return;
    // A read that only names a build task takes no lock and is never denied.
    if (isReadOnly(command)) return;
    try {
      const root = projectRoot(ctx.cwd ?? process.cwd());
      const lockPath = join(root, LOCK_REL);
      const res = tryGrab(lockPath);
      if (res === "acquired") heldPath = lockPath;
      if (res === "contended") {
        const owner = readOwner(lockPath);
        const holder = owner.pid > 0 ? `pid ${owner.pid}` : "an unknown instance";
        return {
          block: true,
          reason:
            `blocked by repo-build lock: another omp instance (${holder}) is running a ` +
            `corpus build (${lockPath}). Wait for it to finish, or if it crashed, ` +
            `remove the lock file and retry.`,
        };
      }
    } catch {
      // never fail a legit command because the hook itself broke
    }
    return;
  });

  pi.on("tool_result", async () => {
    if (!heldPath) return;
    const lockPath = heldPath;
    heldPath = null;
    try {
      if (readOwner(lockPath).pid === process.pid) removeLock(lockPath);
    } catch {
      /* ignore */
    }
  });
}