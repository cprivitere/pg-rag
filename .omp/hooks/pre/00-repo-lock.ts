// Cross-instance corpus-build lock for pg-rag.
//
// Why a file lock instead of the launch broker: a pre-hook is an extension
// module (pi.on("tool_call")) and cannot speak the broker's private socket
// protocol, so a `hub start` daemon can't arbitrate from inside a hook.
// Every omp instance on this repo shares the same filesystem, so an atomic
// O_EXCL lock file under the gitignored `data/` dir is the only primitive
// that genuinely coordinates across instances.
//
// Scope: block destructive corpus mutations (build-index / build-documents /
// generate-docs / mise sync / refresh) when another live instance holds the
// lock. All other commands pass through untouched. Fail-closed only on proven
// contention — any hook runtime error logs and lets the command through.

import type { HookAPI } from "@oh-my-pi/pi-coding-agent/extensibility/hooks";
import { openSync, closeSync, unlinkSync, existsSync, readFileSync, writeSync } from "node:fs";
import { join } from "node:path";

const LOCK_REL = join("data", ".omp-build.lock");
// Fallback staleness when pid-liveness cannot be determined (defensive only;
// a live pid is never stolen regardless of age).
const TTL_MS = 3 * 60 * 60 * 1000;

// Destructive corpus commands. Match on the token that actually rewrites
// shared build state; deliberately conservative so reads/greps pass.
const DESTRUCTIVE = /(?:\b|_)(build-index|build-documents|generate-docs)(?:\b|\s|\s-|$)|mise\s+sync\b|\b(?:uv\s+run\s+)?pgrag\s+build-/;

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
    const parent = dir.slice(0, dir.lastIndexOf("/"));
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

function tryGrab(lockPath: string): "acquired" | "self" | "contended" {
  let fd: number | null = null;
  try {
    fd = openSync(lockPath, "wx"); // O_CREAT|O_EXCL — atomic across instances
    writeSync(fd, JSON.stringify({ pid: process.pid, startedAt: Date.now() }));
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
    }
    return "contended"; // fail-open on any other error
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