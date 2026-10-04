#!/usr/bin/env python3
"""Live-capable, one-cycle early-exit executor.

Dry run is the default.  Live mode requires both --execute and explicit risk
limits, and never touches a market with inventory that this program did not
open itself.  It fail-stops after any partial fill.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from parity_scanner import API, TITLE, TOURNAMENT_SLUG, get, load_key
from trade_executor import post, tournament_id

STATE_FILE = Path("work/live_recycler_state.json")


def state() -> dict[str, Any]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"open": {}}


def save(data: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def submit_pair(key: str, tournament: str, pair: dict[str, Any], quantity: int, action: str) -> dict[str, Any]:
    return post(key, "/orders/multi-leg", {
        "idempotencyKey": str(uuid.uuid4()),
        "legs": [
            {"exchangeId": pair["rep_id"], "tournamentId": tournament, "side": "no", "action": action, "quantity": quantity, "price": pair["rep_no_price"]},
            {"exchangeId": pair["dem_id"], "tournamentId": tournament, "side": "no", "action": action, "quantity": quantity, "price": pair["dem_no_price"]},
        ],
    })


def fully_filled(result: dict[str, Any]) -> bool:
    legs = result.get("results", [])
    return len(legs) == 2 and all(item.get("data", {}).get("remainingQuantity") == 0 for item in legs)


def confirmed_filled(key: str, result: dict[str, Any]) -> bool:
    """Allow the engine projection a few seconds to finish a just-crossed pair."""
    if fully_filled(result):
        return True
    order_ids = [item.get("data", {}).get("orderId") for item in result.get("results", [])]
    if len(order_ids) != 2 or any(order_id is None for order_id in order_ids):
        return False
    for _ in range(3):
        time.sleep(2)
        orders = [get(key, f"/orders/{order_id}") for order_id in order_ids]
        if all(not order.get("open") and order.get("quantityFilled") == order.get("quantity") for order in orders):
            return True
    return False


def discover(key: str, tournament: str) -> tuple[dict[str, dict[str, str]], set[str]]:
    positions = get(key, f"/tournaments/{TOURNAMENT_SLUG}/portfolio/positions").get("positions", [])
    held = {str(item["exchangeId"]) for item in positions if float(item.get("quantity", 0)) != 0}
    markets, cursor = [], None
    while True:
        page = get(key, "/markets", limit=100, cursor=cursor)
        markets.extend(page.get("data", []))
        paging = page.get("pagination", {})
        if not paging.get("hasMore"):
            break
        cursor = paging.get("nextCursor")
    grouped: dict[str, dict[str, dict[str, str]]] = {}
    for market in markets:
        match, exchanges = TITLE.match(str(market.get("title", ""))), market.get("exchanges", [])
        if match and exchanges:
            party, market_state = match.groups()
            grouped.setdefault(market_state, {})[party] = {"id": str(exchanges[0]["id"])}
    return grouped, held


def book_pair(key: str, tournament: str, grouped: dict[str, dict[str, dict[str, str]]], market_state: str, for_exit: bool) -> dict[str, Any] | None:
    pair = grouped.get(market_state, {})
    if set(pair) != {"Republican", "Democratic"}:
        return None
    rep = get(key, f"/exchanges/{pair['Republican']['id']}/orderbook", depth=1, tournamentId=tournament)
    dem = get(key, f"/exchanges/{pair['Democratic']['id']}/orderbook", depth=1, tournamentId=tournament)
    # NO buy takes YES bids; NO sell takes YES asks.
    levels = (rep.get("asks"), dem.get("asks")) if for_exit else (rep.get("bids"), dem.get("bids"))
    if not levels[0] or not levels[1]:
        return None
    return {
        "rep_id": pair["Republican"]["id"], "dem_id": pair["Democratic"]["id"],
        "rep_no_price": 1 - float(levels[0][0]["price"]), "dem_no_price": 1 - float(levels[1][0]["price"]),
        "quantity": min(int(levels[0][0]["quantity"]), int(levels[1][0]["quantity"])),
    }


def ranked_entry_states(key: str, tournament: str, grouped: dict[str, dict[str, dict[str, str]]], held: set[str], open_states: set[str], minimum_edge: float) -> list[str]:
    """Use one bulk-price request to rank candidates before book validation."""
    eligible = {
        name: contracts for name, contracts in grouped.items()
        if name not in open_states and set(contracts) == {"Republican", "Democratic"}
        and contracts["Republican"]["id"] not in held and contracts["Democratic"]["id"] not in held
    }
    ids = [contract["id"] for contracts in eligible.values() for contract in contracts.values()]
    prices: dict[str, dict[str, Any]] = {}
    for start in range(0, len(ids), 100):
        response = get(key, "/exchanges/prices", ids=",".join(ids[start:start + 100]), tournamentId=tournament)
        prices.update({str(item["exchangeId"]): item for item in response.get("data", [])})
    ranked: list[tuple[float, str]] = []
    for name, contracts in eligible.items():
        rep, dem = prices.get(contracts["Republican"]["id"]), prices.get(contracts["Democratic"]["id"])
        if not rep or not dem or rep.get("bestBid") is None or dem.get("bestBid") is None:
            continue
        edge = float(rep["bestBid"]) + float(dem["bestBid"]) - 1.0
        if edge >= minimum_edge:
            ranked.append((edge, name))
    return [name for _, name in sorted(ranked, reverse=True)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Live-capable, fail-stop Senate recycler.")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--max-total", type=float, help="Required total new-entry cap in live mode.")
    parser.add_argument("--max-per-state", type=float, help="Required new-entry cap per state in live mode.")
    parser.add_argument("--entry-edge", type=float, default=0.01)
    parser.add_argument("--exit-profit", type=float, default=0.005)
    args = parser.parse_args()
    if args.execute and (not args.max_total or not args.max_per_state):
        raise RuntimeError("Live mode requires explicit --max-total and --max-per-state limits.")
    key, tournament = load_key(), tournament_id(load_key())
    grouped, held = discover(key, tournament)
    data = state()

    # First, consider only exits from lots this file recorded itself.
    for market_state, lot in list(data["open"].items()):
        pair = book_pair(key, tournament, grouped, market_state, for_exit=True)
        if not pair:
            continue
        exit_value = pair["rep_no_price"] + pair["dem_no_price"]
        if exit_value - float(lot["entry_cost"]) < args.exit_profit:
            continue
        print(f"EXIT {market_state}: {lot['quantity']} pairs, locked profit {(exit_value - lot['entry_cost']) * lot['quantity']:.2f}")
        if args.execute:
            result = submit_pair(key, tournament, pair, int(lot["quantity"]), "sell")
            if not confirmed_filled(key, result):
                raise RuntimeError(f"Partial exit in {market_state}; stopped. Check Orders before continuing.")
            del data["open"][market_state]
            save(data)

    used = sum(float(lot["entry_cost"]) * int(lot["quantity"]) for lot in data["open"].values())
    total_cap = args.max_total or 0.0
    per_state_cap = args.max_per_state or 0.0
    if args.execute and total_cap - used < 1.0:
        print(f"Entry cap already allocated ({used:.2f}/{total_cap:.2f}); no new entries this cycle.")
        print("Live cycle completed.")
        return
    for market_state in ranked_entry_states(key, tournament, grouped, held, set(data["open"]), args.entry_edge):
        if args.execute and used >= total_cap:
            break
        pair = book_pair(key, tournament, grouped, market_state, for_exit=False)
        if not pair:
            continue
        cost = pair["rep_no_price"] + pair["dem_no_price"]
        if 1 - cost < args.entry_edge:
            continue
        available = min(per_state_cap, total_cap - used) if args.execute else 100.0
        quantity = min(pair["quantity"], math.floor(available / cost))
        if quantity <= 0:
            continue
        print(f"ENTRY {market_state}: {quantity} pairs, maximum cost {quantity * cost:.2f}")
        if args.execute:
            result = submit_pair(key, tournament, pair, quantity, "buy")
            if not confirmed_filled(key, result):
                raise RuntimeError(f"Partial entry in {market_state}; stopped. Check Orders before continuing.")
            data["open"][market_state] = {"quantity": quantity, "entry_cost": cost}
            used += quantity * cost
            save(data)

    print("DRY RUN only." if not args.execute else "Live cycle completed.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(f"Live recycler error: {error}")
