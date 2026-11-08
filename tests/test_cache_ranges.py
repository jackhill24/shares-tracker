"""Range-based cache coverage (M3): fetch_ranges accounting in db.py."""
import pandas as pd
import pytest

from src import db


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "t.db")
    yield c
    c.close()


def test_no_ranges_means_whole_span_missing(conn):
    gaps = db.range_missing_subranges(conn, "price", "AAA.AX", "2021-01-01", "2021-01-31")
    assert gaps == [("2021-01-01", "2021-01-31")]


def test_fully_covered_span_has_no_gaps(conn):
    db.add_fetch_range(conn, "price", "AAA.AX", "2021-01-01", "2021-12-31")
    assert db.range_missing_subranges(conn, "price", "AAA.AX", "2021-03-01", "2021-06-30") == []


def test_partial_coverage_returns_only_uncovered_tail(conn):
    db.add_fetch_range(conn, "price", "AAA.AX", "2021-01-01", "2021-06-30")
    gaps = db.range_missing_subranges(conn, "price", "AAA.AX", "2021-01-01", "2021-09-30")
    assert gaps == [("2021-07-01", "2021-09-30")]


def test_gap_between_two_covered_ranges(conn):
    db.add_fetch_range(conn, "price", "AAA.AX", "2021-01-01", "2021-03-31")
    db.add_fetch_range(conn, "price", "AAA.AX", "2021-07-01", "2021-09-30")
    gaps = db.range_missing_subranges(conn, "price", "AAA.AX", "2021-01-01", "2021-09-30")
    assert gaps == [("2021-04-01", "2021-06-30")]


def test_adjacent_ranges_merge(conn):
    db.add_fetch_range(conn, "price", "AAA.AX", "2021-01-01", "2021-03-31")
    db.add_fetch_range(conn, "price", "AAA.AX", "2021-04-01", "2021-06-30")
    # Directly adjacent (next day) → single merged range, no interior gap.
    assert db.get_fetch_ranges(conn, "price", "AAA.AX") == [("2021-01-01", "2021-06-30")]


def test_holiday_with_no_rows_is_not_a_permanent_gap(conn):
    # A range fetched but yielding no price rows (e.g. all holidays) is still
    # recorded as covered, so it won't be re-requested every run.
    db.add_fetch_range(conn, "price", "XYZ.AX", "2021-12-25", "2021-12-26")
    assert db.get_prices_in_range(conn, "XYZ.AX", "2021-12-25", "2021-12-26") == {}
    assert db.range_missing_subranges(conn, "price", "XYZ.AX", "2021-12-25", "2021-12-26") == []


def test_migration_clears_price_history_once(tmp_path):
    path = tmp_path / "m.db"
    c = db.init_db(path)
    db.upsert_prices(c, "AAA.AX", {"2021-01-01": 10.0})
    db.add_fetch_range(c, "price", "AAA.AX", "2021-01-01", "2021-01-01")
    c.close()
    # Re-opening runs migrations again but the id is already marked → no wipe.
    c2 = db.init_db(path)
    assert db.get_prices_in_range(c2, "AAA.AX", "2021-01-01", "2021-01-01") == {"2021-01-01": 10.0}
    c2.close()
