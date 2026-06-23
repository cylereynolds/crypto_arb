"""Tiny SQLite persistence layer for the POC data collectors."""
import sqlite3
from contextlib import contextmanager

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS funding_history (
    exchange    TEXT NOT NULL,
    symbol      TEXT NOT NULL,      -- normalized symbol we asked for
    market      TEXT,               -- actual market symbol used
    ts          INTEGER NOT NULL,   -- funding timestamp (ms)
    funding_rate REAL NOT NULL,     -- per-interval rate (fraction)
    PRIMARY KEY (exchange, symbol, ts)
);

CREATE TABLE IF NOT EXISTS spot_book (
    exchange  TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    ts        INTEGER NOT NULL,     -- local capture time (ms)
    bid       REAL NOT NULL,
    ask       REAL NOT NULL,
    bid_qty   REAL,
    ask_qty   REAL,
    PRIMARY KEY (exchange, symbol, ts)
);

CREATE INDEX IF NOT EXISTS idx_spot_ts ON spot_book (symbol, ts);
"""


@contextmanager
def db(path=DB_PATH):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_funding(conn, rows):
    conn.executemany(
        "INSERT OR REPLACE INTO funding_history "
        "(exchange, symbol, market, ts, funding_rate) VALUES (?,?,?,?,?)",
        rows,
    )


def insert_spot(conn, rows):
    conn.executemany(
        "INSERT OR REPLACE INTO spot_book "
        "(exchange, symbol, ts, bid, ask, bid_qty, ask_qty) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
