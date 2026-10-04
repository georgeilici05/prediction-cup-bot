#!/usr/bin/env python3
"""Dry-run-first allocator for independent Senate-party parity opportunities.

It divides a maximum budget equally across every currently executable pair,
while skipping exchanges where the account already holds a position.  Use
``--execute`` only after reviewing the dry-run plan.
"""

from __future__ import annotations

import argparse
import math
import sys
from typing import Any

from parity_scanner import TITLE, TOURNAMENT_SLUG, get, load_key
from trade_executor import submit_trade, tournament_id


DEFAULT_BUDGET = 10_000.0
DEFAULT_MIN_EDGE = 0.01


def all_markets(key: str) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = get(key, "/markets", limit=100, cursor=cursor)
        markets.extend(page.get("data", []))
        pagination = page.get("pagination", {})
        if not pagination.get("hasMore"):
            return markets
        cursor = pagination.get("nextCursor")
        if not cursor:
            raise RuntimeError("Market pagination reported more results without a next cursor.")


def held_exchange_ids(key: str) -> set[str]:
    """Return every exchange with an existing non-zero position.

    This avoids the New Hampshire problem: a new NO order must not silently
    net against an older YES position (or vice versa).
    """
    data = get(key, f"/tournaments/{TOURNAMENT_SLUG}/portfolio/positions")
    return {
        str(position["exchangeId"])
        for position in data.get("positions", [])
        if float(position.get("quantity", 0)) != 0
    }


def candidate_pairs(key: str, tournament: str, minimum_edge: float) -> tuple[list[dict[str, Any]], set[str]]:
    held = held_exchange_ids(key)
    grouped: dict[str, dict[str, dict[str, str]]] = {}
    for market in all_markets(key):
        match = TITLE.match(str(market.get("title", "")))
        exchanges = market.get("exchanges", [])
        if not match or not exchanges:
            continue
        party, state = match.groups()
        grouped.setdefault(state, {})[party] = {
            "title": str(market["title"]),
            "exchange_id": str(exchanges[0]["id"]),
        }

    complete = {
        state: pair
        for state, pair in grouped.items()
        if set(pair) == {"Republican", "Democratic"}
        and pair["Republican"]["exchange_id"] not in held
        and pair["Democratic"]["exchange_id"] not in held
    }
    exchange_ids = [contract["exchange_id"] for pair in complete.values() for contract in pair.values()]
    quotes: dict[str, dict[str, Any]] = {}
    for start in range(0, len(exchange_ids), 100):
        page = get(
            key,
            "/exchanges/prices",
            ids=",".join(exchange_ids[start : start + 100]),
            tournamentId=tournament,
        )
        quotes.update({str(item["exchangeId"]): item for item in page.get("data", [])})

    # First rank with bulk quotes.  The smaller order-book reads below ensure
    # every planned price and quantity is executable right now.
    ranked: list[tuple[float, str, dict[str, dict[str, str]]]] = []
    for state, pair in complete.items():
        rep = quotes.get(pair["Republican"]["exchange_id"])
        dem = quotes.get(pair["Democratic"]["exchange_id"])
        if not rep or not dem or rep.get("bestBid") is None or dem.get("bestBid") is None:
            continue
        edge = float(rep["bestBid"]) + float(dem["bestBid"]) - 1.0
        if edge >= minimum_edge:
            ranked.append((edge, state, pair))

    candidates: list[dict[str, Any]] = []
    for _, state, pair in sorted(ranked, key=lambda row: (-row[0], row[1])):
        rep_book = get(key, f"/exchanges/{pair['Republican']['exchange_id']}/orderbook", depth=1, tournamentId=tournament)
        dem_book = get(key, f"/exchanges/{pair['Democratic']['exchange_id']}/orderbook", depth=1, tournamentId=tournament)
        if not rep_book.get("bids") or not dem_book.get("bids"):
            continue
        rep_bid = rep_book["bids"][0]
        dem_bid = dem_book["bids"][0]
        rep_no = 1.0 - float(rep_bid["price"])
        dem_no = 1.0 - float(dem_bid["price"])
        cost = rep_no + dem_no
        edge = 1.0 - cost
        if cost <= 0 or edge < minimum_edge:
            continue
        candidates.append(
            {
                "state": state,
                "republican": pair["Republican"],
                "democratic": pair["Democratic"],
                "rep_no_cost": rep_no,
                "dem_no_cost": dem_no,
                "pair_cost": cost,
                "edge": edge,
                "top_quantity": min(int(rep_bid["quantity"]), int(dem_bid["quantity"])),
            }
        )
    return candidates, held


def allocate(candidates: list[dict[str, Any]], total_budget: float) -> list[tuple[dict[str, Any], int]]:
    if not candidates:
        raise RuntimeError("No inventory-safe, executable Senate opportunities meet the minimum edge.")
    equal_slice = total_budget / len(candidates)
    plan: list[tuple[dict[str, Any], int]] = []
    for candidate in candidates:
        quantity = min(candidate["top_quantity"], math.floor(equal_slice / candidate["pair_cost"]))
        if quantity > 0:
            plan.append((candidate, quantity))
    if not plan:
        raise RuntimeError("All eligible opportunities are too small for the requested budget.")
    return plan


def print_plan(plan: list[tuple[dict[str, Any], int]], budget: float, held: set[str], execute: bool) -> None:
    total_cost = sum(candidate["pair_cost"] * quantity for candidate, quantity in plan)
    total_profit = sum(candidate["edge"] * quantity for candidate, quantity in plan)
    equal_slice = budget / len(plan)
    print(f"\nInventory-safe Senate allocation — {'LIVE' if execute else 'DRY RUN'}")
    print(f"Existing-position exchanges skipped: {len(held)}")
    print(f"Opportunities allocated: {len(plan)} | Equal target per opportunity: {equal_slice:.2f}")
    for candidate, quantity in plan:
        cost = candidate["pair_cost"] * quantity
        print(f"  {candidate['state']}: {quantity} pairs | cost {cost:.2f} | settlement edge {candidate['edge'] * quantity:.2f}")
    print(f"Total planned cost: {total_cost:.2f} SUSQies (hard cap: {budget:.2f})")
    print(f"Total minimum settlement edge if all legs fill: {total_profit:.2f} SUSQies")
    if not execute:
        print("No order will be sent.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Equal-budget multi-opportunity Senate executor.")
    parser.add_argument("--execute", action="store_true", help="Submit one two-leg order for every planned state.")
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET)
    parser.add_argument("--min-edge", type=float, default=DEFAULT_MIN_EDGE)
    args = parser.parse_args()
    if args.budget <= 0 or args.min_edge < 0:
        raise RuntimeError("Budget must be positive and minimum edge cannot be negative.")

    key = load_key()
    tournament = tournament_id(key)
    candidates, held = candidate_pairs(key, tournament, args.min_edge)
    plan = allocate(candidates, args.budget)
    print_plan(plan, args.budget, held, args.execute)
    if not args.execute:
        return

    for candidate, quantity in plan:
        result = submit_trade(key, tournament, candidate, quantity)
        legs = result.get("results", [])
        filled = all(item.get("data", {}).get("remainingQuantity") == 0 for item in legs)
        print(f"Submitted {candidate['state']}: {'fully filled' if filled else 'check Orders immediately'}.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(f"Executor error: {error}", file=sys.stderr)
        raise SystemExit(1)
