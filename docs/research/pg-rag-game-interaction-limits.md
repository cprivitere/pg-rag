# pg-rag × Project Gorgon — hard lines (full TOS/CoC compliance)

Rules for every part of this repo that touches the game, its data, or its
traffic.

**Commitment:** pg-rag follows the Project: Gorgon Terms of Service (v3.0,
effective 2026-04-22) and Code of Conduct (v3.0, binding, incorporated into
the TOS) **completely and without exception**. We do not use legal
carve-outs, "defensible space" reasoning, or personal-use arguments to
justify exceptions. Where the TOS prohibits a category by name, pg-rag
simply does not do it — regardless of what a court might allow.

Rationale: a TOS violation by the knowledge base taints every downstream
consumer (this repo, the OpenWebUI pipe, the glogger tab). These lines exist
so no future session has to re-derive them — and so code review can check
against them mechanically via `scripts/check_constraints.py`.

Sources of truth (re-read when this doc feels stale):
- TOS v3.0: https://projectgorgon.com/terms.html — §5 (License Grant
  prohibitions), §8 (Mods, Tools and Anti-Cheat), §4.6 (Unauthorized
  Transactions), §19 (Termination).
- Code of Conduct v3.0 (binding, part of the TOS):
  https://projectgorgon.com/conduct.html — §4.C (Cheating, Exploitation, and
  Security Violations), §4.G (Commercial Misuse).
- Third-party CDN data terms: https://cdn.projectgorgon.com/v486/data/
  (governs everything pg-rag ingests from the game-data CDN).
- Privacy Policy v3.1: https://projectgorgon.com/privacy.html

**The one sentence:** pg-rag reads only what the user's own game client
already wrote to the user's own disk, plus Elder Game's own public CDN for
third-party tools. Everything else about the game is off-limits.

## 1. What the TOS names, and what we therefore never do

| TOS/CoC clause | Verbatim prohibition | pg-rag rule — absolute |
|---|---|---|
| §5 bullet 1 | "Reverse engineer, decompile, disassemble, modify, adapt, or create derivative works from the Game client or Services" | **Never decompile, disassemble, or dump the client.** No client-binary analysis of any kind, no dump outputs anywhere, ever — even "offline", even "personal use". Nothing derived from the client enters pg-rag. (The former IL2CPP decomp source was removed in full on 2026-10-02; see §4.) |
| §5 bullet 2 | "Use bots, macros, scripts, automation, data mining tools, packet interception tools, memory editing tools, or any unauthorized software or hardware in connection with the Services" | **No packet interception in any form.** No capture components, no traffic decoding, no wire-protocol knowledge of any kind in any consumer of this repo. |
| Conduct §4.C bullet 3 | "Reverse engineering, tampering with, intercepting, or modifying game data, communications, or technical protections" | Same as both rows above; also **never touch** the client's own telemetry, update checks, or technical protections. |
| §5 bullet 3 | "Operate, assist, develop, advertise, or use private servers, emulators, cheats, exploits, or unauthorized modifications" | No private-server/emulator compatibility, no exploit documentation or tooling, ever. |
| §5 bullet 4 | "Exploit the Game or Services for commercial purposes without Elder Game's prior written consent" | pg-rag stays free. Never monetize game-derived data: no premium tiers on game-derived features, no ads against game data, no selling "market intelligence". |
| §4.6 + Conduct §4.G | "Unauthorized … selling, exchanging … accounts, virtual currency, items …"; "real money trading" | Never facilitate RMT: no seller-contact flows, no trade matchmaking, no offer/bid aggregation routed to human traders. Stall/gift analytics from the user's own play history are descriptive only (§3). |
| §5 bullet 6 | "Use multiple accounts simultaneously to gain an unfair gameplay advantage" | Multi-client **observation** is fine (session ingestion may read several own-client logs); multi-**play** coordination never. |
| §5 bullet 5 | "Circumvent, disable, or interfere with technical protection measures, access controls, or security features" | Never patch/bypass/avoid ACTk, never write game files, never block the client's own telemetry or updates, never probe game servers outside the running client. |
| TOS §8 | "unauthorized overlays that affect gameplay … any tool intended to alter, automate, manipulate, intercept, or gain unfair advantage" | pg-rag renders only in its own windows/pages. Never an in-game HUD, never input automation. Features stay informational: only data the user's own client already rendered or logged. |
| TOS §11 + Conduct §4.B | privacy: "sharing another person's private or personally identifying information"; "attempting to obtain private information through … technical abuse, or unauthorized access" | The agentic store keeps only what the user's own client displayed publicly (owner names, prices, chat the user saw). Never de-anonymize cross-character identities, never export other players' activity beyond what the user's own session rendered, never aggregate other players' data across users. Store-only goldens ship as *questions only*; the play-history facts stay on the user's own machines, except inside the user's own **private** HF bucket (`Nubula/paddock-private` — never public, never aggregated across users, never shared), which only the user's own molab sandbox reads with the user's own token (`mise upload-store`). |
| Conduct §4.E | "facilitate fraud, scams, deceptive practices" | Play-history observations are timestamped "as seen at" — never presented as authoritative/verified market truth. |
| TOS §9 | live service, anything can change anytime | Shipped features tolerate schema changes overnight; never assume old-version CDN files exist (only 3–4 versions persist). |
| CDN terms | attribution + restriction rights | Attribution required wherever CDN-derived data ships publicly (About/help surfaces). Elder Game may restrict usage by individual/purpose; comply immediately if asked. |

## 2. What ACTk additionally detects (belt-and-suspenders; the TOS rows above already forbid these)

pg-rag is a pure Python data pipeline with no game-process interaction, so
most ACTk categories are structurally impossible. The scanner still checks
the categories that a Python repo *could* reintroduce:

| ACTk detects | Hard line | Check |
|---|---|---|
| Process injection / memory tampering | **NEVER** open a handle to the game process, never read/write game memory | Grep: `OpenProcess`, `ReadProcessMemory`, `WriteProcessMemory`, `VirtualAllocEx`, `CreateRemoteThread`, `DebugActiveProcess`, `SetWindowsHookEx` — zero hits |
| Input automation | **NEVER** synthesize input into the game | Grep: `SendInput`, `keybd_event`, `mouse_event`, `BlockInput` — zero hits |
| Packet interception | **NEVER** capture/decode game traffic | Grep: `WinDivert`, `pcap`, `Npcap`, `tshark`, `tcpdump`, socket binds to the game port — zero hits |
| Client-derived artifacts | **NEVER** store/ship client binaries or dump outputs | Grep: `GameAssembly`, `global-metadata`, `dump\.cs`, `DummyDll`, `Il2CppDumper`, `Cpp2IL` — zero hits anywhere incl. docs |
| Wire-protocol knowledge | **NEVER** encode knowledge of the game's wire format | Grep: `LEB128`-style readers, `GorgonClient`, `ServerCommand`/`ClientCommand` protocol readers — zero hits |

No push without a green `scripts/check_constraints.py` (§6).

## 3. What we DO allow

- **Reading files the user's own client wrote to the user's own disk:**
  `Player.log`, `Chat-*.log`, `Reports/*.json`, `Books/*.txt`
  (`src/pgrag/agentic/session.py`).
- **Reading the glogger app's own SQLite DB** via a WAL-safe snapshot copy
  (`src/pgrag/agentic/glogger.py`) — it is the user's own capture of their
  own play session, on their own disk.
- **HTTP GETs to Elder Game's public CDN for third-party tools**
  (`cdn.projectgorgon.com`) — explicitly offered for this purpose, with
  attribution. No other Elder Game endpoints. No authentication, no session
  tokens, no game-server connections of any kind outside the running client.
- **HTTP to pg-rag's own services** (embed :8081, LLM :8080, reranker :8082,
  HF model buckets) for pg-rag's own data.

## 4. History note — the IL2CPP decomp source (removed)

This repo previously ingested IL2CPP decompilation artifacts
(`GameAssembly.dll` + `global-metadata.dat` staged from the Steam install,
dumped to C# via il2cpp-dumper, rendered into 307 enum/schema/mechanic
documents in the RAG corpus). On reading the actual TOS §5 bullet 1 text,
that entire source was **removed** on 2026-10-02: code
(`decomp_builder.py`, `sync_il2cpp.py`, survey analyzers, their tests), the
1.4 GB `data/il2cpp/` tree, the dumper tooling, the 4 decomp-derived golden
cases, all corpus docs (regenerated: 264,493 docs), and every documentation
reference. This section exists so future sessions don't rediscover the same
idea and redo the same mistake. Schema/mechanic questions now ground in the
CDN tables and wiki — the sources Elder Game publishes for exactly that
purpose.

## 5. What else is *definitely* safe (feature ideas within the lines)

1. **More own-file ingest** — anything the client logs to disk that we don't
   parse yet.
2. **Pure-DB analytics** — wealth curves, XP rates, loot history, market
   history from the agentic store's own data.
3. **Curated doc expansion** — deterministic template docs over CDN+wiki
   (existing curator pattern).
4. **Chat surfaces for the tool loop** — Gradio tab, glogger tab, molab
   notebook pairing: all read the same store/CDN sources. (The molab
   notebook now ships this: its mode selector runs `run_loop` against a
   snapshot of the store published to the owner's private bucket —
   `mise upload-store`; corpus chat remains the other mode.)

Explicitly off this list (rejected, do not propose): packet-level anything,
protocol analysis of the game's network traffic, binary/memory analysis of
the client, any automation of gameplay, decompilation of any kind.

## 6. Enforcement: `scripts/check_constraints.py`

Runs on every push (`.githooks/pre-push`) and in CI. In full-compliance mode
the scanner checks §2's five categories across `src/`, `scripts/`, `tests/`,
`docs/`, `.agents/`, and `mise.toml`/`pyproject.toml`.

Verified: green on the compliant repo, red on probe violations. Skippable
only with `PG_SKIP_CONSTRAINTS=1` (prints a warning — never silent). The
checker script excludes itself (it must name the patterns it greps for), as
does this doc (it quotes the prohibited terms in the table above).

## 7. Change-control for this doc

There is no carve-out reasoning in this doc to relax. Any proposal to touch
a prohibited category must first argue **to Elder Game** (written consent
per §5 bullet 4's "prior written consent" model) — not to this repo. Absent
that, the answer is no. If the TOS itself changes, update this doc from the
new text, never from memory.
