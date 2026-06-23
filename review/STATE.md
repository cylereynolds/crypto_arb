# Data State — crypto_arb POC

_Generated 2026-06-22. Store: `crypto_poc.db` (SQLite), table `funding_history`. Public/keyless ccxt data only — no keys, no funds._

## Funding-rate history collected (Phase 1B)

Total: **16,038 rows** across 4 exchanges × 2 symbols. (Requested symbols are USDT-linear perps; Kraken only lists USD-quoted inverse perps, so its `market` differs.)

| exchange | symbol (requested) | market used | rows | date range (UTC) | span | cadence |
|---|---|---|---:|---|---:|---|
| krakenfutures | BTC/USDT:USDT | BTC/USD:BTC | 8,750 | 2025-06-22 → 2026-06-22 | 365.0d | 1h |
| krakenfutures | ETH/USDT:USDT | ETH/USD:ETH | 6,300 | 2025-06-22 → 2026-03-12 | 262.8d | 1h |
| okx | BTC/USDT:USDT | BTC/USDT:USDT | 294 | 2026-03-16 → 2026-06-22 | 97.7d | 8h |
| okx | ETH/USDT:USDT | ETH/USDT:USDT | 294 | 2026-03-16 → 2026-06-22 | 97.7d | 8h |
| bitget | BTC/USDT:USDT | BTC/USDT:USDT | 100 | 2026-05-20 → 2026-06-22 | 33.0d | 8h |
| bitget | ETH/USDT:USDT | ETH/USDT:USDT | 100 | 2026-05-20 → 2026-06-22 | 33.0d | 8h |
| kucoinfutures | BTC/USDT:USDT | BTC/USDT:USDT | 100 | 2026-05-20 → 2026-06-22 | 33.0d | 8h |
| kucoinfutures | ETH/USDT:USDT | ETH/USDT:USDT | 100 | 2026-05-20 → 2026-06-22 | 33.0d | 8h |

## Spot top-of-book (Phase 1A arbitrage)

**None collected yet.** `spot_book` table exists (schema in `store.py`) but the live streaming collector for Phase 1A has not been run. 7 spot venues verified keyless-reachable (coinbase, kraken, binanceus, okx, bitstamp, gemini, cryptocom).

## Depth caps — what's limited and why

- **krakenfutures** — no cap hit; serves a full **365d** at **hourly** cadence (the only deep, long-window source). ETH history starts later (perp listing) → 263d.
- **okx** — public history endpoint returns ~**98d** (8h cadence); did not page further back.
- **bitget**, **kucoinfutures** — public endpoint **hard-caps at 100 rows ≈ 33d** (8h cadence). bitget can be extended to ~90d via ccxt `paginate=True` (verified in a probe, not yet stored); kucoinfutures stays at 33d even with pagination.
- **gate** — **0 rows collected**. Endpoint rejects `from` windows > 180d (`INVALID_PARAM_VALUE`) and returned empty under the default collector; needs a ≤170d window + paginate to capture.
- **binance / bybit** — excluded entirely: geo/Cloudflare-blocked from this US IP (451 / 403).

**Backtest caveat:** only Kraken (365d/263d) gives a window long enough to fairly judge a buy-and-hold harvest. On the 33–98d venues, the one-time entry/exit round-trip cost dominates when annualized over the short window, so their net-APR figures understate a true long-hold edge.
