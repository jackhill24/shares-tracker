"""Ingest-layer tests for src/ingest.py.

Covers the CSV-facing conveniences that the rest of the pipeline relies on:
  - the transaction-cost column may be named "fees" or "brokerage" (or both,
    summed) and always surfaces as the internal "fees" column
  - a "drp" (dividend reinvestment) row is a CGT acquisition — normalised to a
    buy for every downstream consumer, but flagged via is_drp for the audit trail
  - fees/brokerage default to 0 when absent, and negative costs are rejected
"""
import pandas as pd
import pytest

from src.ingest import load_trades, validate_trades


def _write_csv(tmp_path, rows: list[str], header: str) -> str:
    p = tmp_path / "trades.csv"
    p.write_text(header + "\n" + "\n".join(rows) + "\n")
    return str(p)


_H_BASE = "code,exchange,date,type,quantity,unit_price,platform"


# --------------------------------------------------------------------------
# Fee / brokerage column aliasing
# --------------------------------------------------------------------------

def test_brokerage_column_maps_to_fees(tmp_path):
    path = _write_csv(
        tmp_path,
        ["AAA,ASX,2020-07-31,buy,100,10.00,Test Broker,19.95"],
        _H_BASE + ",brokerage",
    )
    df = load_trades(path)
    assert df.iloc[0]["fees"] == pytest.approx(19.95)


def test_fees_column_still_works(tmp_path):
    path = _write_csv(
        tmp_path,
        ["AAA,ASX,2020-07-31,buy,100,10.00,Test Broker,19.95"],
        _H_BASE + ",fees",
    )
    df = load_trades(path)
    assert df.iloc[0]["fees"] == pytest.approx(19.95)


def test_fees_and_brokerage_are_summed(tmp_path):
    # E.g. brokerage 19.95 + GST 2.00 booked in separate columns.
    path = _write_csv(
        tmp_path,
        ["AAA,ASX,2020-07-31,buy,100,10.00,Test Broker,19.95,2.00"],
        _H_BASE + ",brokerage,fees",
    )
    df = load_trades(path)
    assert df.iloc[0]["fees"] == pytest.approx(21.95)


def test_missing_fee_column_defaults_to_zero(tmp_path):
    path = _write_csv(
        tmp_path,
        ["AAA,ASX,2020-07-31,buy,100,10.00,Test Broker"],
        _H_BASE,
    )
    df = load_trades(path)
    assert df.iloc[0]["fees"] == pytest.approx(0.0)


def test_blank_brokerage_cell_treated_as_zero(tmp_path):
    path = _write_csv(
        tmp_path,
        [
            "AAA,ASX,2020-07-31,buy,100,10.00,Test Broker,19.95",
            "BBB,ASX,2020-08-11,buy,200,5.00,Test Broker,",
        ],
        _H_BASE + ",brokerage",
    )
    df = load_trades(path).set_index("code")
    assert df.loc["AAA", "fees"] == pytest.approx(19.95)
    assert df.loc["BBB", "fees"] == pytest.approx(0.0)


def test_negative_brokerage_rejected(tmp_path):
    path = _write_csv(
        tmp_path,
        ["AAA,ASX,2020-07-31,buy,100,10.00,Test Broker,-5.00"],
        _H_BASE + ",brokerage",
    )
    df = load_trades(path)
    with pytest.raises(ValueError, match="non-negative"):
        validate_trades(df)


# --------------------------------------------------------------------------
# DRP normalisation
# --------------------------------------------------------------------------

def test_drp_normalised_to_buy_and_flagged(tmp_path):
    path = _write_csv(
        tmp_path,
        [
            "VAS,ASX,2021-01-01,buy,100,90.00,Broker,20.00",
            "VAS,ASX,2021-07-15,drp,2,95.00,Broker,0.00",
        ],
        _H_BASE + ",brokerage",
    )
    df = load_trades(path).set_index("date")
    drp_row = df.loc[pd.Timestamp("2021-07-15")]
    # Treated as a buy for every downstream calculation ...
    assert drp_row["type"] == "buy"
    # ... but the reinvestment is still identifiable.
    assert bool(drp_row["is_drp"]) is True
    assert bool(df.loc[pd.Timestamp("2021-01-01")]["is_drp"]) is False


def test_drp_passes_validation(tmp_path):
    path = _write_csv(
        tmp_path,
        ["VAS,ASX,2021-07-15,drp,2,95.00,Broker,0.00"],
        _H_BASE + ",brokerage",
    )
    # No holdings to reinvest against would normally trip the sell check, but a
    # drp is an acquisition, so validation must pass cleanly.
    validate_trades(load_trades(path))


def test_invalid_type_still_rejected(tmp_path):
    path = _write_csv(
        tmp_path,
        ["VAS,ASX,2021-07-15,hold,2,95.00,Broker,0.00"],
        _H_BASE + ",brokerage",
    )
    df = load_trades(path)
    with pytest.raises(ValueError, match="must be 'buy', 'sell' or 'drp'"):
        validate_trades(df)
