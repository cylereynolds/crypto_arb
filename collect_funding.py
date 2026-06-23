#!/usr/bin/env python3
"""
Phase 1B data collector: pull HISTORICAL funding rates via ccxt public endpoints
and persist to SQLite. Paginates as far back as each exchange allows.

No keys. No funds. Public market data only.

Run:  .venv/bin/python collect_funding.py [--days 180]
"""
import argparse
import time

import ccxt

from config import (PERP_EXCHANGES, PERP_SYMBOLS, PERP_SYMBOL_FALLBACKS)
from store import db, upsert_funding

MS_DAY = 86_400_000

# Some exchanges reject windows beyond a hard limit on the history endpoint.
MAX_DAYS = {"gate": 170}


def resolve_market(ex, name, want):
    """Return an actual market symbol on this exchange for the wanted symbol."""
    syms = set(ex.symbols or [])
    if want in syms:
        return want
    for alt in PERP_SYMBOL_FALLBACKS.get(name, {}).get(want, []):
        if alt in syms:
            return alt
    # last resort: any swap whose base matches and is USDT/USD settled
    base = want.split("/")[0]
    for m in ex.markets.values():
        if m.get("swap") and m.get("base") == base and m.get("quote") in ("USDT", "USD", "USDC"):
            return m["symbol"]
    return None


def collect_one(name, want, days):
    out = {"exchange": name, "symbol": want, "rows": 0, "span_days": 0, "detail": ""}
    try:
        ex = getattr(ccxt, name)({"enableRateLimit": True, "timeout": 20000,
                                  "options": {"defaultType": "swap"}})
        ex.load_markets()
        if not ex.has.get("fetchFundingRateHistory"):
            out["detail"] = "no fetchFundingRateHistory"
            return out, []
        market = resolve_market(ex, name, want)
        if not market:
            out["detail"] = "no matching perp market"
            return out, []

        since = ex.milliseconds() - days * MS_DAY
        seen = {}
        last_since = None
        stalls = 0
        while True:
            try:
                batch = ex.fetch_funding_rate_history(market, since=since, limit=100)
            except Exception as e:
                out["detail"] = f"page err {type(e).__name__}: {str(e)[:60]}"
                break
            if not batch:
                break
            for r in batch:
                ts = r["timestamp"]
                rate = r.get("fundingRate")
                if ts is not None and rate is not None:
                    seen[ts] = rate
            newest = max(r["timestamp"] for r in batch)
            # advance; guard against exchanges that ignore `since` and loop
            nxt = newest + 1
            if last_since is not None and nxt <= last_since:
                stalls += 1
                if stalls > 2:
                    break
            last_since = nxt
            since = nxt
            if newest >= ex.milliseconds() - MS_DAY:  # caught up to ~now
                break
            time.sleep(ex.rateLimit / 1000)

        rows = [(name, want, market, ts, rate) for ts, rate in sorted(seen.items())]
        if rows:
            span = (rows[-1][3] - rows[0][3]) / MS_DAY
            out.update(rows=len(rows), span_days=round(span, 1),
                       detail=f"market={market}")
        else:
            out["detail"] = f"market={market} but 0 rows"
        return out, rows
    except Exception as e:
        out["detail"] = f"{type(e).__name__}: {str(e)[:80]}"
        return out, []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180,
                    help="how far back to attempt (exchange may cap it)")
    args = ap.parse_args()

    print(f"Collecting funding history (target {args.days}d back)\n")
    total = 0
    with db() as conn:
        for name in PERP_EXCHANGES:
            for want in PERP_SYMBOLS:
                info, rows = collect_one(name, want, args.days)
                if rows:
                    upsert_funding(conn, rows)
                    conn.commit()
                flag = "OK " if info["rows"] else "-- "
                print(f"  [{flag}] {name:14s} {want:14s} "
                      f"rows={info['rows']:>4d} span={info['span_days']:>5}d  "
                      f"{info['detail']}")
                total += info["rows"]
    print(f"\nStored {total} funding rows -> {db().__class__ and 'crypto_poc.db'}")


if __name__ == "__main__":
    main()
