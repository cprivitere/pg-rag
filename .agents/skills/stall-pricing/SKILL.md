---
name: stall-pricing
description: Price suggestions for the player's own stall stock. Use when asked to price, reprice, or evaluate current stall listings.
---

# stall-pricing — suggest updated prices for current stall stock

Ground rule: **the store is the oracle, never the live glogger DB.**
`player_state section=stall_inventory` over `data/sqlite_gorgon.db` is the
single definition of CURRENTLY LISTED. Never open the live glogger DB
directly (binary dumps, repeated reads); if the user just played, run
`mise sync-glogger` first (seconds — glogger tables only, no corpus rebuild),
then read the store.

## Procedure

1. **Sync when stale.** Just played (or the user says so): `mise
   sync-glogger`. Full rebuilds (`mise sql-store`) are for builder/loop/doc
   changes, never for pricing.
2. **List current stock.** One call, no filters first:
   `player_state section=stall_inventory` (via `execute_tool` or `mise
   chat`). This replays glogger's Inventory-tab semantics from the store:
   `stall_inventory_baseline` rows seed tiers per (owner, item), ledger events
   at-or-before the baseline drop, later events replay in `(event_at ASC, id
   ASC)` order (added / visible|configured / bought / removed / hid_item;
   hid_item evicts, a visible/configured with no tiers seeds stock — priced
   as the ask, unpriced as UNPRICED).
   The 100-day window filters history, never post-baseline events or bought
   rows. Items with remaining qty > 0 list as `qty@price` tiers (priceless
   remainder as UNPRICED); the rest is OUT.
3. **Comps per listed item.** For each in-stock item, one
   `player_state section=stall query=<item>` (or batched `sql_query` over
   `stall_events`) for:
   - own realized sales (`bought`: per-unit price, buyer, date — never average),
   - own asking history (`visible`/`configured`),
   - CDN base value (`items.value` — the vendor floor),
   - `source:` lines (where the item comes from / is spent),
   - work-order floor (fixed payout ÷ required count, when it applies),
   - other-stall observations (`stall_price_observations`) and chat mentions
     when present — dated observations/asks, never own sales.
4. **Suggest.** Per item: HOLD, RAISE, or CUT with the comp behind it.
   Pricing layers, in order: own realized sale > own ask history > base value
   + work-order floor. Name seasonal/low-confidence calls (masks, remnants)
   as volatile.

## Traps

- The pg-rag fold and glogger's tab disagree when the store is stale or the
  ingest diverges — sync first, then trust the store.
- `stall_events` holds the player's OWN ledger only; `stall_price_observations`
  is the only other-stall signal (often absent — no table, no comps).
- Zero 7-day sales on an item is not a cut signal by itself; most of the
  stall was already repriced on 2026-11-09 — cutting again prices against no signal.
- Restocks (sold-out items with proven prices) are a separate list from reprices.
