"""Snapshot data-integrity: skip-on-NaN (H5) and gap-fill idempotency (H6).

These run fully offline: price/FX history is seeded directly into the DB and
fetch_ranges is marked as covering the whole span so backfill_snapshots never
makes a network call.
"""
from datetime import date, timedelta

import pandas as pd
import pytest

from src import db
from src.snapshots import _generate_snapshot_dates, backfill_snapshots


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "snap.db")
    yield c
    c.close()


def _seed_daily_prices(conn, ticker, start: date, end: date, price: float):
    d = start
    rows = {}
    while d <= end:
        rows[d.isoformat()] = price
        d += timedelta(days=1)
    db.upsert_prices(conn, ticker, rows)
    # Mark the whole span covered so get_historical_prices skips the network.
    db.add_fetch_range(conn, "price", ticker, start.isoformat(), end.isoformat())


def _one_asx_buy(buy_date: date):
    df = pd.DataFrame([{
        "code": "AAA", "exchange": "ASX", "date": buy_date, "type": "buy",
        "quantity": 100, "unit_price": 10.0, "platform": "P", "fees": 0.0,
    }])
    df["date"] = pd.to_datetime(df["date"])
    return df


def _one_nasdaq_buy(buy_date: date):
    df = pd.DataFrame([{
        "code": "USX", "exchange": "NASDAQ", "date": buy_date, "type": "buy",
        "quantity": 10, "unit_price": 100.0, "platform": "P", "fees": 0.0,
    }])
    df["date"] = pd.to_datetime(df["date"])
    return df


def test_backfill_writes_snapshots_when_data_present(conn):
    buy = date.today() - timedelta(days=400)
    _seed_daily_prices(conn, "AAA.AX", buy, date.today(), 12.0)
    backfill_snapshots(conn, _one_asx_buy(buy))
    totals = db.load_snapshot_totals(conn)
    assert not totals.empty
    # ASX = AUD, so no FX needed; every snapshot value = 100 * 12 = 1200.
    assert totals["total_value_aud"].tolist() == pytest.approx([1200.0] * len(totals))
    assert not totals["total_value_aud"].isna().any()


def test_snapshot_skipped_entirely_when_fx_missing(conn):
    # A USD position with prices but NO FX seeded: get_fx_at_or_before returns
    # None for every date, so every snapshot must be skipped (H5) — a snapshot
    # with unknown FX would silently value the position at NaN/zero.
    buy = date.today() - timedelta(days=400)
    _seed_daily_prices(conn, "USX", buy, date.today(), 100.0)
    # No fx_history rows for USD, but mark the FX range covered so backfill's
    # pre-fetch does not hit the network.
    db.add_fetch_range(conn, "fx", "USD", buy.isoformat(), date.today().isoformat())

    backfill_snapshots(conn, _one_nasdaq_buy(buy))

    totals = db.load_snapshot_totals(conn)
    # Nothing written at all — better a missing series than a wrong one.
    assert totals.empty


def test_partial_position_missing_fx_skips_whole_snapshot(conn):
    # Two positions on the same dates: AUD (fine) + USD (no FX). The USD gap
    # must skip the ENTIRE snapshot, not just drop the USD line and under-sum.
    buy = date.today() - timedelta(days=400)
    _seed_daily_prices(conn, "AAA.AX", buy, date.today(), 12.0)
    _seed_daily_prices(conn, "USX", buy, date.today(), 100.0)
    db.add_fetch_range(conn, "fx", "USD", buy.isoformat(), date.today().isoformat())

    trades = pd.concat([_one_asx_buy(buy), _one_nasdaq_buy(buy)], ignore_index=True)
    backfill_snapshots(conn, trades)

    assert db.load_snapshot_totals(conn).empty


def test_gap_fill_is_idempotent(conn):
    buy = date.today() - timedelta(days=400)
    _seed_daily_prices(conn, "AAA.AX", buy, date.today(), 12.0)
    trades = _one_asx_buy(buy)

    backfill_snapshots(conn, trades)
    first = db.load_snapshot_totals(conn)
    # Running again writes no new rows and changes nothing.
    backfill_snapshots(conn, trades)
    second = db.load_snapshot_totals(conn)

    assert len(first) == len(second)
    pd.testing.assert_frame_equal(first, second)


def test_gap_fill_heals_a_missing_interior_date(conn):
    buy = date.today() - timedelta(days=400)
    _seed_daily_prices(conn, "AAA.AX", buy, date.today(), 12.0)
    trades = _one_asx_buy(buy)

    backfill_snapshots(conn, trades)
    totals = db.load_snapshot_totals(conn)
    assert len(totals) >= 2

    # Delete a recent snapshot to simulate a gap, then re-run.
    victim = totals.iloc[-2]["date"].strftime("%Y-%m-%d")
    with conn:
        conn.execute("DELETE FROM snapshot_totals WHERE date = ?", (victim,))
    assert db.load_snapshot_totals(conn).shape[0] == len(totals) - 1

    backfill_snapshots(conn, trades)
    healed = db.load_snapshot_totals(conn)
    assert victim in set(healed["date"].dt.strftime("%Y-%m-%d"))
    assert len(healed) == len(totals)


def test_cadence_quarterly_then_every_friday_for_a_year():
    today = date(2026, 11, 8)  # a Sunday
    dates = _generate_snapshot_dates(date(2020, 1, 15), today)
    recent = [d for d in dates if d > today - timedelta(weeks=52)]
    assert recent[-1] == today
    fridays = recent[:-1]
    assert all(d.weekday() == 4 for d in fridays)
    assert all((b - a).days == 7 for a, b in zip(fridays, fridays[1:]))
    assert len(fridays) == 52
    older = [d for d in dates if d <= today - timedelta(weeks=52)]
    assert older[0] == date(2020, 1, 15) and older[1] == date(2020, 4, 15)


def test_cadence_is_the_same_whatever_day_it_runs():
    # Each run used to add its own weekly dates, counted back from that day.
    earliest = date(2022, 3, 1)
    monday = set(_generate_snapshot_dates(earliest, date(2026, 11, 2)))
    thursday = set(_generate_snapshot_dates(earliest, date(2026, 11, 5)))
    assert thursday - monday == {date(2026, 11, 5)}  # only that day's "today"


def test_cadence_starts_at_the_first_trade():
    today = date(2026, 11, 8)
    dates = _generate_snapshot_dates(date(2026, 6, 10), today)
    assert dates[0] == date(2026, 6, 12)  # first Friday on or after it
    assert dates[-1] == today
