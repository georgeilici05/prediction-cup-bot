#!/usr/bin/env python3
"""Paper-trade a higher-turnover Senate parity strategy.

This file NEVER posts an order.  It records hypothetical entries when buying
both NO contracts is below one SUSQie, then records an early exit only when
selling both positions at current *bids* would lock in a profit after spreads.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from parity_scanner import TITLE, TOURNAMENT_SLUG, get, load_key
from trade_executor import tournament_id


STATE_FILE = Path("work/paper_recycler_state.json")


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"open": {}, "closed": [], "realized_profit": 0.0}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def senate_pairs(key: str, tournament: str) -> dict[str, dict[str, str]]:
    markets: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = get(key, "/markets", limit=100, cursor=cursor)
        markets.extend(page.get("data", []))
        pagination = page.get("pagination", {})
        if not pagination.get("hasMore"):
            break
        cursor = pagination.get("nextCursor")
        if not cursor:
            raise RuntimeError("Market pagination reported more results without a next cursor.")
    grouped: dict[str, dict[str, dict[str, str]]] = {}
    for market in markets:
        match = TITLE.match(str(market.get("title", "")))
        exchanges = market.get("exchanges", [])
        if not match or not exchanges:
            continue
        party, state = match.groups()
        grouped.setdefault(state, {})[party] = {"exchange_id": str(exchanges[0]["id"])}
    return {
        state: pair
        for state, pair in grouped.items()
        if set(pair) == {"Republican", "Democratic"}
    }


def quotes(key: str, tournament: str, pairs: dict[str, dict[str, dict[str, str]]]) -> dict[str, dict[str, float]]:
    ids = [contract["exchange_id"] for pair in pairs.values() for contract in pair.values()]
    result: dict[str, dict[str, Any]] = {}
    for start in range(0, len(ids), 100):
        page = get(key, "/exchanges/prices", ids=",".join(ids[start : start + 100]), tournamentId=tournament)
        result.update({str(item["exchangeId"]): item for item in page.get("data", [])})
    return result


def scan_once(key: str, tournament: str, notional: float, entry_edge: float, exit_profit: float) -> None:
    state = load_state()
    pairs = senate_pairs(key, tournament)
    price_data = quotes(key, tournament, pairs)
    now = datetime.now(timezone.utc).isoformat()
    entries = exits = 0

    for market_state, pair in pairs.items():
        republican = price_data.get(pair["Republican"]["exchange_id"])
        democratic = price_data.get(pair["Democratic"]["exchange_id"])
        if not republican or not democratic:
            continue
        if any(value is None for value in (republican.get("bestBid"), republican.get("bestAsk"), democratic.get("bestBid"), democratic.get("bestAsk"))):
            continue

        # To enter we buy NO by taking YES bids.  To exit we sell NO by taking
        # YES asks.  Both values are executable, spread-adjusted prices.
        entry_cost = 2.0 - float(republican["bestBid"]) - float(democratic["bestBid"])
        exit_value = 2.0 - float(republican["bestAsk"]) - float(democratic["bestAsk"])
        edge = 1.0 - entry_cost
        open_lot = state["open"].get(market_state)

        if open_lot is None and edge >= entry_edge:
            quantity = math.floor(notional / entry_cost)
            if quantity > 0:
                state["open"][market_state] = {
                    "quantity": quantity,
                    "entry_cost_per_pair": round(entry_cost, 6),
                    "entered_at": now,
                }
                entries += 1
                print(f"PAPER BUY  {market_state}: {quantity} pairs at {entry_cost:.3f}")
        elif open_lot is not None:
            profit_per_pair = exit_value - float(open_lot["entry_cost_per_pair"])
            if profit_per_pair >= exit_profit:
                quantity = int(open_lot["quantity"])
                profit = quantity * profit_per_pair
                state["closed"].append(
                    {
                        "state": market_state,
                        "quantity": quantity,
                        "entry_cost_per_pair": open_lot["entry_cost_per_pair"],
                        "exit_value_per_pair": round(exit_value, 6),
                        "profit": round(profit, 6),
                        "entered_at": open_lot["entered_at"],
                        "exited_at": now,
                    }
                )
                state["realized_profit"] += profit
                del state["open"][market_state]
                exits += 1
                print(f"PAPER SELL {market_state}: {quantity} pairs at {exit_value:.3f}; profit {profit:.2f}")

    save_state(state)
    print(f"\nScan complete: {entries} hypothetical entries, {exits} hypothetical exits.")
    print(f"Open paper positions: {len(state['open'])}")
    print(f"Realized paper profit: {state['realized_profit']:.2f} SUSQies")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only, early-exit parity paper trader.")
    parser.add_argument("--notional", type=float, default=100.0, help="Paper SUSQies per state (default: 100).")
    parser.add_argument("--entry-edge", type=float, default=0.01, help="Minimum entry edge per pair (default: 0.01).")
    parser.add_argument("--exit-profit", type=float, default=0.005, help="Minimum locked early-exit profit per pair (default: 0.005).")
    parser.add_argument("--cycles", type=int, default=1, help="Number of scans (default: 1).")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between scans (default: 60).")
    args = parser.parse_args()
    if args.notional <= 0 or args.entry_edge < 0 or args.exit_profit < 0 or args.cycles <= 0 or args.interval <= 0:
        raise RuntimeError("Notional, cycles, and interval must be positive; thresholds cannot be negative.")

    key = load_key()
    tournament = tournament_id(key)
    for scan_number in range(args.cycles):
        print(f"\n--- Paper scan {scan_number + 1}/{args.cycles} ---")
        scan_once(key, tournament, args.notional, args.entry_edge, args.exit_profit)
        if scan_number + 1 < args.cycles:
            time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(f"Paper recycler error: {error}")
