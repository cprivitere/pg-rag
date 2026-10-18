#!/usr/bin/env node
/**
 * JavaScript/TypeScript gate for the repo's script-class JS
 * (`scripts/*.mjs`). Usage: `node scripts/check_ts.mjs [paths...]`, default
 * `scripts`; exit 1 on any diagnostic, 0 with a summary line.
 *
 * Why a parser and not `tsc`'s full type check: these files run inside
 * oh-my-pi's compiled-binary runtime — `@oh-my-pi/pi-coding-agent` has no
 * type definitions on a dev box, so that use cannot be type-checked anywhere
 * but inside omp. What is checkable, and what this gate exists for, is that
 * the files are valid JS/TS (ruff covers Python).
 *
 * The one expected diagnostic is `TS2307 cannot find module` for that runtime
 * import (see above); it is reported as a note, never a failure. Everything
 * else fails the gate — a syntax error, a typo'd local, a bad-arity call
 * against the resolvable types (`node:fs`/`node:path`/`process` come from
 * `@types/node`, a devDependency). `.mjs` files are parsed (not type-checked)
 * as JavaScript.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import ts from "typescript";

const ROOT = resolve(import.meta.dirname, "..");
const DEFAULT_TARGETS = ["scripts"];
const EXTENSIONS = [".ts", ".mjs"];

const OPTIONS = {
  noEmit: true,
  allowJs: true, // `.mjs` roots: parsed for syntax, not type-checked (no checkJs)
  // Not `strict`: the runtime API's types are the thing that is missing, so
  // every parameter of the hook's callback is implicitly `any` — noise, not a
  // defect. Unstrict leaves the useful half of the check (syntax, unresolved
  // names, arity/type use of everything that *is* resolvable).
  strict: false,
  target: ts.ScriptTarget.ES2022,
  module: ts.ModuleKind.NodeNext,
  moduleResolution: ts.ModuleResolutionKind.NodeNext,
  skipLibCheck: true,
  types: ["node"],
};

// "Cannot find module": the omp runtime's own types, absent by construction.
const EXPECTED_CODES = new Set([2307]);

function isScript(file) {
  return EXTENSIONS.some((extension) => file.endsWith(extension));
}

function collect(target, matched) {
  const absolute = resolve(ROOT, target);
  let stats;
  try {
    stats = statSync(absolute);
  } catch {
    throw new Error(`no such path: ${target}`);
  }
  if (stats.isFile()) {
    if (isScript(absolute)) matched.push(absolute);
    return matched;
  }
  for (const entry of readdirSync(absolute).sort()) {
    const child = join(absolute, entry);
    if (statSync(child).isDirectory()) collect(child, matched);
    else if (isScript(child)) matched.push(child);
  }
  return matched;
}

function main() {
  const targets = process.argv.slice(2);
  const requested = targets.length ? targets : DEFAULT_TARGETS;
  const files = requested.flatMap((target) => collect(target, []));
  if (files.length === 0) {
    console.log(`check_ts: no ${EXTENSIONS.join("/")} files under ${requested.join(", ")}`);
    return 1;
  }

  const program = ts.createProgram({ rootNames: files, options: OPTIONS });
  let failed = 0;
  let expected = 0;
  for (const diagnostic of ts.getPreEmitDiagnostics(program)) {
    const code = `TS${diagnostic.code}`;
    if (!diagnostic.file) {
      console.log(`check_ts: ${code} ${ts.flattenDiagnosticMessageText(diagnostic.messageText, " ")}`);
      failed += 1;
      continue;
    }
    const { line, character } = diagnostic.file.getLineAndCharacterOfPosition(
      diagnostic.start ?? 0,
    );
    const relative = diagnostic.file.fileName.slice(ROOT.length + 1).replaceAll("\\", "/");
    console.log(
      `${relative}:${line + 1}:${character + 1} ${code} ` +
        ts.flattenDiagnosticMessageText(diagnostic.messageText, " "),
    );
    if (EXPECTED_CODES.has(diagnostic.code)) expected += 1;
    else failed += 1;
  }
  const names = files.map((file) => file.slice(ROOT.length + 1).replaceAll("\\", "/"));
  console.log(
    `check_ts: ${files.length} file(s) parsed (${names.join(", ")})` +
      (expected ? `; ${expected} expected unresolved-import note(s)` : "") +
      (failed ? `; ${failed} diagnostic(s)` : "; no diagnostics"),
  );
  return failed ? 1 : 0;
}

try {
  process.exit(main());
} catch (error) {
  console.error(`check_ts: ${error instanceof Error ? error.message : error}`);
  process.exit(1);
}
