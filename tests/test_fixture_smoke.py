"""Smoke tests for the tests/fixtures/trades.csv fixture itself.

Confirms the fixture loads, validates, and exercises the FIFO/CGT paths it
was built for. Golden-number CGT correctness tests (ATO discount ordering,
carry-forward, 12-month rule) live in test_cgt.py once Phase 1 lands.
"""
from src.ingest import load_trades, validate_trades
from src.cgt import compute_realised_gains, summarise_by_fy
from src.compute import compute_holdings


def test_fixture_loads_and_validates(trades_csv_path):
    df = load_trades(trades_csv_path)
    validate_trades(df)
    assert len(df) == 15


def test_fixture_has_reentered_position(trades_csv_path):
    """AAA is fully closed then re-bought."""
    df = load_trades(trades_csv_path)
    holdings = compute_holdings(df)
    aaa = holdings[holdings["code"] == "AAA"]
    assert len(aaa) == 1
    assert aaa.iloc[0]["units"] == 50


def test_fixture_sell_straddles_two_parcels(trades_csv_path, flat_fx_provider):
    """BBB's single sell of 300 units is matched against two buy parcels (200 + 100)."""
    df = load_trades(trades_csv_path)
    realised, _ = compute_realised_gains(df, flat_fx_provider)
    bbb = realised[realised["code"] == "BBB"]
    assert len(bbb) == 2
    assert sorted(bbb["units"]) == [100, 200]


def test_fixture_has_12_month_boundary_pair(trades_csv_path, flat_fx_provider):
    """CCC is held exactly 365 days; DDD is held 366 days."""
    df = load_trades(trades_csv_path)
    realised, _ = compute_realised_gains(df, flat_fx_provider)
    ccc = realised[realised["code"] == "CCC"].iloc[0]
    ddd = realised[realised["code"] == "DDD"].iloc[0]
    assert ccc["held_days"] == 365
    assert ddd["held_days"] == 366


def test_fixture_fy_has_mixed_gain_and_loss(trades_csv_path, flat_fx_provider):
    """FY25/26 contains both a gain (CCC) and a loss (DDD)."""
    df = load_trades(trades_csv_path)
    realised, _ = compute_realised_gains(df, flat_fx_provider)
    fy_summary = summarise_by_fy(realised)
    fy2526 = fy_summary[fy_summary["fy"] == "FY25/26"].iloc[0]
    assert fy2526["gross_gains"] > 0
    assert fy2526["gross_losses"] < 0
