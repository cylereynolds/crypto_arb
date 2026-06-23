# crypto_arb — Funding-Rate / Cross-Exchange Edge POC

A **proof-of-concept** that measures whether a real, capturable edge exists in
(a) perpetual-futures **funding-rate harvesting** and (b) **cross-exchange price
arbitrage** — using **public market data only**.

> **Phase 1 is data-only.** No API keys with trade/withdrawal scope, no funds on
> any exchange, no live orders. All data comes from ccxt's keyless public
> endpoints. **Not financial advice.**

The discipline here is to measure edges **net of realistic frictions** (fees,
slippage, rebalancing, basis/funding-flip risk) rather than eyeballing a gross
spread — and to report a plain verdict, including "no edge after frictions."

## Layout

| file | purpose |
|---|---|
| `verify_exchanges.py` | Build-step-1: confirm keyless public data from spot + perp venues |
| `config.py` | Exchange lists + the realistic friction model (fees, slippage, capital, rebalancing) |
| `store.py` | SQLite persistence (`crypto_poc.db`) |
| `collect_funding.py` | **Phase 1B** collector: historical funding rates via ccxt → SQLite |
| `backtest_funding.py` | **Phase 1B** backtest: delta-neutral funding harvest, net of frictions, with verdict |
| `review/STATE.md` | Current data-state summary (rows, ranges, depth caps) |
| `review/handoff.md` | Cost model + PnL functions extracted for review |

Phase 1A (cross-exchange arbitrage: live top-of-book streaming + net-edge
distribution) is scaffolded (`spot_book` table) but not yet collected.

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install ccxt pandas numpy aiohttp matplotlib
.venv/bin/python verify_exchanges.py      # confirm reachable venues
.venv/bin/python collect_funding.py        # pull funding history -> crypto_poc.db
.venv/bin/python backtest_funding.py       # net-of-cost backtest + verdict
```

## Status

Funding history collected from 4 reachable perp venues (Kraken Futures gives a
full 365d/hourly window; OKX ~98d; bitget/kucoin ~33d). Early read: the harvest
is net-positive only where funding runs persistently rich (e.g. Kraken ETH), and
net-negative elsewhere once frictions are applied. See `review/STATE.md`.
