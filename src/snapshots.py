"""Historical net-worth snapshots backed by SQLite.

Backfills snapshots at a 3-month cadence from the earliest trade until a year
ago, then every Friday (the close of each trading week) and today. Going forward, maybe_take_snapshot writes a
fresh snapshot at most weekly using current prices.
"""
import calendar
from datetime import date, timedelta

import pandas as pd

from src import db
from src import cgt as compute_cgt
from src.compute import compute_holdings
from src.market_data import (
    EXCHANGE_MAP,
    get_historical_fx,
    get_historical_prices,
    to_yf_ticker,
)

_EPS = 1e-9
_WEEKLY_WINDOW = timedelta(weeks=52)
_WEEKLY_ANCHOR = 4  # date.weekday() of Friday
_SNAPSHOT_MAX_AGE_DAYS = 7


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    last_day = calendar.monthrange(y, m)[1]
    return d.replace(year=y, month=m, day=min(d.day, last_day))


def _generate_snapshot_dates(earliest: date, today: date) -> list[date]:
    cutoff = today - _WEEKLY_WINDOW
    dates: list[date] = []

    d = earliest
    while d <= cutoff:
        dates.append(d)
        d = _add_months(d, 3)

    # Pinned to Fridays so a run on any day lands on the same dates (counting
    # back from today gave every weekday its own set, and they piled up).
    d = cutoff + timedelta(days=(_WEEKLY_ANCHOR - cutoff.weekday()) % 7)
    while d <= today:
        dates.append(d)
        d += timedelta(weeks=1)
    dates.append(today)

    # Nothing before the first trade (a portfolio younger than the weekly
    # window), so the FX history wanted starts at the first trade too.
    return sorted(d for d in set(dates) if d >= earliest)


def backfill_snapshots(
    conn, trades_df: pd.DataFrame, corporate_actions: pd.DataFrame | None = None
) -> None:
    earliest = trades_df["date"].min().date()
    today = date.today()
    snapshot_dates = _generate_snapshot_dates(earliest, today)
    print(f"  {len(snapshot_dates)} snapshot dates from {earliest} to {today}.")

    # --- Pre-fetch price history: one call per unique ticker over full range ---
    pairs = trades_df[["code", "exchange"]].drop_duplicates()
    tickers = {to_yf_ticker(r["code"], r["exchange"]) for _, r in pairs.iterrows()}
    print(f"  Pre-fetching historical prices for {len(tickers)} tickers...")
    for ticker in sorted(tickers):
        get_historical_prices(conn, ticker, earliest.isoformat(), today.isoformat())

    # --- Pre-fetch FX history: one call per foreign currency ---
    currencies = {EXCHANGE_MAP[ex]["currency"] for ex in trades_df["exchange"].unique()}
    foreign = sorted(c for c in currencies if c != "AUD")
    snap_strs = [d.isoformat() for d in snapshot_dates]
    for currency in foreign:
        print(f"  Pre-fetching historical FX for {currency}...")
        get_historical_fx(conn, currency, snap_strs)

    def _fx_provider(currency: str, date_str: str) -> float:
        rate = db.get_fx_at_or_before(conn, currency, date_str)
        return float("nan") if rate is None else rate

    # --- Build snapshots ---
    processed = 0
    for snap_date in snapshot_dates:
        snap_str = snap_date.isoformat()
        if conn.execute(
            "SELECT 1 FROM snapshot_totals WHERE date = ?", (snap_str,)
        ).fetchone():
            continue

        prior = trades_df[trades_df["date"].dt.date <= snap_date]
        if prior.empty:
            continue
        holdings = compute_holdings(prior, corporate_actions)
        if holdings.empty:
            continue

        # M4: cost base per position in AUD using FX at each buy date.
        cost_base_map = compute_cgt.open_cost_base_aud_as_of(
            prior, snap_date, _fx_provider, corporate_actions
        )

        rows = []
        total_value = 0.0
        total_cost = 0.0
        incomplete = False
        for _, pos in holdings.iterrows():
            price_local = db.get_price_at_or_before(conn, pos["yf_ticker"], snap_str)
            currency = pos["currency"]
            fx = db.get_fx_at_or_before(conn, currency, snap_str)

            # H5: a snapshot with ANY unknown price/FX is silently wrong when
            # summed — skip the whole date rather than under-report net worth.
            if price_local is None or fx is None or pd.isna(price_local) or pd.isna(fx):
                print(
                    f"  WARNING: incomplete data for {pos['code']} ({pos['exchange']}) "
                    f"at {snap_str} — skipping this snapshot entirely."
                )
                incomplete = True
                break

            price_aud = price_local * fx
            value_aud = pos["units"] * price_aud
            cost_aud = cost_base_map.get(
                (pos["code"], pos["exchange"], pos["platform"]), 0.0
            )

            total_value += value_aud
            total_cost += cost_aud
            rows.append({
                "date": snap_str,
                "code": pos["code"],
                "exchange": pos["exchange"],
                "platform": pos["platform"],
                "units": pos["units"],
                "price_local": price_local,
                "price_aud": price_aud,
                "value_aud": value_aud,
            })

        if incomplete or not rows:
            continue

        db.upsert_snapshot_rows(conn, rows)
        db.upsert_snapshot_total(conn, snap_str, total_value, total_value - total_cost)
        processed += 1
        print(f"  Snapshot {snap_str}  |  Value: ${total_value:,.0f}")

    print(f"  Backfill complete. {processed} new snapshots written.")


def maybe_take_snapshot(conn, enriched_df: pd.DataFrame, totals: dict) -> None:
    today_str = date.today().isoformat()
    latest = db.get_latest_snapshot_date(conn)

    if latest is not None:
        age = (date.fromisoformat(today_str) - date.fromisoformat(latest)).days
        if age < _SNAPSHOT_MAX_AGE_DAYS:
            print(f"  Snapshot up to date (last: {latest}), skipping.")
            return

    # H5: never persist a snapshot with any unknown price/FX — it would silently
    # under-sum today's net worth. Better a missing week than a wrong one.
    bad = enriched_df[
        enriched_df["current_price_aud"].isna() | enriched_df["market_value_aud"].isna()
    ]
    if not bad.empty:
        codes = ", ".join(sorted(bad["code"].unique()))
        print(
            f"  WARNING: incomplete market data for {codes} — skipping today's snapshot."
        )
        return

    rows = [{
        "date": today_str,
        "code": row["code"],
        "exchange": row["exchange"],
        "platform": row["platform"],
        "units": row["units"],
        "price_local": row["current_price_local"],
        "price_aud": row["current_price_aud"],
        "value_aud": row["market_value_aud"],
    } for _, row in enriched_df.iterrows()]

    db.upsert_snapshot_rows(conn, rows)
    db.upsert_snapshot_total(
        conn, today_str, totals["total_value_aud"], totals["total_pl_aud"]
    )
    print(f"  Snapshot saved for {today_str}.")
