#!/usr/bin/env python3
"""
Phase 1B backtest: delta-neutral funding harvest (short perp + long spot),
net of realistic frictions. Reads funding_history from SQLite, reports
net-of-cost annualized return + risk metrics, and an honest per-market verdict.

No keys, no funds — operates entirely on collected public funding history.

Run:  .venv/bin/python backtest_funding.py
"""
import sqlite3
import statistics

import numpy as np

from config import (FEES, SLIPPAGE_BPS_PER_LEG, CAPITAL_MULTIPLE,
                    REBALANCE_DRAG_BPS_PER_DAY, BORROW_APR,
                    COND_TRAILING_INTERVALS, DB_PATH)

MS_DAY = 86_400_000

# ============================================================================
# COST MODEL  — every friction that turns gross funding into capturable PnL.
# All returns are fractions of NOTIONAL unless stated. Annualization uses the
# market's own funding cadence (Kraken perps fund hourly; most others 8h).
# ============================================================================

def fee(exchange):
    """Per-side taker fees for this venue's spot and perp legs."""
    return FEES.get(exchange, FEES["_default"])


def intervals_per_year(median_gap_hours):
    """Funding payments per year implied by the observed median funding gap."""
    return (365.0 * 24.0) / median_gap_hours


def roundtrip_cost_frac(exchange, slip_bps=None):
    """
    One full in-and-out of a delta-neutral hedge:
      entry  = buy spot  (taker+slip) + short perp (taker+slip)
      exit   = sell spot (taker+slip) + cover perp (taker+slip)
    => 2x spot taker + 2x perp taker + 4 leg-crosses of slippage.
    `slip_bps` overrides the config default (used by the slippage sweep).
    """
    f = fee(exchange)
    slip = (SLIPPAGE_BPS_PER_LEG if slip_bps is None else slip_bps) / 1e4
    return 2 * f["spot_taker"] + 2 * f["perp_taker"] + 4 * slip


def rebalance_drag_frac(days_held, drag_bps_per_day=None):
    """Delta-rebalancing taker drag accrued over the holding period.
    `drag_bps_per_day` overrides the config default (used by the drag sweep)."""
    drag = REBALANCE_DRAG_BPS_PER_DAY if drag_bps_per_day is None else drag_bps_per_day
    return (drag / 1e4) * days_held


def borrow_cost_frac(days_held):
    """Margin/borrow interest. ~0 for unlevered same-venue cash+perp."""
    return BORROW_APR * (days_held / 365.0)


# ============================================================================
# PnL FUNCTIONS  — apply the cost model to a funding-rate series.
# `rates` is the ordered list of per-interval funding rates (fractions). We are
# SHORT the perp, so we RECEIVE +rate each interval when rate>0 and PAY when <0.
# The spot long cancels perp price PnL (delta-neutral), so PnL == funding flow.
# ============================================================================

def harvest_always_on(rates, exchange, days_held, slip_bps=None, drag_bps_per_day=None):
    """
    Hold the hedge for the whole window. Returns a dict of PnL components, all as
    fractions of notional, plus the funding+drag equity path for drawdown.

    `gross` is the period funding total (sign-aware sum); it feeds `net`. For
    cross-venue comparison use an APR built from mean(rates) x intervals/yr, not
    this sum, because funding cadences differ.
    """
    rates = np.asarray(rates, dtype=float)
    n = len(rates)
    gross = float(rates.sum())                                  # period funding (sign-aware)
    rt = roundtrip_cost_frac(exchange, slip_bps)                # one entry + one exit
    rebal = rebalance_drag_frac(days_held, drag_bps_per_day)
    borrow = borrow_cost_frac(days_held)
    net = gross - rt - rebal - borrow

    # Equity path = funding minus per-interval rebalance drag, ONE node per
    # interval. Round-trip entry/exit cost is deliberately NOT in the path:
    # paying to enter is not a drawdown. A dip here means funding actually bled.
    per_interval_rebal = rebal / n if n else 0.0
    path = np.cumsum(rates - per_interval_rebal)
    return {
        "gross": gross, "roundtrip": rt, "rebal": rebal, "borrow": borrow,
        "net": net, "path": path, "n": n,
    }


def harvest_conditional(rates, exchange, days_held, trailing=COND_TRAILING_INTERVALS,
                        slip_bps=None, drag_bps_per_day=None):
    """
    Only hold while trailing-mean funding is positive; sit flat otherwise.
    Pays a fresh round trip each time we re-enter (this is what kills naive
    'just turn it off' strategies — flip costs eat the saved negative funding).

    The entry signal uses STRICTLY PRIOR bars rates[i-trailing:i]; on the first
    bar the window is empty so the cold start is FLAT (no peeking at the current
    bar's funding to decide whether to collect that same bar). The equity path
    is funding+drag only, one node per interval; round-trip entry/exit cost is
    accumulated separately and subtracted into `net`, never into the path.
    """
    rates = np.asarray(rates, dtype=float)
    n = len(rates)
    rt = roundtrip_cost_frac(exchange, slip_bps)
    drag = REBALANCE_DRAG_BPS_PER_DAY if drag_bps_per_day is None else drag_bps_per_day
    gap_days = days_held / n if n else 0.0
    per_interval_rebal = (drag / 1e4) * gap_days

    in_pos = False
    gross = 0.0                       # funding collected while in position
    roundtrip_cost = 0.0              # entry/exit costs only (kept off the path)
    entries = 0
    equity = 0.0                      # funding+drag equity, in NOTIONAL fractions
    path = np.empty(n)
    for i, r in enumerate(rates):
        window = rates[max(0, i - trailing):i]          # strictly prior bars
        signal = bool(window.mean() > 0) if window.size else False  # cold start flat
        if signal and not in_pos:
            in_pos = True
            entries += 1
            roundtrip_cost += rt / 2.0                   # entry cost (NOT in path)
        elif not signal and in_pos:
            in_pos = False
            roundtrip_cost += rt / 2.0                   # exit cost (NOT in path)
        if in_pos:
            gross += r
            equity += r - per_interval_rebal             # funding+drag only
        path[i] = equity                                 # exactly one node / interval
    if in_pos:                                           # close at end
        roundtrip_cost += rt / 2.0
    net = float(equity) - roundtrip_cost
    return {"gross": gross, "roundtrip": roundtrip_cost, "net": net,
            "entries": entries, "path": path, "n": n}


def max_drawdown(path):
    """Largest peak-to-trough decline of a cumulative-PnL path (fraction)."""
    peak = np.maximum.accumulate(path)
    return float((path - peak).min())


def longest_negative_streak(rates):
    """Most consecutive funding intervals with rate<=0 (worst dry spell)."""
    best = cur = 0
    for r in rates:
        cur = cur + 1 if r <= 0 else 0
        best = max(best, cur)
    return best


def annualize(net_frac, days_held):
    """Net fraction over the window -> simple annualized rate."""
    return net_frac * (365.0 / days_held) if days_held else 0.0


# ============================================================================
# REPORT
# ============================================================================

def load_series(conn, exchange, symbol):
    rows = conn.execute(
        "SELECT ts, funding_rate FROM funding_history "
        "WHERE exchange=? AND symbol=? ORDER BY ts", (exchange, symbol)
    ).fetchall()
    ts = [r[0] for r in rows]
    rates = [r[1] for r in rows]
    return ts, rates


def verdict(net_apr_capital, max_dd_frac):
    if net_apr_capital <= 0:
        return "NO EDGE — net return <=0 after frictions"
    # crude risk gate: return must clear a hurdle vs its own drawdown
    if net_apr_capital < 0.03:
        return "MARGINAL — positive but below a 3%/yr hurdle; not worth the risk"
    if max_dd_frac < -0.05 and net_apr_capital < 0.10:
        return "RISKY — return doesn't justify drawdown/basis risk"
    return "REAL — net positive, risk-justified; candidate for paper trading"


def main():
    conn = sqlite3.connect(DB_PATH)
    pairs = conn.execute(
        "SELECT DISTINCT exchange, symbol FROM funding_history "
        "ORDER BY exchange, symbol").fetchall()

    print("=" * 92)
    print("  PHASE 1B — DELTA-NEUTRAL FUNDING HARVEST, NET OF FRICTIONS")
    print(f"  slippage={SLIPPAGE_BPS_PER_LEG}bp/leg  rebal={REBALANCE_DRAG_BPS_PER_DAY}bp/day"
          f"  capital_mult={CAPITAL_MULTIPLE}x  borrow_apr={BORROW_APR}")
    print("=" * 92)
    hdr = (f"{'market':<28} {'days':>5} {'%pos':>5} {'grossAPR':>9} "
           f"{'netAPR(cap)':>11} {'maxDD':>7} {'negStrk':>7} {'cond.netAPR':>11}  verdict")
    print(hdr)
    print("-" * len(hdr))

    for ex, sym in pairs:
        ts, rates = load_series(conn, ex, sym)
        if len(rates) < 10:
            continue
        gaps = [(ts[i + 1] - ts[i]) / 3_600_000 for i in range(len(ts) - 1)]
        gap_h = statistics.median(gaps)
        days = (ts[-1] - ts[0]) / MS_DAY
        ipy = intervals_per_year(gap_h)

        always = harvest_always_on(rates, ex, days)
        cond = harvest_conditional(rates, ex, days)

        gross_apr = statistics.mean(rates) * ipy
        net_apr_notional = annualize(always["net"], days)
        net_apr_cap = net_apr_notional / CAPITAL_MULTIPLE
        cond_apr_cap = annualize(cond["net"], days) / CAPITAL_MULTIPLE
        pct_pos = 100.0 * sum(1 for r in rates if r > 0) / len(rates)
        dd = max_drawdown(always["path"])
        negstreak = longest_negative_streak(rates)

        v = verdict(net_apr_cap, dd)
        label = f"{ex}:{sym.split('/')[0]}"
        print(f"{label:<28} {days:>5.0f} {pct_pos:>4.0f}% {gross_apr*100:>8.2f}% "
              f"{net_apr_cap*100:>10.2f}% {dd*100:>6.2f}% {negstreak:>7d} "
              f"{cond_apr_cap*100:>10.2f}%  {v}")

    print("-" * len(hdr))
    print("Notes: APRs are on CAPITAL (notional x capital_mult). grossAPR is pre-cost on "
          "notional.\n       maxDD/negStrk gauge funding-flip risk. 'cond' = hold-only-"
          "when-trailing-funding>0.")
    conn.close()


def kraken_verdict_report():
    """
    Stress the funding harvest on the REAL Kraken Futures BTC perp series (the
    only ~365d window in the DB), sweeping the frictions that actually decide
    the verdict. Hourly cadence => mean(rate) x intervals/yr is the only honest
    APR; the raw sum is shown but never used for comparison.
    """
    conn = sqlite3.connect(DB_PATH)
    ex, sym = "krakenfutures", "BTC/USDT:USDT"
    ts, rates = load_series(conn, ex, sym)
    conn.close()
    rates = np.asarray(rates, dtype=float)
    if rates.size < 100:
        print("\nKraken 365d BTC series not found in DB — run collect_funding.py first.")
        return

    gap_h = float(np.median(np.diff(ts)) / 3_600_000)
    days = (ts[-1] - ts[0]) / MS_DAY
    ipy = intervals_per_year(gap_h)
    gross_apr = float(rates.mean()) * ipy
    negstrk = longest_negative_streak(rates)
    negstrk_days = negstrk * gap_h / 24.0

    SLIPS = [1, 3, 5]            # bps per leg-cross
    DRAGS = [0.5, 1.0, 1.5]      # rebalance bps per day
    CAPS = [1.2, 1.6, 2.0]       # capital multiple (1.2 levered/liq-risk .. 2.0 unlevered-safe)
    base_drag = REBALANCE_DRAG_BPS_PER_DAY

    print("\n" + "=" * 78)
    print("  VERDICT HARNESS — real Kraken Futures BTC perp (crypto_poc.db)")
    print("=" * 78)
    print(f"  series : {ex} {sym}   n={rates.size} intervals   span={days:.0f}d   "
          f"cadence={gap_h:.2f}h")
    print(f"  gross APR (mean x intervals/yr) : {gross_apr * 100:+.2f}%   "
          f"[raw window sum = {rates.sum() * 100:+.2f}%, NOT used cross-venue]")
    print(f"  longest negative funding streak : {negstrk} intervals "
          f"({negstrk_days:.1f} days)")

    # Always-on NET APR on NOTIONAL: slippage x rebalance-drag
    print("\n  Always-on NET APR on NOTIONAL   (rows=slippage bp/leg, cols=rebalance bp/day)")
    print("            " + "".join(f"{d:>9.1f}bp/d" for d in DRAGS))
    for slip in SLIPS:
        cells = [annualize(harvest_always_on(rates, ex, days, slip_bps=slip,
                                             drag_bps_per_day=drag)["net"], days) * 100
                 for drag in DRAGS]
        print(f"    {slip:>2d}bp/leg " + "".join(f"{c:>11.2f}%" for c in cells))

    # Always-on NET APR on CAPITAL: slippage x capital-multiple (at base drag), + path DD
    print(f"\n  Always-on NET APR on CAPITAL   (rows=slippage bp/leg, cols=capital_multiple; "
          f"rebalance drag={base_drag}bp/day)")
    print("            " + "".join(f"{c:>9.1f}x  " for c in CAPS) + "    maxDD(fund+drag)")
    cap_tbl = {}
    for slip in SLIPS:
        res = harvest_always_on(rates, ex, days, slip_bps=slip, drag_bps_per_day=base_drag)
        notion = annualize(res["net"], days)
        cells = []
        for cap in CAPS:
            cap_tbl[(slip, cap)] = notion / cap * 100
            cells.append(notion / cap * 100)
        dd = max_drawdown(res["path"]) * 100
        print(f"    {slip:>2d}bp/leg " + "".join(f"{c:>10.2f}%" for c in cells)
              + f"      {dd:>8.2f}%")

    # Conditional strategy (base slip/drag)
    cond = harvest_conditional(rates, ex, days)
    cond_apr = annualize(cond["net"], days) * 100
    cond_dd = max_drawdown(cond["path"]) * 100
    print(f"\n  Conditional (hold only when trailing funding>0, base slip/drag): "
          f"net APR(notional)={cond_apr:+.2f}%  entries={cond['entries']}  "
          f"maxDD={cond_dd:+.2f}%")

    # VERDICT — funding harvest is REAL only if the unlevered-safe (2.0x) capital
    # return stays positive at slippage >= 3 bps/leg. We test that across the FULL
    # rebalance-drag sweep, not just the rosiest drag: a "pass" that survives only
    # at 0.5bp/day drag and dies at realistic drag is not a robust edge — it's
    # compensation for the short-perp tail risk.
    def cap2(slip, drag):
        notion = annualize(harvest_always_on(rates, ex, days, slip_bps=slip,
                                             drag_bps_per_day=drag)["net"], days)
        return notion / 2.0 * 100.0

    print("\n" + "-" * 78)
    print("  TEST: 2.0x (unlevered-safe) capital net APR at slip>=3bp/leg, "
          "across rebalance drag")
    print("          " + "".join(f"{d:>9.1f}bp/d" for d in DRAGS))
    grid = {}
    for slip in (3, 5):
        row = [cap2(slip, d) for d in DRAGS]
        for d, v in zip(DRAGS, row):
            grid[(slip, d)] = v
        print(f"   slip={slip}bp " + "".join(f"{v:>11.2f}%" for v in row))

    all_pos = all(v > 0 for v in grid.values())
    rosy_only = (not all_pos) and grid[(3, base_drag)] > 0 and grid[(5, base_drag)] > 0
    if all_pos:
        print("  VERDICT: PASS — robust capturable edge. Net positive on unlevered-safe")
        print("           capital at slip>=3bp across every rebalance-drag assumption.")
    elif rosy_only:
        print("  VERDICT: FAIL — positive ONLY at the most optimistic drag (0.5bp/day);")
        print("           any realistic rebalancing cost (>=1.0bp/day) turns it negative.")
        print("           A sub-1%/yr edge this fragile is compensation for short-perp")
        print("           tail risk, NOT a real edge.")
    else:
        print("  VERDICT: FAIL — net <=0 on unlevered-safe capital at slip>=3bp/leg.")
        print("           This is compensation for short-perp tail risk, NOT an edge.")
    print("-" * 78)


if __name__ == "__main__":
    main()
    kraken_verdict_report()
