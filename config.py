"""
Shared config for the crypto-edge POC.

REALISTIC FRICTIONS — these are the numbers that decide whether an edge is real.
All fees are TAKER (we assume we cross the spread; maker rebates are not assumed
because in a real harvest you can't count on resting fills). Default/lowest VIP
tier — i.e. the WORST realistic case for a small account. Sources are each
exchange's public fee schedule (2025-2026). Document, don't hand-wave.
"""

# ----------------------------------------------------------------------------
# Exchanges confirmed reachable keyless from this box (see verify_exchanges.py)
# ----------------------------------------------------------------------------
SPOT_EXCHANGES = ["coinbase", "kraken", "binanceus", "okx", "bitstamp",
                  "gemini", "cryptocom"]

PERP_EXCHANGES = ["okx", "kucoinfutures", "krakenfutures", "bitget", "gate"]

# ----------------------------------------------------------------------------
# Taker fee schedule (fraction of notional, per side). Lowest/base VIP tier.
# spot_taker is the fee on the SPOT leg of a same-venue cash-and-carry hedge.
# perp_taker is the fee on the PERP leg.
# ----------------------------------------------------------------------------
FEES = {
    # exchange         spot_taker  perp_taker   note
    "okx":           {"spot_taker": 0.0010, "perp_taker": 0.0005},  # 0.10% / 0.05%
    "bitget":        {"spot_taker": 0.0010, "perp_taker": 0.0006},  # 0.10% / 0.06%
    "gate":          {"spot_taker": 0.0010, "perp_taker": 0.0005},  # 0.10%(w/disc) / 0.05%
    "kucoinfutures": {"spot_taker": 0.0010, "perp_taker": 0.0006},  # kucoin spot / kucoinfut
    "krakenfutures": {"spot_taker": 0.0025, "perp_taker": 0.0005},  # kraken spot taker is steep
    # generic fallback
    "_default":      {"spot_taker": 0.0010, "perp_taker": 0.0006},
}

# Perp symbols to harvest (liquid majors). USDT-margined linear perps.
PERP_SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
# Kraken Futures uses inverse/USD-quoted; handled with fallbacks at collect time.
PERP_SYMBOL_FALLBACKS = {
    "krakenfutures": {
        "BTC/USDT:USDT": ["BTC/USD:BTC", "BTC/USD:USD"],
        "ETH/USDT:USDT": ["ETH/USD:ETH", "ETH/USD:USD"],
    },
}

# ----------------------------------------------------------------------------
# Backtest cost assumptions (delta-neutral funding harvest)
# ----------------------------------------------------------------------------
# Capital multiple: unlevered delta-neutral on $N notional ties up ~$N in spot
# plus perp initial margin + buffer. 1.2 => $1.20 capital per $1 of funded
# notional. Return-on-capital = return-on-notional / capital_multiple.
CAPITAL_MULTIPLE = 1.2

# Slippage: bps of adverse fill PER LEG-CROSS beyond the fee, for a modest
# clip on a liquid major. A delta-neutral entry crosses 2 legs (spot+perp) and
# exit crosses 2 more => 4 leg-crosses per full round trip. 1.0 bp/leg is
# realistic for BTC/ETH top-of-book at retail size; scale up for size/illiquid.
SLIPPAGE_BPS_PER_LEG = 1.0

# Rebalancing drag: keeping the hedge delta-neutral as price moves costs small
# taker trades. Modeled as a constant bps/day haircut on notional. 0.5 bps/day
# ~= 1.8%/yr drag — deliberately conservative. Reported with sensitivity.
REBALANCE_DRAG_BPS_PER_DAY = 0.5

# Borrow/margin interest. Unlevered same-venue cash+perp => ~0. Leverage trades
# borrow cost for liquidation risk instead; flagged in the report, not modeled.
BORROW_APR = 0.0

# Conditional strategy: only hold the hedge when trailing funding is positive,
# to skip negative-funding stretches. Trailing window in number of funding
# intervals used to decide entry/exit.
COND_TRAILING_INTERVALS = 3

DB_PATH = "crypto_poc.db"

# ----------------------------------------------------------------------------
# Proxy — residential HTTP proxy used ONLY where useful: venues that are geo /
# Cloudflare blocked from a US IP. Loaded at runtime from PROXY_URL env or a
# gitignored proxies.txt (NEVER committed — public repo). Exit observed: SG.
# ----------------------------------------------------------------------------
import os  # noqa: E402

# Venues unreachable direct from US (451/403) but reachable via the proxy.
# Everything else stays DIRECT (faster, no metered proxy bandwidth).
PROXY_VENUES = {"binance", "binanceusdm", "bybit"}


def get_proxy():
    """HTTP proxy URL (or None). PROXY_URL env wins; else first line of proxies.txt."""
    url = os.environ.get("PROXY_URL", "").strip()
    if url:
        return url
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies.txt")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except FileNotFoundError:
        pass
    return None


def make_exchange(name, default_type="swap", use_proxy=None, timeout=30000):
    """Build a ccxt exchange, routing through the proxy when the venue needs it.
    Only `httpsProxy` is set (exchange APIs are HTTPS; setting >1 proxy errors)."""
    import ccxt
    ex = getattr(ccxt, name)({"enableRateLimit": True, "timeout": timeout,
                              "options": {"defaultType": default_type}})
    if use_proxy is None:
        use_proxy = name in PROXY_VENUES
    if use_proxy:
        p = get_proxy()
        if p:
            ex.httpsProxy = p
    return ex
