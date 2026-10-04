"""Agentic tool loop: the LLM answers by issuing tool calls against the
SQLite store (and BM25 corpus) in a bounded follow-up loop.

Round protocol (both mechanisms wired, deterministic order):
1. Send the conversation with OpenAI ``tools=`` definitions AND the same
   contract embedded as text (a fenced ```tool block carrying JSON).
2. If the response carries native ``tool_calls`` -> execute each, append
   ``role=tool`` messages.
3. Elif the content carries a fenced ```tool block -> parse JSON, execute,
   append the result as a user message ("Tool results: ...").
4. Else the content is the final answer -> return.

Transport is this module's own requests.post to the llama.cpp
OpenAI-compatible endpoint at :8080 (pgrag.rag.llm stays single-message and
untouched). Return shape mirrors pipeline.ask() so eval tooling treats both
uniformly.
"""

from __future__ import annotations

import hashlib
import json
import re
import time

import requests

from pgrag.agentic.tools import DEFAULT_STORE, execute_tool

LLM_URL = "http://localhost:8080/v1/chat/completions"

DEFAULT_GENERATION = {"temperature": 0, "seed": 0}
DEFAULT_MAX_ROUNDS = 6
DEFAULT_MAX_TOKENS = 4096
_TIMEOUT = 300

# Per-round tool-result payload cap: truncate the middle, keep head+tail.
_RESULT_CAP = 12_000
_RESULT_HEAD = 8_000
_RESULT_TAIL = 4_000

_TOOL_BLOCK_RE = re.compile(r"```tool\s*\n(.*?)\n?```", re.DOTALL)
# Alternate text form some chat templates emit:
# <function=NAME><parameter=KEY>value</parameter>...</function>
_FN_BLOCK_RE = re.compile(r"<function=([\w.]+)>((?:(?!</function>).)*)</function>", re.DOTALL)
_FN_PARAM_RE = re.compile(r"<parameter=(\w+)>\s*(.*?)\s*</parameter>", re.DOTALL)

_FINAL_SYSTEM = (
    "You are a Project Gorgon game assistant. Answer from the gathered data "
    "provided by the user. NEVER fabricate facts that are not in the data. "
    "No tools are available in this conversation - just answer."
)


def _still_wants_tools(message: dict) -> bool:
    if (message.get("content") or "").strip() and not message.get("tool_calls"):
        return bool(_parse_tool_blocks(message.get("content")))
    return True


def _strip_tool_markup(text: str) -> str:
    text = re.sub(r"```tool\s*\n.*?\n?```", "", text, flags=re.DOTALL)
    text = re.sub(r"<function=[\w.]+>(?:(?!</function>).)*</function>", "", text, flags=re.DOTALL)
    return text.strip()


_DIGEST_BUDGET = 24_000


def _gathered_digest(trace_rounds: list, messages: list[dict]) -> str:
    """Compress tool results into the fallback prompt: newest results first,
    each row-capped, total budgeted so a big conversation cannot push the
    fallback prompt into context-limit territory (which starves `content`
    behind the thinking budget and returns an empty answer)."""
    parts = []
    for msg in messages:
        if msg.get("role") == "tool":
            parts.append(_cap(msg.get("content") or ""))
        elif msg.get("role") == "user" and isinstance(msg.get("content"), str):
            c = msg["content"]
            if c.startswith("Tool results:"):
                parts.append(_cap(c))
    if not parts:
        parts.append("(no tool data was gathered)")
    parts.reverse()  # newest first
    out, used = [], 0
    for part in parts:
        if used + len(part) > _DIGEST_BUDGET and out:
            break
        out.append(part)
        used += len(part)
    if used > _DIGEST_BUDGET:
        out[-1] = _cap_rows(out[-1][: _DIGEST_BUDGET] + "\n…")
    return "\n\n---\n\n".join(out)


_FORCE_ANSWER = (
    "You are out of tool rounds. Do NOT call any tool or emit any tool block. "
    "Write the final answer to the user's original question NOW, using only "
    "the data already gathered in this conversation. If something was not "
    "found, say exactly what is missing."
)

# Base rules copied verbatim from prompts.build_prompt's shared block
# (prompts.py is shared code — never import-and-slice it).
_SYSTEM_TEMPLATE = """You are a Project Gorgon game assistant.

Answer the user's question using the provided context.

Rules:
- Figure out what the user is really asking, even when the question is informal, and assemble the answer from the context — the exact answer need not be stated word-for-word in the documents.
- Reason from the context: connect information across documents, compare and rank options, and draw conclusions that follow from the stated facts. Planning a path or choosing the most efficient option from stated values is expected and helpful.
- Include relevant names, skills, levels, ingredients, and quantities when available.
- If the user asks about recipes, include the recipe name, ingredients with quantities, required skill level, and the recipe's description text (e.g. dose counts, effects, results).
- If multiple answers exist, list them.
- If the context contains PARTIAL information for the question, answer with exactly what is present and explicitly state what is missing — do not refuse the whole question because one detail is absent.
- Only say you do not know when the context contains nothing relevant: no facts, names, levels, or values that bear on the question.
- NEVER fabricate: do not invent facts, names, values, recipes, XP numbers, formulas, or mechanics that are not present in the context. Arithmetic directly derived from stated context values (such as calculating the difference between two stated cumulative XP totals: Target Level cumulative XP minus Start Level cumulative XP) is grounded analysis, not fabrication. If a specific number is not stated or derivable from the context, say it is not stated rather than guessing.
- NEVER cite sources that are not listed in the provided context.
- When listing sources, only reference documents that actually contributed to your answer."""

_SCHEMA_SUMMARY = """
You also have a live SQLite store (Project Gorgon primary data) that you can
query with tools. Tool results are authoritative: never fabricate rows that
were not returned, and prefer them over memory.

Store schema (main tables):
- items(code PK, name, internal_name, keywords, description, value, stack,
  equip_slot, skill_reqs, effect_descs, bestow_recipes, raw)
- ingredients(recipe_id, seq, item_code, item_keys, desc, stack)
  [recipe_id joins recipes.id; ingredients with NULL item_code have item_keys]
- recipes(id PK, name, internal_name, skill, skill_level_req, reward_skill,
  reward_xp, reward_xp_first, drop_off_level, drop_off_pct, description,
  result_items, prereq_recipe, keywords, raw)
- abilities(id PK, name, internal_name, skill, level, damage_type,
  description, keywords, reset_time, power_cost, raw)
- quests(id PK, name, internal_name, description, keywords, objectives,
  rewards, reward_items, favor_npc, displayed_location, raw)
- npcs(key PK, name, area, area_friendly, description, services,
  preferences, gift_items, raw)
- effects(id PK, name, desc, keywords, duration, raw)
- skills(id PK, name, description, combat, advancement_table,
  associated_keywords, xp_table, raw)
- xptables(id PK, internal_name, xp_amounts)  -- xp_amounts: JSON array
- xptable_levels(internal_name, level, xp_needed, cumulative_xp)  -- per-level + cumulative XP, PK (internal_name, level)
- advancementtables(id PK, name, raw)
- tsys_powers(id PK, internal_name, skill, slots, suffix, prefix, tiers, raw)
- sources(item_key, seq, entries, source_table)  -- entry JSON per row
- attributes(id PK, label, default_value, raw)
- itemuses(item_key PK, recipes_that_use, raw)  -- JSON array of recipe ids
- records(id PK, tbl, raw)  -- every other CDN table, JSON per row
- wiki_pages(title PK, filename, text)  -- raw wiki page text
- chat_events(ts, channel, speaker, text, source_file, line_no)
- player_events(ts, event, args, raw, source_file, line_no)
- char_reports(character, server, ts, report)  -- report: full JSON
- player_items(character, ts, type_id, name, rarity, storage, stack, value,
  imbue_power, imbue_tier, seq, report)
- session_docs(name PK, text)
- entity_names(name, type, snippet_source)  -- FTS5 over entities

Notes:
- wiki_pages.title LIKE '%Keyword%' lists candidate page titles; then
  get_page(title) fetches the full text. Procedural/mechanic prose (how a
  skill works, growing/farming, crafting workflows) lives on wiki pages -
  for 'how do I X' questions, find the relevant page(s) and read them.
  Wiki table rows are pipe-separated (| Item || col || col …) — READ EVERY
  COLUMN of the matching row and quote the values: e.g. the Mushroom Farming
  page's per-mushroom row is | Field Mushroom || 15 || 05 hrs || Bone ||
  Organs || … (level=15, grow time=5 hours, low substrate=Bone, high
  substrate=Organs). Skill prerequisites also appear as 'requires [[X]] 60
  to learn' lines in the overview.
- abilities.raw is JSON; base PvE damage per ability is
  json_extract(raw, '$.PvE.Damage') (e.g. Fireball 14, Fire Breath 80).
  Use it for damage comparisons instead of guessing from descriptions.
- keywords/slots/result_items are ' | '-joined TEXT; Lint_* internal markers
  are already filtered out of keyword columns (still in raw).
- recipes.result_items format: "ItemCode xStackSize" (e.g. '5011 x3').
- quests favor_npc is 'Area/NPC_Name'; npcs.key is 'NPC_Name'.
- report JSON in char_reports: Skills {name: {Level, BonusLevels, ...}},
  Currencies {GOLD: n, ...}, NPCs {npc: {FavorLevel}}, ActiveQuests list.
  Skills[<skill>].Abilities is the ONLY known-ability list (CharacterSheet
  exports); non-skill abilities (e.g. CharmRat) are bucketed under the
  pseudo-skill "Unknown". player_state section=abilities diffs a skill's
  CDN family against that list — use it for 'which abilities do I know'
  questions. NEVER trust abilities.level_req as a known-gate: shrine/
  riddle unlocks (TameBear/TameCat carry level_req=1) need their unlock
  event, not a level. game_state_skills has fresher LEVELS (log-sourced)
  than the sheet dump — cross-check both.
- player_items storage: vault/bag/NPC names; value is per-item gold value.
- chat_events has indexes on speaker and channel: 'SELECT channel, COUNT(*)
  FROM chat_events WHERE speaker = <name>' and channel-only counts are
  cheap. Avoid unfiltered GROUP BY/DISTINCT over all 3.5M rows (operation
  budget). The player character name is usually like the in-game name
  (case-sensitive; check DISTINCT speaker first with LIMIT).
- Ingredient item codes always resolve: JOIN ingredients.item_code = items.code
  to get names (e.g. SELECT i.item_code, it.name, i.stack FROM ingredients i
  JOIN items it ON it.code = i.item_code WHERE i.recipe_id = ?). Never tell
  the user item codes are unresolvable without running this join.
- Recipe lists and unlock levels are authoritative in the recipes table
  (recipes.skill + recipes.skill_level_req, XP in recipes.reward_xp /
  reward_xp_first). Wiki skill pages contain tables with unlabeled numeric
  columns (portion sizes, stat tiers, '000') that are NOT skill levels —
  never infer recipe unlock levels from a wiki table; query recipes instead
  (SELECT name, skill_level_req, reward_xp, reward_xp_first FROM recipes
  WHERE skill = 'X' ORDER BY skill_level_req).
- Leveling math: a recipe with skill_level_req = N requires skill level N
  to CRAFT (so 'level X to Y' means recipes with N < Y are craftable by Y);
  repeat XP is reward_xp, first-craft bonus is reward_xp_first. Level
  thresholds per skill live in xptables.xp_amounts (JSON array, index i =
  XP to reach level i+1). Better: xptable_levels(internal_name, level,
  xp_needed, cumulative_xp) has the same data PRECOMPUTED per level — never
  sum or subtract these numbers yourself; make SQL do it. For 'how much XP
  to reach level N' SELECT cumulative_xp WHERE level = N. For 'level X to
  Y' / 'most efficient way to level X to Y' run the scalar-difference query
  and quote its result:
  SELECT (SELECT cumulative_xp FROM xptable_levels WHERE internal_name =
  <skill's xp_table> AND level = Y) - (SELECT cumulative_xp FROM
  xptable_levels WHERE internal_name = <skill's xp_table> AND level = X)
  AS range_xp  -- e.g. Cheesemaking, X=17, Y=25 → range_xp = 6990.
  The skill row links the table via skills.xp_table → xptables.internal_name.
- Glogger play-history tables (ingested from the glogger companion app;
  source of truth for YOUR OWN gameplay behavior, all times server-local):
  - stall_events(id, event_timestamp, event_at, log_timestamp, log_title,
    action, player, owner, item, quantity, price_unit, price_total,
    raw_message, entry_index, ignored, created_at) — your player-shop stall
    ledger. action: added/removed/configured/visible/bought/collected/
    hire_stall/visitor_note/hid_item/shop_tag/unknown. 'bought' rows have
    price_unit + price_total and player=buyer; 'collected' rows are your
    payout pickups (price_total); 'configured' sets a price; 'hire_stall'
    rows are stall-keeper hire fees (price_total = fee paid, item NULL);
    'visitor_note' rows have player = the visitor who left a note.
    Revenue = SUM(price_total) WHERE action='bought'; owners' own buys are
    NOT excluded automatically — group by player to separate.
  - game_state_gift_log(id, character_name, npc_key, npc_name, gifted_at,
    favor_delta) — every gift you gave, with favor delta. Weekly gift-limit
    math: strftime('%Y-%W', gifted_at) buckets per NPC.
  - game_state_favor(character_name, npc_key, npc_name, cumulative_delta,
    favor_tier, last_confirmed_at) — current cumulative favor per NPC.
  - enemy_kills(id, enemy_name, enemy_entity_id, killing_ability,
    health_damage, armor_damage, killed_at, character_name, zone,
    combat_skills) + character_deaths(id, killer_name, killing_ability,
    died_at, area, damage_type) + death_damage_sources(death_id,
    event_order, attacker_name, ability_name, health_damage, armor_damage,
    is_crit) — combat history: kills by enemy/zone/ability, deaths by killer.
  - item_transactions(id, timestamp, character_name, item_name, internal_name,
    item_type_id, quantity, context, source) — item gain/loss ledger; context:
    loot/vendor_sell/storage_deposit/storage_withdraw/unknown/summoned.
  - character_recipe_completions(snapshot_id, recipe_key, completions) —
    latest snapshot only; JOIN recipes ON recipes.internal_name = recipe_key
    for names. Zero completions rows exist too (recipes you've unlocked).
  - character_npc_favor(snapshot_id, npc_key, favor_level) — favor tier per
    NPC at the latest snapshot.
  - character_skill_levels(snapshot_id, skill_name, level, bonus_levels,
    xp_toward_next, xp_needed_for_next) — skills at the latest snapshot.
  - character_currencies(snapshot_id, currency_key, amount) — wallet at the
    latest snapshot.
  - gourmand_eaten_foods(food_name, times_eaten) — gourmand diet history.
  - character_stats(snapshot_id, stat_key, value) — stats at the latest
    snapshot.
  - character_active_quests(snapshot_id, quest_key, category) — active
    quests at the latest snapshot.
  - game_state_storage(character_name, vault_key, item_name, ...) — current
    storage-vault contents.
  - words_of_power(character_name, word, power_name, description,
    discovered_at) — discovered Words of Power.
  - corpse_extracts(character_name, corpse_name, item_name, quantity, skill,
    skill_level, extracted_at) — anatomy/extraction history.
  - game_state_npc_vendor(character_name, npc_key, vendor_gold_available,
    vendor_gold_max, councils_earned_current, councils_earned_lifetime,
    last_confirmed_at) — NPC vendor gold pool + your sell earnings per NPC.
  Snapshot-linked rows are from the LATEST character_snapshots id (per-store
  watermark); event tables are cumulative history. All tables are small
  enough for GROUP BY except item_transactions (~240k rows) — filter it by
  character_name/context first.
"""

_TOOL_CONTRACT = """
Tools (call to gather more data; up to {max_rounds} rounds):

1. sql_query(sql) - one read-only SELECT on the store above. LIMIT 200 is
   added when absent; heavy full scans are aborted (>2M operations) - filter
   or use entity_names instead. Returns a markdown table.
2. find_entities(query, type=None) - FTS5 over entity names (type one of
   item, recipe, ability, quest, npc, effect, skill). Up to 20 hits.
3. get_page(title) - full wiki page text by exact title (LIKE fallback lists
   candidates). Best for skill/mechanic prose.
4. corpus_search(query, k=10) - BM25 over the generated corpus.
5. player_state(character=None, section="summary", query=None, skill=None) - live
   session data. section: summary, skills, currencies, quests, favor, items,
   abilities. abilities REQUIRES skill=<skill name> (e.g. "Animal Handling")
   and diffs the character sheet's known-ability list against the CDN family —
   use it for 'which abilities/skills do I know' questions.

How to call tools:
- Preferred: emit a native tool call (the API's tools parameter).
- Equivalent text form: a fenced block exactly like
  ```tool
  {{"name": "sql_query", "args": {{"sql": "SELECT ..."}}}}
  ```
  on its own, possibly several blocks in one reply.
- Alternate text form: <function=NAME><parameter=KEY>value</parameter>...</function>
- When you don't need more data, answer normally with NO tool blocks.
"""


def _tools_api() -> list[dict]:
    """OpenAI tools-parameter definitions for the five tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": "sql_query",
                "description": (
                    "Run one read-only SELECT against the Project Gorgon "
                    "SQLite store. LIMIT 200 added when absent."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"sql": {"type": "string"}},
                    "required": ["sql"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_entities",
                "description": "FTS5 search over entity names (items, recipes, abilities, quests, npcs, effects, skills).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": [
                                "item",
                                "recipe",
                                "ability",
                                "quest",
                                "npc",
                                "effect",
                                "skill",
                            ],
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_page",
                "description": "Fetch a full wiki page by title (markup lightened, 20k chars).",
                "parameters": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                    "required": ["title"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "corpus_search",
                "description": "BM25 search over the generated document corpus.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "k": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "player_state",
                "description": (
                    "Live play-session data: character skills, currencies, "
                    "quests, NPC favor, items, known-ability audit."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "character": {"type": "string"},
                        "section": {
                            "type": "string",
                            "enum": [
                                "summary",
                                "skills",
                                "currencies",
                                "quests",
                                "favor",
                                "items",
                                "abilities",
                            ],
                        },
                        "query": {"type": "string"},
                        "skill": {
                            "type": "string",
                            "description": (
                                "Skill name for section=abilities known-vs-"
                                "missing audit (e.g. 'Animal Handling')."
                            ),
                        },
                    },
                    "required": [],
                },
            },
        },
    ]


def _post(
    messages: list[dict], tools: list[dict] | None, generation: dict, max_tokens: int
) -> dict:
    payload = {
        "messages": messages,
        "temperature": generation.get("temperature", 0),
        "max_tokens": max_tokens,
        "stream": False,
    }
    if generation.get("seed") is not None:
        payload["seed"] = generation["seed"]
    if tools:
        payload["tools"] = tools
    try:
        response = requests.post(LLM_URL, json=payload, timeout=_TIMEOUT)
        response.raise_for_status()
    except requests.exceptions.ConnectionError as exc:
        raise LLMServerError(
            f"Cannot connect to LLM server at {LLM_URL}. Ensure llama.cpp is running on port 8080."
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise LLMServerError(f"LLM server at {LLM_URL} timed out after {_TIMEOUT}s.") from exc
    return response.json()


class LLMServerError(ConnectionError):
    pass


def _cap(text: str) -> str:
    if len(text) <= _RESULT_CAP:
        return text
    return _cap_rows(text)


def _cap_rows(text: str) -> str:
    """Row-aware cap for sql_query tables: drop middle ROWS instead of
    slicing characters, so every surviving row is intact — numbers the model
    needs must not vanish into a cut cell."""
    lines = text.split("\n")
    head = lines[0]  # '<n> rows.' from sql_query
    header_end = 1
    while header_end < len(lines) and lines[header_end].startswith("|"):
        header_end += 1
    header = lines[1:header_end]
    rows = [ln for ln in lines[header_end:] if ln.startswith("|")]
    if not rows:
        omitted = len(text) - _RESULT_HEAD - _RESULT_TAIL
        return text[:_RESULT_HEAD] + f"\n…[truncated {omitted} chars]…\n" + text[-_RESULT_TAIL:]
    budget = _RESULT_CAP - len(head) - 1 - sum(len(h) + 1 for h in header) - 60
    kept, used = [], 0
    for row in rows:
        if used + len(row) + 1 > budget // 2 and rows:
            break
        kept.append(row)
        used += len(row) + 1
    tail_rows, tail_used = [], 0
    for row in reversed(rows[len(kept):]):
        if tail_used + len(row) + 1 > budget // 2:
            break
        tail_rows.insert(0, row)
        tail_used += len(row) + 1
    omitted = len(rows) - len(kept) - len(tail_rows)
    note = f"\n…[{omitted} middle rows omitted]…" if omitted > 0 else ""
    return head + "\n" + "\n".join(header + kept + tail_rows) + note


def _parse_tool_blocks(content: str) -> list[dict]:
    calls = []
    for match in _TOOL_BLOCK_RE.finditer(content or ""):
        try:
            obj = json.loads(match.group(1))
        except ValueError:
            calls.append({"_raw": match.group(1)[:200]})
            continue
        if isinstance(obj, dict) and obj.get("name"):
            calls.append(obj)
        else:
            calls.append({"_raw": match.group(1)[:200]})
    for match in _FN_BLOCK_RE.finditer(content or ""):
        name = match.group(1)
        args = {}
        for pm in _FN_PARAM_RE.finditer(match.group(2)):
            key, raw = pm.group(1), pm.group(2)
            try:
                args[key] = json.loads(raw)
            except ValueError:
                args[key] = raw
        calls.append({"name": name, "args": args})
    return calls


def _execute(calls: list[dict], store_path: str, corpus: str, trace_rounds: list) -> list[str]:
    results = []
    for call in calls:
        if "_raw" in call:
            result = f"Could not parse tool call: {call['_raw']}"
            rows_or_len = None
            name = ""
        else:
            name = call.get("name") or ""
            args = call.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            started = time.time()
            result = execute_tool(store_path, name, args, corpus=corpus)
            rows_or_len = len(result)
            trace_rounds.append(
                {
                    "tool": name,
                    "args_hash": hashlib.md5(
                        json.dumps(args, sort_keys=True, default=str).encode()
                    ).hexdigest()[:8],
                    "rows_or_len": rows_or_len,
                    "ms": int((time.time() - started) * 1000),
                }
            )
        results.append(result if name == "get_page" else _cap(result))
    return results


def run_loop(
    question: str,
    history: list[dict] | None = None,
    corpus: str = "full",
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    trace: dict | None = None,
    generation: dict | None = None,
    store_path: str = DEFAULT_STORE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """Answer `question` via the tool loop. Same 5-key shape as pipeline.ask."""
    generation = dict(DEFAULT_GENERATION if generation is None else generation)
    trace_rounds: list = []
    seed = execute_tool(store_path, "corpus_search", {"query": question, "k": 12}, corpus=corpus)
    system = (
        f"{_SYSTEM_TEMPLATE}\n{_SCHEMA_SUMMARY}\n"
        f"{_TOOL_CONTRACT.format(max_rounds=max_rounds)}\n\n"
        f"Seed context (BM25 over the generated corpus):\n{seed}"
    )
    messages: list[dict] = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": question})

    rounds_used = 0
    budget_line = "Tool budget exhausted - answer now from data already gathered."
    while True:
        tools = _tools_api() if rounds_used < max_rounds else None
        if tools is None and budget_line:
            # The forced final turn must know why it can't call tools.
            # Sent as a user-role message: several chat templates (qwen
            # MoE family) reject a system message after the first turn
            # ("System message must be at the beginning").
            messages.append({"role": "user", "content": f"(system) {budget_line}"})
            budget_line = None
        data = _post(messages, tools, generation, max_tokens)
        message = data["choices"][0]["message"]
        # Empty-content guard: a reasoning model can spend its whole
        # generation budget on `reasoning_content` and return empty `content`
        # (finish_reason="length"). _still_wants_tools already treats an
        # empty message as "wants tools" — on the forced-final turn that
        # routes into the digest fallback; on mid-loop turns it would
        # otherwise be returned verbatim as the final answer, so re-ask once
        # with the digest framing instead. No loop risk: this branch only
        # fires when the message has neither content nor tool_calls to
        # execute, so it always returns.
        if not (message.get("content") or "").strip() and not message.get("tool_calls"):
            digest = _gathered_digest(trace_rounds, messages)
            data = _post(
                [
                    {"role": "system", "content": _FINAL_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"{digest}\n\nOriginal question: {question}\n\n"
                            "Write the final answer now. Do not request any "
                            "tool; if a fact was never retrieved, state "
                            "exactly what is missing."
                        ),
                    },
                ],
                None,
                generation,
                # Thinking-model budget: leave room for `content` after the
                # server-side reasoning spend (a reasoning model with a 4096
                # thinking budget can otherwise return empty content).
                max_tokens * 2,
            )
            message = data["choices"][0]["message"]
            stripped = _strip_tool_markup(message.get("content") or "")
            if not stripped:
                # Last resort: any content in ANY alternative field or a
                # minimal honest notice.
                stripped = (
                    "The tool round budget was exhausted before an answer "
                    "was written. Gathered data is recorded in the trace."
                )
            message = {"role": "assistant", "content": stripped}
            return {
                "answer": stripped,
                "documents": [],
                "query_type": "agentic",
                "rerank_used": False,
                "sources": [],
                "rounds": rounds_used,
                "trace_rounds": trace_rounds,
            }
        native_calls = message.get("tool_calls") or []
        text_calls = _parse_tool_blocks(message.get("content"))
        calls: list[dict] = []
        native_ids: list[str | None] = []
        for call in native_calls:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {"_raw": (fn.get("arguments") or "")[:200]}
                calls.append(args)
                native_ids.append(call.get("id"))
                continue
            calls.append({"name": fn.get("name"), "args": args})
            native_ids.append(call.get("id"))
        for call in text_calls:
            calls.append(call)
            native_ids.append(None)

        if not calls or rounds_used >= max_rounds:
            content = message.get("content") or ""
            return {
                "answer": content,
                "documents": [],
                "query_type": "agentic",
                "rerank_used": False,
                "sources": [],
                "rounds": rounds_used,
                "trace_rounds": trace_rounds,
            }

        rounds_used += 1
        results = _execute(calls, store_path, corpus, trace_rounds)
        used_native = any(cid is not None for cid in native_ids)
        if used_native:
            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content"),
                    "tool_calls": native_calls,
                }
            )
            for cid, result in zip(native_ids, results, strict=True):
                if cid is not None:
                    messages.append({"role": "tool", "tool_call_id": cid, "content": result})
                else:
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Tool results:\n{result}\n\n"
                            "Continue answering the original question.",
                        }
                    )
        else:
            joined = "\n\n".join(results)
            messages.append({"role": "assistant", "content": message.get("content")})
            messages.append(
                {
                    "role": "user",
                    "content": f"Tool results:\n{joined}\n\n"
                    "Continue answering the original question.",
                }
            )

    return {}  # pragma: no cover - loop always returns from inside
