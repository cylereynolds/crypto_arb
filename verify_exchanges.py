#!/usr/bin/env python3
"""
Build Step 1 verification: confirm ccxt can pull PUBLIC (keyless) market data.

Tests two capabilities per exchange, since the POC needs both:
  (A) SPOT top-of-book  -> for Phase 1A cross-exchange arbitrage
  (B) FUNDING RATES     -> for Phase 1B funding-rate harvesting

No API keys. No funds. Public endpoints only.

Run:  .venv/bin/python verify_exchanges.py
"""
import time
import ccxt

# Candidate spot exchanges (liquid, commonly reachable). We test reachability
# from THIS box and report — geo-blocks (e.g. Binance.com from US) are expected.
SPOT_CANDIDATES = [
    "coinbase", "kraken", "binanceus", "binance", "okx",
    "bybit", "kucoin", "bitstamp", "gemini", "cryptocom",
]

# Candidate perp/swap exchanges for funding rates.
PERP_CANDIDATES = [
    "binance", "okx", "bybit", "kucoinfutures", "krakenfutures",
    "bitget", "gate", "hyperliquid",
]

# Symbol fallbacks: USDT pairs dominate, but some US venues use USD.
SPOT_SYMBOLS = ["BTC/USDT", "BTC/USD", "ETH/USDT", "ETH/USD"]
PERP_SYMBOLS = ["BTC/USDT:USDT", "BTC/USD:BTC", "BTC/USDC:USDC", "ETH/USDT:USDT"]


def first_available_symbol(ex, candidates):
    syms = set(ex.symbols or [])
    for s in candidates:
        if s in syms:
            return s
    return None


def test_spot(name):
    out = {"exchange": name, "ok": False, "detail": ""}
    try:
        ex = getattr(ccxt, name)({"enableRateLimit": True, "timeout": 15000})
        ex.load_markets()
        sym = first_available_symbol(ex, SPOT_SYMBOLS)
        if not sym:
            out["detail"] = "no BTC/ETH USD(T) spot market listed"
            return out
        t0 = time.time()
        ob = ex.fetch_order_book(sym, limit=5)
        ms = (time.time() - t0) * 1000
        bid = ob["bids"][0][0] if ob["bids"] else None
        ask = ob["asks"][0][0] if ob["asks"] else None
        if bid and ask:
            spread_bps = (ask - bid) / ((ask + bid) / 2) * 1e4
            out.update(ok=True, detail=f"{sym} bid={bid:.2f} ask={ask:.2f} "
                       f"spread={spread_bps:.1f}bps  ({ms:.0f}ms)")
        else:
            out["detail"] = f"{sym} empty book"
    except Exception as e:
        out["detail"] = f"{type(e).__name__}: {str(e)[:90]}"
    return out


def test_funding(name):
    out = {"exchange": name, "ok": False, "detail": ""}
    try:
        ex = getattr(ccxt, name)({"enableRateLimit": True, "timeout": 15000,
                                  "options": {"defaultType": "swap"}})
        ex.load_markets()
        sym = first_available_symbol(ex, PERP_SYMBOLS)
        if not sym:
            out["detail"] = "no BTC/ETH perp market listed"
            return out
        if not ex.has.get("fetchFundingRate"):
            out["detail"] = "fetchFundingRate not supported"
            return out
        t0 = time.time()
        fr = ex.fetch_funding_rate(sym)
        ms = (time.time() - t0) * 1000
        rate = fr.get("fundingRate")
        hist = ex.has.get("fetchFundingRateHistory")
        if rate is not None:
            # annualized assuming 3x/day (8h) funding as a rough orientation
            ann = rate * 3 * 365 * 100
            out.update(ok=True, detail=f"{sym} funding={rate:+.6f} "
                       f"(~{ann:+.1f}%/yr) history={'Y' if hist else 'N'} ({ms:.0f}ms)")
        else:
            out["detail"] = f"{sym} funding rate None"
    except Exception as e:
        out["detail"] = f"{type(e).__name__}: {str(e)[:90]}"
    return out


def banner(t):
    print("\n" + "=" * 78 + f"\n  {t}\n" + "=" * 78)


def main():
    print(f"ccxt version: {ccxt.__version__}")

    banner("(A) SPOT top-of-book  [Phase 1A arbitrage]")
    spot_ok = []
    for name in SPOT_CANDIDATES:
        r = test_spot(name)
        flag = "OK " if r["ok"] else "-- "
        print(f"  [{flag}] {name:14s} {r['detail']}")
        if r["ok"]:
            spot_ok.append(name)

    banner("(B) FUNDING rates  [Phase 1B funding harvest]")
    perp_ok = []
    for name in PERP_CANDIDATES:
        r = test_funding(name)
        flag = "OK " if r["ok"] else "-- "
        print(f"  [{flag}] {name:14s} {r['detail']}")
        if r["ok"]:
            perp_ok.append(name)

    banner("VERDICT")
    print(f"  Spot exchanges reachable (arb):     {len(spot_ok)}  -> {spot_ok}")
    print(f"  Perp exchanges reachable (funding): {len(perp_ok)}  -> {perp_ok}")
    gate = len(spot_ok) >= 3 or len(perp_ok) >= 3
    print(f"\n  Build-step-1 gate (>=3 in either category): "
          f"{'PASS' if gate else 'FAIL'}")


if __name__ == "__main__":
    main()
