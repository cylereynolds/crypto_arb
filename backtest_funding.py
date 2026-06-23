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


def roundtrip_cost_frac(exchange):
    """
    One full in-and-out of a delta-neutral hedge:
      entry  = buy spot  (taker+slip) + short perp (taker+slip)
      exit   = sell spot (taker+slip) + cover perp (taker+slip)
    => 2x spot taker + 2x perp taker + 4 leg-crosses of slippage.
    """
    f = fee(exchange)
    slip = SLIPPAGE_BPS_PER_LEG / 1e4
    return 2 * f["spot_taker"] + 2 * f["perp_taker"] + 4 * slip


def rebalance_drag_frac(days_held):
    """Delta-rebalancing taker drag accrued over the holding period."""
    return (REBALANCE_DRAG_BPS_PER_DAY / 1e4) * days_held


def borrow_cost_frac(days_held):
    """Margin/borrow interest. ~0 for unlevered same-venue cash+perp."""
    return BORROW_APR * (days_held / 365.0)


# ============================================================================
# PnL FUNCTIONS  — apply the cost model to a funding-rate series.
# `rates` is the ordered list of per-interval funding rates (fractions). We are
# SHORT the perp, so we RECEIVE +rate each interval when rate>0 and PAY when <0.
# The spot long cancels perp price PnL (delta-neutral), so PnL == funding flow.
# ============================================================================

def harvest_always_on(rates, exchange, days_held):
    """
    Hold the hedge for the whole window. Returns a dict of PnL components,
    all as fractions of notional, plus the cumulative net-PnL path for risk.
    """
    rates = np.asarray(rates, dtype=float)
    n = len(rates)
    gross = float(rates.sum())                       # funding collected (net of sign)
    rt = roundtrip_cost_frac(exchange)               # one entry + one exit
    rebal = rebalance_drag_frac(days_held)
    borrow = borrow_cost_frac(days_held)
    net = gross - rt - rebal - borrow

    # Cumulative net-PnL path: pay half the round trip at entry, accrue funding
    # minus per-interval rebal drag, pay the other half at exit. Used for DD.
    per_interval_rebal = rebal / n if n else 0.0
    path = np.empty(n + 1)
    path[0] = -rt / 2.0
    for i, r in enumerate(rates):
        path[i + 1] = path[i] + r - per_interval_rebal
    path[-1] -= rt / 2.0
    return {
        "gross": gross, "roundtrip": rt, "rebal": rebal, "borrow": borrow,
        "net": net, "path": path, "n": n,
    }


def harvest_conditional(rates, exchange, days_held, trailing=COND_TRAILING_INTERVALS):
    """
    Only hold while trailing-mean funding is positive; sit flat otherwise.
    Pays a fresh round trip each time we re-enter (this is what kills naive
    'just turn it off' strategies — flip costs eat the saved negative funding).
    """
    rates = np.asarray(rates, dtype=float)
    n = len(rates)
    rt = roundtrip_cost_frac(exchange)
    per_day_rebal = REBALANCE_DRAG_BPS_PER_DAY / 1e4
    gap_days = days_held / n if n else 0.0

    in_pos = False
    gross = 0.0
    cost = 0.0
    entries = 0
    path = [0.0]
    for i, r in enumerate(rates):
        window = rates[max(0, i - trailing):i]
        signal = window.mean() > 0 if len(window) else r > 0
        if signal and not in_pos:
            in_pos = True
            entries += 1
            cost += rt / 2.0
            path.append(path[-1] - rt / 2.0)
        elif not signal and in_pos:
            in_pos = False
            cost += rt / 2.0
            path.append(path[-1] - rt / 2.0)
        if in_pos:
            gross += r
            step_cost = per_day_rebal * gap_days
            cost += step_cost
            path.append(path[-1] + r - step_cost)
        else:
            path.append(path[-1])
    if in_pos:                       # close at end
        cost += rt / 2.0
        path.append(path[-1] - rt / 2.0)
    return {"gross": gross, "cost": cost, "net": gross - cost,
            "entries": entries, "path": np.asarray(path), "n": n}


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


if __name__ == "__main__":
    main()
