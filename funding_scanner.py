#!/usr/bin/env python3
"""
funding_scanner.py — map funding RICHNESS across reachable perp venues.

Goal: find which perps are *structurally* rich enough to deserve a full friction
sweep — NOT a snapshot rank of current funding (that's a trap). For every perp on
each reachable venue we measure, over the deepest funding history available:

  * gross_apr            mean(rate) x intervals_per_year  (per-venue cadence)
  * pct_positive         fraction of intervals where longs paid shorts
  * longest_neg_streak   worst run of consecutive negative-funding intervals (days)
  * worst_7d_cumulative  most-negative cumulative funding over any rolling 7d window
  * days_of_history      sample depth, to discount shallow series

A high gross APR with a brutal negative tail / low persistence is a risk premium,
not an edge. The shortlist keeps only names rich AND persistent AND deep enough.

Public/keyless ccxt data only. Caches to crypto_poc.db so reruns are fast.

Run:  .venv/bin/python funding_scanner.py [--days 365] [--limit N] [--refresh]
"""
import argparse
import csv
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import ccxt
import numpy as np

from config import DB_PATH
from store import SCHEMA, upsert_funding

MS_DAY = 86_400_000

# Reachable perp venues to scan. kucoinfutures is intentionally skipped — its
# public history caps at ~33d, too short to judge structural richness.
SCAN_VENUES = ["okx", "krakenfutures", "bitget", "gate"]

# Per-venue history window caps (endpoint refuses longer). gate rejects >180d.
MAX_DAYS = {"gate": 170}

SETTLE_QUOTES = ("USDT", "USDC", "USD")     # harvestable linear/inverse perps

# Shortlist gates: rich AND persistent AND deep enough to trust.
MIN_GROSS_APR = 0.08
MIN_PCT_POS = 0.65
MIN_DAYS = 120

CSV_PATH = "funding_scan.csv"
_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Collection (reuses the collector's forward-pagination + day-cap logic)
# ---------------------------------------------------------------------------

def load_exchange(name):
    ex = getattr(ccxt, name)({"enableRateLimit": True, "timeout": 25000,
                              "options": {"defaultType": "swap"}})
    for _ in range(3):
        try:
            ex.load_markets()
            return ex
        except Exception:
            time.sleep(2)
    return None


def list_perps(ex):
    """Active perpetual swaps settled/quoted in USD-like units."""
    out = []
    for m in ex.markets.values():
        if m.get("swap") and m.get("active", True) and m.get("quote") in SETTLE_QUOTES:
            out.append(m["symbol"])
    return sorted(out)


def fetch_funding(ex, name, market, target_days):
    """Paginate funding history forward from (now - capped_days). Returns sorted
    [(ts, rate)]. enableRateLimit throttles; we also guard against endpoints that
    ignore `since` and loop."""
    cap = MAX_DAYS.get(name, target_days)
    days = min(target_days, cap)
    since = ex.milliseconds() - days * MS_DAY
    seen, last, stalls = {}, None, 0
    while True:
        try:
            batch = ex.fetch_funding_rate_history(market, since=since, limit=1000)
        except Exception:
            break
        if not batch:
            break
        for r in batch:
            ts, rate = r.get("timestamp"), r.get("fundingRate")
            if ts is not None and rate is not None:
                seen[ts] = rate
        newest = max(r["timestamp"] for r in batch)
        nxt = newest + 1
        if last is not None and nxt <= last:
            stalls += 1
            if stalls > 2:
                break
        last = since = nxt
        if newest >= ex.milliseconds() - MS_DAY:     # caught up to ~now
            break
    return sorted(seen.items())


def open_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


def scan_venue(name, target_days, refresh, limit, progress):
    """Fetch (or reuse cached) funding history for every perp on one venue."""
    ex = load_exchange(name)
    if ex is None:
        return {"venue": name, "status": "load failed", "perps": 0, "fetched": 0}
    perps = list_perps(ex)
    if limit:
        perps = perps[:limit]
    conn = open_conn()
    have = {r[0] for r in conn.execute(
        "SELECT DISTINCT market FROM funding_history WHERE exchange=?", (name,))}
    fetched = reused = 0
    for i, market in enumerate(perps, 1):
        if market in have and not refresh:
            reused += 1
        else:
            rows = fetch_funding(ex, name, market, target_days)
            if rows:
                tuples = [(name, market, market, ts, rate) for ts, rate in rows]
                with _write_lock:
                    upsert_funding(conn, tuples)
                    conn.commit()
                fetched += 1
        if progress and i % 50 == 0:
            print(f"    [{name}] {i}/{len(perps)} (fetched={fetched} reused={reused})",
                  flush=True)
    conn.close()
    return {"venue": name, "status": "ok", "perps": len(perps),
            "fetched": fetched, "reused": reused}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _longest_run(mask):
    """Longest run of True in a boolean array."""
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def compute_metrics(conn):
    pairs = conn.execute(
        "SELECT DISTINCT exchange, market FROM funding_history "
        "ORDER BY exchange, market").fetchall()
    out = []
    for ex, market in pairs:
        if ex not in SCAN_VENUES:            # e.g. skip cached kucoinfutures rows
            continue
        data = conn.execute(
            "SELECT ts, funding_rate FROM funding_history "
            "WHERE exchange=? AND market=? ORDER BY ts", (ex, market)).fetchall()
        if len(data) < 20:                       # too few points to trust
            continue
        ts = np.array([d[0] for d in data], dtype=float)
        rates = np.array([d[1] for d in data], dtype=float)
        gap_h = float(np.median(np.diff(ts)) / 3_600_000)
        if gap_h <= 0:
            continue
        ipy = (365.0 * 24.0) / gap_h
        gross_apr = float(rates.mean()) * ipy
        pct_pos = float((rates > 0).mean())
        neg_streak = _longest_run(rates < 0)
        neg_streak_days = neg_streak * gap_h / 24.0
        # worst rolling 7-day cumulative funding (you PAID over the worst week)
        wn = max(1, round(7 * 24 / gap_h))
        if len(rates) > wn:
            c = np.concatenate(([0.0], np.cumsum(rates)))
            worst7d = float((c[wn:] - c[:-wn]).min())
        else:
            worst7d = float(rates.sum())
        days = (ts[-1] - ts[0]) / MS_DAY
        out.append({
            "venue": ex, "symbol": market, "gross_apr": gross_apr,
            "pct_positive": pct_pos, "longest_neg_streak_days": neg_streak_days,
            "worst_7d_cumulative": worst7d, "days_of_history": days,
        })
    out.sort(key=lambda r: r["gross_apr"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_row(r):
    print(f"  {r['venue']:<14}{r['symbol']:<22}{r['gross_apr']*100:>9.1f}%"
          f"{r['pct_positive']*100:>8.1f}%{r['longest_neg_streak_days']:>11.1f}d"
          f"{r['worst_7d_cumulative']*100:>13.2f}%{r['days_of_history']:>9.0f}d")


def header():
    print(f"  {'venue':<14}{'symbol':<22}{'gross_apr':>10}{'pct_pos':>8}"
          f"{'neg_streak':>12}{'worst_7d':>13}{'days':>10}")
    print("  " + "-" * 87)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365, help="target history depth")
    ap.add_argument("--limit", type=int, default=None, help="cap perps/venue (testing)")
    ap.add_argument("--refresh", action="store_true", help="refetch even if cached")
    ap.add_argument("--top", type=int, default=60, help="table rows to print")
    ap.add_argument("--no-collect", action="store_true", help="metrics only, skip fetch")
    args = ap.parse_args()

    if not args.no_collect:
        print(f"Scanning funding across {SCAN_VENUES} (target {args.days}d, "
              f"reuse cached={'no' if args.refresh else 'yes'})...")
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=len(SCAN_VENUES)) as pool:
            futs = [pool.submit(scan_venue, v, args.days, args.refresh, args.limit, True)
                    for v in SCAN_VENUES]
            for f in futs:
                r = f.result()
                print(f"  done {r['venue']:<14} status={r['status']} "
                      f"perps={r.get('perps',0)} fetched={r.get('fetched',0)} "
                      f"reused={r.get('reused',0)}")
        print(f"collection took {time.time()-t0:.0f}s")

    conn = open_conn()
    rows = compute_metrics(conn)
    conn.close()

    # Full results -> CSV (the table can be thousands of rows).
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["venue", "symbol", "gross_apr",
                          "pct_positive", "longest_neg_streak_days",
                          "worst_7d_cumulative", "days_of_history"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print("\n" + "=" * 91)
    print(f"  FUNDING RICHNESS MAP — {len(rows)} perps, sorted by gross APR desc "
          f"(top {min(args.top, len(rows))} shown; full set -> {CSV_PATH})")
    print("=" * 91)
    header()
    for r in rows[:args.top]:
        print_row(r)

    # Shortlist: rich AND persistent AND deep — the friction-sweep candidates.
    short = [r for r in rows if r["gross_apr"] > MIN_GROSS_APR
             and r["pct_positive"] > MIN_PCT_POS
             and r["days_of_history"] > MIN_DAYS]
    print("\n" + "=" * 91)
    print(f"  SHORTLIST — gross_apr>{MIN_GROSS_APR*100:.0f}%  "
          f"pct_positive>{MIN_PCT_POS:.2f}  days>{MIN_DAYS}   "
          f"({len(short)} candidates worth a friction sweep)")
    print("=" * 91)
    if short:
        header()
        for r in short:
            print_row(r)
    else:
        print("  none — no perp is simultaneously rich, persistent, and deep enough.")


if __name__ == "__main__":
    main()
