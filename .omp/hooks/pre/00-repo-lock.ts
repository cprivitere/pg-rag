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
// `git commit -m 'fix build-documents stamp'`) passes untouched, and so does a
// pipeline that names one as an argument (`mise tasks | grep docs`, `mise exec
// -- ls docs/`): the probe is reached only for commands whose shape can rewrite
// corpus state — a corpus task in task position, or a writer's own spelling.
// A rare spelling that puts the name in task position (`mise which docs`) is
// still probed, which is the fail-safe direction; a `mise` spelled with a path
// or `.exe` is probed too, while the inspection subcommands (`mise tasks`/`ls`/
// `list`/`env`) are reader heads, so naming a build task at them is never a
// block. The probed root
// is the repo the *call* runs in, not necessarily the session's: a bash call's
// own non-empty `cwd` (another checkout) wins over the session cwd, which is
// only the fallback. Every hook runtime error is swallowed silently — this
// hook never blocks a command it cannot reason about.

import type { HookAPI } from "@oh-my-pi/pi-coding-agent/extensibility/hooks";
import { existsSync, readFileSync, statSync } from "node:fs";
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
// the task (`mise -C . syf`). Those in-between words are skipped by a *bounded
// lazy token skip* (`(?:[^\s]+\s+){0,12}?`): `run`/`r`, a flag and a separated
// flag value are all just whitespace-delimited tokens, so the same detection
// costs a fixed (tiny) number of alternatives instead of the exponential
// backtracking a nested `\S+`-with-optional-value group caused — a long flag
// run can no longer make the probe fail closed on its handler timeout. The
// skip is bounded: a corpus task more than a dozen words after `mise` is missed
// (again "no early warning", never a wrong block — `isReadOnly` runs after).
// A `:::`-separated chain element (`mise run golden ::: syf`) is matched by the
// trailing bare-alias clause. `sq`/`sql-store` is deliberately absent (see the
// header).
// The skip's token class excludes the shell's word separators (`|`, `&`, `;`,
// `<`, `>`, parens): without that, `mise tasks | grep docs` skips the pipe as
// if it were a token and matches the neighbouring command's word, probing (and
// blocking) a command that cannot rewrite anything. The task token is bounded
// by `(?![\w/.\-])`, not `\b`: `docs` is also a directory (`mise exec -- ls
// docs/`, any word ending in a task alias), and `\b` matches before the slash.
const CORPUS_TASKS = "sync-(?:fast|full|wiki)|generate-docs|build-bm25|build-tool-corpus|syf|syw|docs|b25|btc|sy";
const MISE_SKIP = "(?:[^\\s|&;<>()]+\\s+){0,12}?";
// `mise` may be spelled with a path or an interpreter extension (`mise.exe`,
// `F:\tools\mise.exe`, `./mise.cmd`) — the Windows spelling of the same writer.
// A *quoted* one (`& "$env:MISE_BIN\mise.exe" syf`) still misses: its token class
// excludes the call operator and the quotes, so it stays in the "no early
// warning, never a wrong block" class the writers' own lock covers.
const MISE_EXE = "(?:[^\\s|&;<>()]*[\\\\/])?mise(?:\\.exe|\\.cmd|\\.ps1)?";
const DESTRUCTIVE = new RegExp(
  "(?:\\b|_)(build-documents|build-bm25|build-tool-corpus|generate-docs)(?:\\b|\\s|\\s-|$)" +
    `|${MISE_EXE}\\s+${MISE_SKIP}` +
    `(?:${CORPUS_TASKS})(?![\\w/.\\-])` +
    `|:::\\s*(?:${CORPUS_TASKS})(?![\\w/.\\-])` +
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
//
// mise's *inspection* subcommands are reader heads for the same reason: a
// pipeline that lists or prints tasks (`mise tasks | grep build-bm25`) cannot
// rewrite corpus state, and without them every such pipeline was denied for the
// whole duration of a peer's build — a wrong block, which the probe must never
// produce. `mise run`/`mise exec` stay out, so a writer in task position is
// still probed.
const READ_ONLY_HEAD =
  /^\s*(?:git|rg|grep|egrep|fgrep|findstr|Select-String|Get-ChildItem|Get-Content|Get-Item|Get-Command|Test-Path|gci|gc|ruff|read|awk|sed\s+-n|cat|bat|head|tail|less|more|wc|sort|uniq|jq|file|stat|du|ls|dir|type|where|echo|printf|mise\s+(?:tasks|ls|list|env)|uv\s+run\s+(?:ruff|pytest|pyright))\b/;

// True when every segment of the command is a pure reader. Chaining (`&&`, `;`,
// `&`, backticks, `$(…)`, subshells) and output redirection always disqualify:
// `grep x && mise sync-fast` is a writer. Quote-aware, so
// `grep -E "sync|build"` is not read as a pipeline of two commands — but a
// substitution stays a substitution even inside quotes (`echo "$(mise syf)"`
// runs it), so those are rejected directly.
function isReadOnly(command: string): boolean {
  // Strip single-quoted spans first: in sh they are literal, so
  // `git commit -m 'fix: rerun `mise syf`'` runs nothing. What remains is the
  // double-quoted and unquoted text, where `$(`/backticks really do run
  // (`echo "$(mise syf)"`), so a substitution there is rejected.
  if (/(?:\$\(|`)/.test(command.replace(/'[^']*'/g, ""))) return false; // command substitution
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

// How long a created-but-unwritten lock file is trusted to its creator. Mirrors
// EMPTY_GRACE_S in `src/pgrag/buildlock.py`: during that window the writer
// defers, so the probe should say "busy" rather than let the command start.
const EMPTY_GRACE_MS = 5_000;

// The live, non-stale owner of `path`, or null when there is no owner worth
// deferring to (absent, unreadable, dead pid, or past the staleness horizon) —
// in which case the writer that follows steals or takes it. The rules are the
// writer's (`src/pgrag/buildlock.py: read_owner`): a record with no positive
// start time is unusable rather than permanently blocking, and a fresh *empty*
// file belongs to the creator mid-write.
function liveOwner(path: string): Owner | null {
  let raw: string;
  try {
    raw = readFileSync(path, "utf8");
  } catch {
    return null; // absent -> nothing to defer to
  }
  if (!raw.trim()) {
    // create→write window (see EMPTY_GRACE_MS)
    let ageMs = 0;
    try {
      ageMs = Date.now() - statSync(path).mtimeMs;
    } catch {
      return null; // vanished: the writer will just take it
    }
    return ageMs < EMPTY_GRACE_MS ? { pid: -1, ageMs: Math.max(0, ageMs) } : null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null; // corrupt -> the writer steals it
  }
  if (!parsed || typeof parsed !== "object" || !("pid" in parsed)) return null;
  const pid = typeof parsed.pid === "number" ? parsed.pid : -1;
  const startedAt =
    "startedAt" in parsed && typeof parsed.startedAt === "number" ? parsed.startedAt : 0;
  if (!pidAlive(pid)) return null;
  // No usable start time: nothing proves how old this is, and a record that
  // blocks forever has no escape hatch (the writer steals it). Treat as unusable.
  if (!(startedAt > 0)) return null;
  const ageMs = Date.now() - startedAt;
  if (ageMs > TTL_MS) return null;
  return { pid, ageMs: Math.max(0, ageMs) };
}

export default function (pi: HookAPI): void {
  pi.on("tool_call", async (event, ctx) => {
    if (event.toolName !== "bash") return;
    const input = event.input && typeof event.input === "object" ? event.input : undefined;
    const command = input && "command" in input ? String(input.command) : "";
    if (!DESTRUCTIVE.test(command)) return;
    // A command that only names a build task takes no lock and is never denied.
    if (isReadOnly(command)) return;
    try {
      // The lock belongs to the repo the command runs in: a bash call may carry
      // its own `cwd` (another checkout), so a non-empty string wins over the
      // session cwd — otherwise we'd probe the session repo's lock (false block
      // when it is free) and lose the warning for the repo actually targeted.
      const callCwd = input && "cwd" in input ? String(input.cwd ?? "").trim() : "";
      const root = projectRoot(callCwd || ctx.cwd || process.cwd());
      const lockPath = join(root, LOCK_REL);
      const owner = liveOwner(lockPath);
      if (!owner) return; // free, stale, or unreadable: the writer will take it
      const held = owner.ageMs > 0 ? ` for ${Math.round(owner.ageMs / 1000)}s` : "";
      const holder = owner.pid > 0 ? `pid ${owner.pid}` : "an unidentified process";
      return {
        block: true,
        reason:
          `blocked by the repo corpus-build lock: ${holder} has held ${lockPath}${held}. ` +
          "Wait for that build to finish, or if it crashed, remove the lock file and retry.",
      };
    } catch {
      // never fail a legit command because the hook itself broke
    }
    return;
  });
}
