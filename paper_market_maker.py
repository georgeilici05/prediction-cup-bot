#!/usr/bin/env python3
"""Read-only, conservative paper market-maker for party election contracts.

It never sends an order. A hypothetical limit order fills only after a later
public best price crosses it; this intentionally understates fill probability.
"""
from __future__ import annotations
import argparse, json, time
from pathlib import Path
from typing import Any
from parity_scanner import TITLE, TOURNAMENT_SLUG, get, load_key
from trade_executor import tournament_id

STATE = Path("work/paper_market_maker_state.json")

def load() -> dict[str, Any]:
    return json.loads(STATE.read_text()) if STATE.exists() else {"quotes": {}, "positions": {}, "realized": 0.0, "fills": []}

def save(data: dict[str, Any]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True); STATE.write_text(json.dumps(data, indent=2))

def exchanges(key: str) -> list[str]:
    markets, cursor = [], None
    while True:
        page=get(key,"/markets",limit=100,cursor=cursor); markets += page.get("data",[])
        p=page.get("pagination",{})
        if not p.get("hasMore"): break
        cursor=p.get("nextCursor")
    return [str(e["id"]) for m in markets if TITLE.match(str(m.get("title",""))) for e in m.get("exchanges",[])]

def snapshot(key: str, tournament: str, ids: list[str]) -> dict[str, dict[str,float]]:
    result={}
    for start in range(0,len(ids),100):
        data=get(key,"/exchanges/prices",ids=",".join(ids[start:start+100]),tournamentId=tournament).get("data",[])
        for q in data:
            if q.get("bestBid") is not None and q.get("bestAsk") is not None:
                result[str(q["exchangeId"])]={"bid":float(q["bestBid"]),"ask":float(q["bestAsk"])}
    return result

def scan(key: str, tournament: str, max_shares: int, target: float) -> None:
    data=load(); prices=snapshot(key,tournament,exchanges(key))
    for eid,q in prices.items():
        quote=data["quotes"].get(eid); position=data["positions"].get(eid)
        # A paper buy fills only when later sellers offer at or below its quote.
        if quote and quote["side"]=="buy" and q["ask"] <= quote["price"]:
            data["positions"][eid]={"quantity":max_shares,"entry":quote["price"]}; data["fills"].append({"exchange":eid,"side":"buy","price":quote["price"]}); print(f"PAPER BUY {eid} @ {quote['price']:.3f}"); quote=None
        # A paper sell fills only when later buyers bid at or above its quote.
        if quote and quote["side"]=="sell" and q["bid"] >= quote["price"] and position:
            pnl=(quote["price"]-position["entry"])*position["quantity"]; data["realized"]+=pnl; data["fills"].append({"exchange":eid,"side":"sell","price":quote["price"],"pnl":pnl}); del data["positions"][eid]; print(f"PAPER SELL {eid} @ {quote['price']:.3f}, pnl {pnl:.2f}"); quote=None
        if quote is None:
            if eid in data["positions"]:
                entry=data["positions"][eid]["entry"]; data["quotes"][eid]={"side":"sell","price":round(max(q["ask"],entry+target),3)}
            else:
                data["quotes"][eid]={"side":"buy","price":round(q["bid"],3)}
    save(data)
    print(f"Open paper inventory: {len(data['positions'])} contracts; realized P&L: {data['realized']:.2f}")

def main() -> None:
    p=argparse.ArgumentParser(); p.add_argument("--cycles",type=int,default=1); p.add_argument("--interval",type=int,default=60); p.add_argument("--shares",type=int,default=25); p.add_argument("--target",type=float,default=.01); a=p.parse_args()
    if min(a.cycles,a.interval,a.shares)<=0 or a.target<=0: raise RuntimeError("All settings must be positive.")
    key=load_key(); t=tournament_id(key)
    for i in range(a.cycles):
        print(f"--- Paper market-maker scan {i+1}/{a.cycles} ---"); scan(key,t,a.shares,a.target)
        if i+1<a.cycles: time.sleep(a.interval)
if __name__=="__main__": main()
