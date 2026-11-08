"""SQLite persistence layer for the shares tracker.

Owns all database interaction: schema creation, snapshot storage, and
cached historical price / FX series. No dependencies on other src modules.
"""
import sqlite3
from pathlib import Path

import pandas as pd


_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    date          TEXT,
    code          TEXT,
    exchange      TEXT,
    platform      TEXT,
    units         REAL,
    price_local   REAL,
    price_aud     REAL,
    value_aud     REAL,
    PRIMARY KEY (date, code, exchange, platform)
);

CREATE TABLE IF NOT EXISTS snapshot_totals (
    date              TEXT PRIMARY KEY,
    total_value_aud   REAL,
    unrealised_pl_aud REAL
);

CREATE TABLE IF NOT EXISTS fx_history (
    date        TEXT,
    currency    TEXT,
    rate_to_aud REAL,
    PRIMARY KEY (date, currency)
);

CREATE TABLE IF NOT EXISTS price_history (
    date        TEXT,
    ticker      TEXT,
    close_local REAL,
    PRIMARY KEY (date, ticker)
);

CREATE TABLE IF NOT EXISTS dividend_history (
    date          TEXT,           -- ex-date YYYY-MM-DD
    ticker        TEXT,
    amount_local  REAL,           -- dividend per share, local currency
    PRIMARY KEY (date, ticker)
);

CREATE TABLE IF NOT EXISTS fetch_ranges (
    kind        TEXT,           -- 'price' or 'fx'
    key         TEXT,           -- ticker (price) or currency (fx)
    start       TEXT,           -- inclusive YYYY-MM-DD
    end         TEXT,           -- inclusive YYYY-MM-DD
    PRIMARY KEY (kind, key, start)
);

CREATE TABLE IF NOT EXISTS ticker_info (
    ticker      TEXT PRIMARY KEY,
    sector      TEXT
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    id          TEXT PRIMARY KEY
);
"""


def _run_migrations(conn: sqlite3.Connection) -> None:
    """One-off, idempotent data migrations keyed by id in schema_migrations."""
    def _applied(mid: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = ?", (mid,)
        ).fetchone() is not None

    def _mark(mid: str) -> None:
        conn.execute("INSERT INTO schema_migrations (id) VALUES (?)", (mid,))

    # H4: previously cached prices used auto_adjust=True (dividend/split
    # adjusted). Valuation now uses raw Close, so the adjusted cache is wrong —
    # clear it (and its fetch_ranges) so it refetches unadjusted on next run.
    mid = "2026-07_clear_adjusted_price_history"
    if not _applied(mid):
        with conn:
            conn.execute("DELETE FROM price_history")
            conn.execute("DELETE FROM fetch_ranges WHERE kind = 'price'")
            _mark(mid)


def init_db(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the database, ensure schema, return connection.

    The caller holds the connection for the process lifetime.
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _run_migrations(conn)
    conn.commit()
    return conn


# --- snapshot_totals ---------------------------------------------------------

def is_snapshot_totals_empty(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT COUNT(*) AS n FROM snapshot_totals").fetchone()
    return row["n"] == 0


def get_latest_snapshot_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(date) AS d FROM snapshot_totals").fetchone()
    return row["d"] if row and row["d"] is not None else None


def upsert_snapshot_total(
    conn: sqlite3.Connection,
    date: str,
    total_value_aud: float,
    unrealised_pl_aud: float,
) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO snapshot_totals "
            "(date, total_value_aud, unrealised_pl_aud) VALUES (?, ?, ?)",
            (date, total_value_aud, unrealised_pl_aud),
        )


def load_snapshot_totals(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT date, total_value_aud, unrealised_pl_aud "
        "FROM snapshot_totals ORDER BY date",
        conn,
    )
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


# --- snapshots (per-position rows) -------------------------------------------

def upsert_snapshot_rows(conn: sqlite3.Connection, rows: list[dict]) -> None:
    if not rows:
        return
    params = [
        (
            r["date"], r["code"], r["exchange"], r["platform"],
            r["units"], r["price_local"], r["price_aud"], r["value_aud"],
        )
        for r in rows
    ]
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO snapshots "
            "(date, code, exchange, platform, units, price_local, price_aud, value_aud) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            params,
        )


# --- price_history -----------------------------------------------------------

def upsert_prices(conn: sqlite3.Connection, ticker: str, price_dict: dict[str, float]) -> None:
    if not price_dict:
        return
    params = [(d, ticker, p) for d, p in price_dict.items()]
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO price_history (date, ticker, close_local) "
            "VALUES (?, ?, ?)",
            params,
        )


def get_cached_price_dates(conn: sqlite3.Connection, ticker: str) -> set[str]:
    rows = conn.execute(
        "SELECT date FROM price_history WHERE ticker = ?", (ticker,)
    ).fetchall()
    return {r["date"] for r in rows}


def get_price_at_or_before(
    conn: sqlite3.Connection, ticker: str, date_str: str
) -> float | None:
    """Nearest prior trading-day close (forward-fill for weekends/holidays)."""
    row = conn.execute(
        "SELECT close_local FROM price_history "
        "WHERE ticker = ? AND date <= ? ORDER BY date DESC LIMIT 1",
        (ticker, date_str),
    ).fetchone()
    return row["close_local"] if row else None


def get_prices_in_range(
    conn: sqlite3.Connection, ticker: str, start: str, end: str
) -> dict[str, float]:
    """Batch read all cached closes for a ticker in [start, end] in one query."""
    rows = conn.execute(
        "SELECT date, close_local FROM price_history "
        "WHERE ticker = ? AND date BETWEEN ? AND ? ORDER BY date",
        (ticker, start, end),
    ).fetchall()
    return {r["date"]: r["close_local"] for r in rows}


# --- dividend_history --------------------------------------------------------

def upsert_dividends(
    conn: sqlite3.Connection, ticker: str, div_dict: dict[str, float]
) -> None:
    """Cache per-share dividends {ex_date_str: amount_local} for a ticker."""
    if not div_dict:
        return
    params = [(d, ticker, a) for d, a in div_dict.items()]
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO dividend_history (date, ticker, amount_local) "
            "VALUES (?, ?, ?)",
            params,
        )


def get_dividends_in_range(
    conn: sqlite3.Connection, ticker: str, start: str, end: str
) -> dict[str, float]:
    """All cached dividends for a ticker with ex-date in [start, end]."""
    rows = conn.execute(
        "SELECT date, amount_local FROM dividend_history "
        "WHERE ticker = ? AND date BETWEEN ? AND ? ORDER BY date",
        (ticker, start, end),
    ).fetchall()
    return {r["date"]: r["amount_local"] for r in rows}


# --- fetch_ranges (honest cache coverage, M3) --------------------------------

def get_fetch_ranges(
    conn: sqlite3.Connection, kind: str, key: str
) -> list[tuple[str, str]]:
    """Return recorded [start, end] ranges already fetched for a ticker/currency."""
    rows = conn.execute(
        "SELECT start, end FROM fetch_ranges WHERE kind = ? AND key = ? ORDER BY start",
        (kind, key),
    ).fetchall()
    return [(r["start"], r["end"]) for r in rows]


def add_fetch_range(
    conn: sqlite3.Connection, kind: str, key: str, start: str, end: str
) -> None:
    """Record a fetched range, merging any overlapping/adjacent existing ranges."""
    from datetime import date as _date, timedelta as _td

    existing = get_fetch_ranges(conn, kind, key)
    merged: list[list[str]] = []
    for s, e in sorted(existing + [(start, end)]):
        if merged and _date.fromisoformat(s) <= _date.fromisoformat(merged[-1][1]) + _td(days=1):
            # Overlapping or directly adjacent — extend the previous range.
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])

    with conn:
        conn.execute("DELETE FROM fetch_ranges WHERE kind = ? AND key = ?", (kind, key))
        conn.executemany(
            "INSERT INTO fetch_ranges (kind, key, start, end) VALUES (?, ?, ?, ?)",
            [(kind, key, s, e) for s, e in merged],
        )


def range_missing_subranges(
    conn: sqlite3.Connection, kind: str, key: str, start: str, end: str
) -> list[tuple[str, str]]:
    """Return the sub-ranges of [start, end] NOT yet covered by fetch_ranges."""
    from datetime import date as _date, timedelta as _td

    covered = get_fetch_ranges(conn, kind, key)
    gaps: list[tuple[str, str]] = []
    cursor = _date.fromisoformat(start)
    end_d = _date.fromisoformat(end)

    for s, e in covered:
        s_d, e_d = _date.fromisoformat(s), _date.fromisoformat(e)
        if e_d < cursor or s_d > end_d:
            continue
        if s_d > cursor:
            gaps.append((cursor.isoformat(), (s_d - _td(days=1)).isoformat()))
        cursor = max(cursor, e_d + _td(days=1))
        if cursor > end_d:
            break

    if cursor <= end_d:
        gaps.append((cursor.isoformat(), end_d.isoformat()))
    return gaps


# --- ticker_info (sector cache) ----------------------------------------------

def get_ticker_sector(conn: sqlite3.Connection, ticker: str) -> str | None:
    row = conn.execute(
        "SELECT sector FROM ticker_info WHERE ticker = ?", (ticker,)
    ).fetchone()
    return row["sector"] if row else None


def upsert_ticker_sector(conn: sqlite3.Connection, ticker: str, sector: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO ticker_info (ticker, sector) VALUES (?, ?)",
            (ticker, sector),
        )


# --- fx_history --------------------------------------------------------------

def upsert_fx(conn: sqlite3.Connection, currency: str, rate_dict: dict[str, float]) -> None:
    if not rate_dict:
        return
    params = [(d, currency, r) for d, r in rate_dict.items()]
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO fx_history (date, currency, rate_to_aud) "
            "VALUES (?, ?, ?)",
            params,
        )


def get_cached_fx_dates(conn: sqlite3.Connection, currency: str) -> set[str]:
    rows = conn.execute(
        "SELECT date FROM fx_history WHERE currency = ?", (currency,)
    ).fetchall()
    return {r["date"] for r in rows}


def get_fx_at_or_before(
    conn: sqlite3.Connection, currency: str, date_str: str
) -> float | None:
    """Nearest prior trading-day FX rate (forward-fill for weekends/holidays)."""
    if currency == "AUD":
        return 1.0
    row = conn.execute(
        "SELECT rate_to_aud FROM fx_history "
        "WHERE currency = ? AND date <= ? ORDER BY date DESC LIMIT 1",
        (currency, date_str),
    ).fetchone()
    return row["rate_to_aud"] if row else None
