#!/usr/bin/env python3
"""Read-only party-parity scanner for the Predictions Cup.

This program never sends a trade, cancel, or modification request.
It looks for Republican/Democratic pairs with the exact same contest title
(Senate, Governor, House races, and national-control markets).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://www.thesuper.market/api/v1"
TOURNAMENT_SLUG = "midterm-elections"
# The second capture is the complete contest label: e.g. "Michigan Senate",
# "MI-07 House race", "Michigan Governor", or "U.S. House".  Exact matching
# prevents a House district from being paired with a state-wide contest.
TITLE = re.compile(r"^Will the (Republican|Democratic) Party win the (.+?)\?$")


def load_key() -> str:
    """Read the private key from a secret environment variable or .env."""
    environment_key = os.environ.get("SUSQ_API_KEY", "").strip()
    if environment_key:
        return environment_key
    try:
        lines = Path(".env").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as error:
        raise RuntimeError("Missing .env file.") from error
    for line in lines:
        if line.startswith("SUSQ_API_KEY="):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")
            if key and key != "PASTE_YOUR_READ_ONLY_KEY_HERE":
                return key
    raise RuntimeError("SUSQ_API_KEY is missing from .env.")


def get(key: str, path: str, **params: object) -> dict:
    query = urlencode({name: value for name, value in params.items() if value is not None})
    request = Request(
        f"{API}{path}{'?' + query if query else ''}",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API returned HTTP {error.code} for {path}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"Cannot reach the API: {error.reason}") from error


def main() -> None:
    key = load_key()

    tournaments = get(key, "/tournaments").get("data", [])
    tournament = next((item for item in tournaments if item.get("slug") == TOURNAMENT_SLUG), None)
    if tournament is None:
        raise RuntimeError(f"Tournament '{TOURNAMENT_SLUG}' was not found.")

    # Market discovery returns every accessible tournament context for this key.
    # The endpoint is cursor-paginated and capped at 100 markets per request.
    # Tournament context is selected explicitly on the quote requests below.
    markets = []
    cursor = None
    while True:
        page = get(key, "/markets", limit=100, cursor=cursor)
        markets.extend(page.get("data", []))
        pagination = page.get("pagination", {})
        if not pagination.get("hasMore"):
            break
        cursor = pagination.get("nextCursor")
        if not cursor:
            raise RuntimeError("Market pagination reported more results without a next cursor.")
    pairs: dict[str, dict[str, dict]] = {}
    for market in markets:
        match = TITLE.match(str(market.get("title", "")))
        exchanges = market.get("exchanges", [])
        if not match or not exchanges:
            continue
        party, state = match.groups()
        pairs.setdefault(state, {})[party] = {
            "title": market["title"],
            "exchange_id": exchanges[0]["id"],
        }

    complete_pairs = {state: pair for state, pair in pairs.items() if set(pair) == {"Republican", "Democratic"}}
    ids = [item["exchange_id"] for pair in complete_pairs.values() for item in pair.values()]
    quotes: dict[str, dict] = {}
    for start in range(0, len(ids), 100):
        result = get(key, "/exchanges/prices", ids=",".join(ids[start : start + 100]), tournamentId=tournament["id"])
        quotes.update({str(item["exchangeId"]): item for item in result.get("data", [])})

    found = 0
    print(f"\n{tournament['name']} — live party-parity scan\n")
    for state, pair in sorted(complete_pairs.items()):
        republican = quotes.get(str(pair["Republican"]["exchange_id"]))
        democratic = quotes.get(str(pair["Democratic"]["exchange_id"]))
        if not republican or not democratic:
            continue
        # API prices are YES-normalized. A NO offer costs 1 - best YES bid.
        rep_yes_bid = republican.get("bestBid")
        dem_yes_bid = democratic.get("bestBid")
        if rep_yes_bid is None or dem_yes_bid is None:
            continue
        rep_no_cost = 1 - float(rep_yes_bid)
        dem_no_cost = 1 - float(dem_yes_bid)
        total_cost = rep_no_cost + dem_no_cost
        edge = 1 - total_cost
        if edge <= 0:
            continue
        found += 1
        print(state)
        print(f"  Republican NO: {rep_no_cost:.3f}")
        print(f"  Democratic NO: {dem_no_cost:.3f}")
        print(f"  Total cost: {total_cost:.3f}")
        print(f"  Minimum settlement profit: {edge:.3f} per matched pair\n")

    if not found:
        print("No top-of-book Republican/Democratic party-pair opportunities right now.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(f"Scanner error: {error}", file=sys.stderr)
        raise SystemExit(1)
