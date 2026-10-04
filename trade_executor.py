#!/usr/bin/env python3
"""One-shot, budget-capped Senate-party parity executor.

By default this program is a dry run: it reads the live order book and prints
the exact pair it *would* submit.  It cannot submit anything unless invoked
with ``--execute``.  The trade API key stays in .env and is never printed.

The strategy buys equal quantities of:
  * Republican NO, and
  * Democratic NO

for a single state Senate market.  This only makes sense when the two market
rules really are mutually exclusive and their settlement rules match.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from parity_scanner import API, TITLE, TOURNAMENT_SLUG, get, load_key


DEFAULT_BUDGET = 400.0
DEFAULT_MIN_EDGE = 0.01  # Ignore apparent edges smaller than one cent per pair.


def post(key: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    """Send one authenticated JSON request, without logging credentials."""
    request = Request(
        f"{API}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Trade API returned HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"Cannot reach the trade API: {error.reason}") from error


def tournament_id(key: str) -> str:
    tournaments = get(key, "/tournaments").get("data", [])
    tournament = next((item for item in tournaments if item.get("slug") == TOURNAMENT_SLUG), None)
    if tournament is None:
        raise RuntimeError(f"Tournament '{TOURNAMENT_SLUG}' was not found.")
    return str(tournament["id"])


def find_trade(
    key: str, tournament: str, budget: float, min_edge: float, requested_state: str | None
) -> tuple[dict[str, Any], int]:
    """Find an executable pair without reading every order book individually."""
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
        grouped.setdefault(state, {})[party] = {
            "title": str(market["title"]),
            "exchange_id": str(exchanges[0]["id"]),
        }

    complete_pairs = {
        state: pair
        for state, pair in grouped.items()
        if set(pair) == {"Republican", "Democratic"}
        and (requested_state is None or state.casefold() == requested_state.casefold())
    }
    exchange_ids = [contract["exchange_id"] for pair in complete_pairs.values() for contract in pair.values()]
    quotes: dict[str, dict[str, Any]] = {}
    for start in range(0, len(exchange_ids), 100):
        response = get(
            key,
            "/exchanges/prices",
            ids=",".join(exchange_ids[start : start + 100]),
            tournamentId=tournament,
        )
        quotes.update({str(item["exchangeId"]): item for item in response.get("data", [])})

    # Rank cheaply with bulk quotes, then recheck the best candidates against
    # their own live order books immediately before a submission.
    ranked: list[tuple[float, str, dict[str, dict[str, str]]]] = []
    for state, pair in complete_pairs.items():
        rep_quote = quotes.get(pair["Republican"]["exchange_id"])
        dem_quote = quotes.get(pair["Democratic"]["exchange_id"])
        if not rep_quote or not dem_quote:
            continue
        rep_bid = rep_quote.get("bestBid")
        dem_bid = dem_quote.get("bestBid")
        if rep_bid is None or dem_bid is None:
            continue
        quoted_edge = float(rep_bid) + float(dem_bid) - 1.0
        if quoted_edge >= min_edge:
            ranked.append((quoted_edge, state, pair))

    for _, state, pair in sorted(ranked, key=lambda item: (-item[0], item[1])):
        # The API exposes each order book in YES terms.  The top YES bid is
        # therefore an immediately executable NO-buy price of 1 - best_bid.
        rep_book = get(key, f"/exchanges/{pair['Republican']['exchange_id']}/orderbook", depth=1, tournamentId=tournament)
        dem_book = get(key, f"/exchanges/{pair['Democratic']['exchange_id']}/orderbook", depth=1, tournamentId=tournament)
        rep_bids = rep_book.get("bids", [])
        dem_bids = dem_book.get("bids", [])
        if not rep_bids or not dem_bids:
            continue

        rep_yes_bid = float(rep_bids[0]["price"])
        dem_yes_bid = float(dem_bids[0]["price"])
        rep_no_cost = 1.0 - rep_yes_bid
        dem_no_cost = 1.0 - dem_yes_bid
        pair_cost = rep_no_cost + dem_no_cost
        edge = 1.0 - pair_cost
        if pair_cost <= 0 or edge < min_edge:
            continue
        candidate = {
            "state": state,
            "republican": pair["Republican"],
            "democratic": pair["Democratic"],
            "rep_no_cost": rep_no_cost,
            "dem_no_cost": dem_no_cost,
            "pair_cost": pair_cost,
            "edge": edge,
            "top_quantity": min(int(rep_bids[0]["quantity"]), int(dem_bids[0]["quantity"])),
        }
        quantity = min(candidate["top_quantity"], math.floor(budget / candidate["pair_cost"]))
        if quantity > 0:
            return candidate, quantity
    requested = f" for {requested_state}" if requested_state else ""
    raise RuntimeError(f"No executable Senate NO-pair meets the edge and budget rules{requested}.")


def print_plan(candidate: dict[str, Any], quantity: int, budget: float, execute: bool) -> None:
    total_cost = quantity * candidate["pair_cost"]
    print("\nSenate parity trade plan")
    print(f"  State: {candidate['state']}")
    print(f"  Buy {quantity} Republican NO at {candidate['rep_no_cost']:.3f}")
    print(f"  Buy {quantity} Democratic NO at {candidate['dem_no_cost']:.3f}")
    print(f"  Maximum entry cost: {total_cost:.2f} SUSQies (cap: {budget:.2f})")
    print(f"  Settlement value if both legs fill: {quantity:.2f} SUSQies")
    print(f"  Minimum settlement profit if both legs fill: {quantity * candidate['edge']:.2f} SUSQies")
    print("  Mode: LIVE SUBMISSION" if execute else "  Mode: DRY RUN — no order will be sent")


def submit_trade(key: str, tournament: str, candidate: dict[str, Any], quantity: int) -> dict[str, Any]:
    # For a NO order, use the displayed NO price.  The official API converts
    # NO prices to its YES-normalized book internally.
    body = {
        "idempotencyKey": str(uuid.uuid4()),
        "legs": [
            {
                "exchangeId": candidate["republican"]["exchange_id"],
                "tournamentId": tournament,
                "side": "no",
                "action": "buy",
                "quantity": quantity,
                "price": candidate["rep_no_cost"],
            },
            {
                "exchangeId": candidate["democratic"]["exchange_id"],
                "tournamentId": tournament,
                "side": "no",
                "action": "buy",
                "quantity": quantity,
                "price": candidate["dem_no_cost"],
            },
        ],
    }
    return post(key, "/orders/multi-leg", body)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Budget-capped Senate NO-pair executor.")
    parser.add_argument("--execute", action="store_true", help="Actually submit one atomic multi-leg order.")
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET, help="Hard maximum total entry cost (default: 400).")
    parser.add_argument("--min-edge", type=float, default=DEFAULT_MIN_EDGE, help="Minimum profit per matched pair (default: 0.01).")
    parser.add_argument("--state", help="Only consider one state, e.g. 'New Hampshire'.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.budget <= 0 or args.min_edge < 0:
        raise RuntimeError("Budget must be positive and minimum edge cannot be negative.")

    key = load_key()
    tournament = tournament_id(key)
    candidate, quantity = find_trade(key, tournament, args.budget, args.min_edge, args.state)
    print_plan(candidate, quantity, args.budget, args.execute)

    if not args.execute:
        return

    result = submit_trade(key, tournament, candidate, quantity)
    print("\nSubmission accepted by the API. Check the platform's Orders/Positions page for fills.")
    print(json.dumps(result, indent=2))
    print("\nImportant: atomic submission means both orders were accepted together; it does not guarantee both fill."
          " Do not treat the profit as locked in until the two filled quantities match.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(f"Executor error: {error}", file=sys.stderr)
        raise SystemExit(1)
