# Handoff — Cost Model & PnL Functions (Phase 1B)

Self-contained extract for review. The delta-neutral funding harvest = **short perp + long spot**: we receive funding when the rate is positive, pay when negative; spot cancels the perp's price delta. Everything below is a fraction of **notional** unless stated; APRs are reported on **capital** (notional × capital_mult).

Two source files: cost constants live in `config.py`; the cost-model and PnL functions live in `backtest_funding.py` (the report/CLI portion of that file is omitted here).

```python
# ===== config.py (frictions the cost model depends on) =====

# Taker fee schedule (fraction of notional, PER SIDE). Lowest/base VIP tier =
# worst realistic case for a small account. spot_taker = spot leg of a
# same-venue cash-and-carry hedge; perp_taker = perp leg.
FEES = {
    # exchange         spot_taker  perp_taker   note
    "okx":           {"spot_taker": 0.0010, "perp_taker": 0.0005},  # 0.10% / 0.05%
    "bitget":        {"spot_taker": 0.0010, "perp_taker": 0.0006},  # 0.10% / 0.06%
    "gate":          {"spot_taker": 0.0010, "perp_taker": 0.0005},  # 0.10%(w/disc) / 0.05%
    "kucoinfutures": {"spot_taker": 0.0010, "perp_taker": 0.0006},  # kucoin spot / kucoinfut
    "krakenfutures": {"spot_taker": 0.0025, "perp_taker": 0.0005},  # kraken spot taker is steep
    "_default":      {"spot_taker": 0.0010, "perp_taker": 0.0006},
}

# Slippage: bps of adverse fill PER LEG-CROSS beyond the fee, modest clip on a
# liquid major. Entry crosses 2 legs (spot+perp), exit 2 more => 4 per round trip.
SLIPPAGE_BPS_PER_LEG = 1.0

# Unlevered delta-neutral on $N notional ties up ~$N spot + perp margin + buffer.
# 1.2 => $1.20 capital per $1 funded notional. ROC = return_on_notional / this.
CAPITAL_MULTIPLE = 1.2

# Delta-rebalancing taker drag, modeled as constant bps/day on notional.
# 0.5 bp/day ~= 1.8%/yr. Conservative; reported with sensitivity.
REBALANCE_DRAG_BPS_PER_DAY = 0.5

# Margin/borrow interest. ~0 for unlevered same-venue cash+perp. Leverage swaps
# borrow cost for liquidation risk instead (flagged in report, not modeled).
BORROW_APR = 0.0

# Conditional strategy: hold only while trailing-mean funding > 0. Window length
# in funding intervals used to decide entry/exit.
COND_TRAILING_INTERVALS = 3

# Funding cadence OBSERVED in collected data -> drives intervals_per_year():
#   krakenfutures               : 1h  -> 8760 intervals/yr
#   okx / bitget / kucoinfutures : 8h  -> 1095 intervals/yr
# (computed at runtime from the median timestamp gap, not hard-coded.)
```

```python
# ===== backtest_funding.py (cost model + PnL functions) =====
import numpy as np

from config import (FEES, SLIPPAGE_BPS_PER_LEG, CAPITAL_MULTIPLE,
                    REBALANCE_DRAG_BPS_PER_DAY, BORROW_APR,
                    COND_TRAILING_INTERVALS)

MS_DAY = 86_400_000

# -------------------------------------------------------------------------
# COST MODEL — every friction that turns gross funding into capturable PnL.
# -------------------------------------------------------------------------

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


# -------------------------------------------------------------------------
# PnL FUNCTIONS — apply the cost model to a funding-rate series.
# `rates` is the ordered list of per-interval funding rates (fractions). We are
# SHORT the perp: RECEIVE +rate when rate>0, PAY when <0. Spot long cancels the
# perp price delta, so PnL == funding flow minus frictions.
# -------------------------------------------------------------------------

def harvest_always_on(rates, exchange, days_held):
    """
    Hold the hedge for the whole window. Returns PnL components as fractions of
    notional, plus the cumulative net-PnL path for drawdown.
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
    Only hold while trailing-mean funding is positive; sit flat otherwise. Pays a
    fresh round trip each re-entry (this is what kills naive 'just turn it off':
    flip costs eat the saved negative funding, esp. on high-spot-fee venues).
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
```

## How the pieces compose (for a reviewer)

- **Gross** = `sum(rates)` over the window. **Gross APR** (window-independent) = `mean(rates) × intervals_per_year(gap_h)`.
- **Net (notional)** = gross − `roundtrip_cost_frac` − `rebalance_drag_frac(days)` − `borrow_cost_frac(days)`.
- **Net APR (capital)** = `annualize(net, days) / CAPITAL_MULTIPLE`.
- **Risk** = `max_drawdown(path)` + `longest_negative_streak(rates)` capture funding-flip risk.

**Known caveat to scrutinize:** `harvest_always_on` charges one round trip then annualizes over the *actual* window. On 33–98d venues the fixed round-trip dominates the annualization and understates a true long-hold edge — only Kraken's 365d/263d windows judge buy-and-hold fairly. A reviewer may want a steady-state variant that amortizes the round trip over an assumed 1y hold.
