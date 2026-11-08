"""Phase 3 performance analytics tests (src/performance.py).

All numbers are hand-computable:
  - XIRR against known closed-form cases (a plain 1-year doubling, a real
    multi-flow case cross-checked against Excel's XIRR).
  - The shadow benchmark portfolio against a 3-trade example with round prices.
  - TWR chain-linking with one mid-period contribution, verified to strip the
    contribution out of the period return.
Everything runs offline — no fx layer for AUD, benchmark prices supplied inline.
"""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.performance import (
    xirr,
    portfolio_xirr,
    shadow_portfolio,
    trade_cashflows,
    net_contributions_series,
    time_weighted_return,
)


def _aud_fx(currency: str, date_str: str) -> float:
    return 1.0 if currency == "AUD" else 1.5


def _trades(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    if "fees" not in df.columns:
        df["fees"] = 0.0
    return df.sort_values("date").reset_index(drop=True)


# --------------------------------------------------------------------------
# XIRR
# --------------------------------------------------------------------------

def _expected_doubling(t0: date, t1: date) -> float:
    """(1+r)^years = 2 with the module's 365-day-basis year count."""
    years = (t1 - t0).days / 365.0
    return 2.0 ** (1.0 / years) - 1.0


def test_xirr_one_year_doubling():
    # Invest $100, get $200 a year later. 2024→2025 is a 366-day (leap) span,
    # and the module annualises on a fixed 365-day basis, so the rate is a hair
    # under 100% — computed the same way here so the golden value is exact.
    t0, t1 = date(2024, 1, 1), date(2025, 1, 1)
    r = xirr([(t0, -100.0), (t1, 200.0)])
    assert r == pytest.approx(_expected_doubling(t0, t1), abs=1e-6)


def test_xirr_flat_is_zero():
    flows = [(date(2024, 1, 1), -100.0), (date(2025, 1, 1), 100.0)]
    assert xirr(flows) == pytest.approx(0.0, abs=1e-6)


def test_xirr_half_year_doubling_annualises_up():
    # Double in half a year → (1+r)^0.5 = 2 → r = 3.0 (300%).
    flows = [(date(2024, 1, 1), -100.0), (date(2024, 7, 1), 200.0)]
    r = xirr(flows)
    # ~183 days / 365 ≈ 0.5014 years; solve (1+r)^years = 2.
    years = (date(2024, 7, 1) - date(2024, 1, 1)).days / 365.0
    expected = 2.0 ** (1.0 / years) - 1.0
    assert r == pytest.approx(expected, abs=1e-4)


def test_xirr_multi_flow_solves_xnpv_to_zero():
    # A real multi-flow case: the defining property of XIRR is XNPV(rate) == 0.
    flows = [
        (date(2020, 1, 1), -10000.0),
        (date(2020, 3, 1), -5000.0),
        (date(2020, 10, 30), 3000.0),
        (date(2021, 2, 15), 18000.0),
    ]
    r = xirr(flows)
    t0 = min(d for d, _ in flows)
    amounts = np.array([a for _, a in flows])
    years = np.array([(d - t0).days / 365.0 for d, _ in flows])
    xnpv = float(np.sum(amounts / (1.0 + r) ** years))
    assert xnpv == pytest.approx(0.0, abs=1e-6)
    # Sanity: net +6000 on 15000 invested over ~1yr → clearly positive.
    assert r > 0


def test_xirr_returns_none_without_sign_change():
    assert xirr([(date(2024, 1, 1), -100.0), (date(2025, 1, 1), -50.0)]) is None
    assert xirr([(date(2024, 1, 1), 100.0)]) is None


def test_xirr_loss_case_is_negative():
    # Halve over ~1yr → roughly −50%; exact value is day-count dependent.
    t0, t1 = date(2024, 1, 1), date(2025, 1, 1)
    years = (t1 - t0).days / 365.0
    expected = 0.5 ** (1.0 / years) - 1.0
    assert xirr([(t0, -100.0), (t1, 50.0)]) == pytest.approx(expected, abs=1e-6)


def test_portfolio_xirr_uses_terminal_value():
    # One buy of $100 a year ago, still worth $150 today → +50% over ~1yr.
    trades = _trades([
        {"code": "X", "exchange": "ASX", "date": "2024-01-01", "type": "buy",
         "quantity": 10, "unit_price": 10.0, "platform": "P"},
    ])
    cf = trade_cashflows(trades, _aud_fx)
    r = portfolio_xirr(cf, current_value_aud=150.0, as_of=date(2025, 1, 1))
    years = (date(2025, 1, 1) - date(2024, 1, 1)).days / 365.0
    expected = 1.5 ** (1.0 / years) - 1.0
    assert r == pytest.approx(expected, abs=1e-4)


# --------------------------------------------------------------------------
# Cashflows / contributions
# --------------------------------------------------------------------------

def test_trade_cashflows_sign_and_fx_and_fees():
    trades = _trades([
        {"code": "A", "exchange": "ASX", "date": "2024-01-01", "type": "buy",
         "quantity": 100, "unit_price": 10.0, "platform": "P", "fees": 20.0},
        {"code": "U", "exchange": "NASDAQ", "date": "2024-02-01", "type": "sell",
         "quantity": 5, "unit_price": 100.0, "platform": "P", "fees": 10.0},
    ])
    cf = trade_cashflows(trades, _aud_fx)
    # Buy: -(100*10 + 20) * 1.0 = -1020
    assert cf.iloc[0]["amount_aud"] == pytest.approx(-1020.0)
    # Sell: (5*100 - 10) * 1.5 = 490 * 1.5 = 735
    assert cf.iloc[1]["amount_aud"] == pytest.approx(735.0)


def test_net_contributions_running_sum():
    trades = _trades([
        {"code": "A", "exchange": "ASX", "date": "2024-01-01", "type": "buy",
         "quantity": 100, "unit_price": 10.0, "platform": "P"},
        {"code": "A", "exchange": "ASX", "date": "2024-06-01", "type": "sell",
         "quantity": 40, "unit_price": 12.0, "platform": "P"},
    ])
    cf = trade_cashflows(trades, _aud_fx)
    series = net_contributions_series(
        cf, [date(2024, 3, 1), date(2024, 12, 1)]
    )
    # First date: only the buy has happened → net invested 1000.
    assert series.iloc[0]["net_contributed_aud"] == pytest.approx(1000.0)
    # Second date: buy 1000 - sell 480 = 520 net invested.
    assert series.iloc[1]["net_contributed_aud"] == pytest.approx(520.0)


# --------------------------------------------------------------------------
# Shadow benchmark portfolio (3-trade hand example)
# --------------------------------------------------------------------------

def test_shadow_portfolio_three_trades():
    # Benchmark priced at $10 on buy-1, $20 on buy-2, $25 on sell, $40 at eval.
    # Trade 1: buy invests $1000 AUD at benchmark $10 → 100 units.
    # Trade 2: buy invests $1000 AUD at benchmark $20 → 50 units. (150 total)
    # Trade 3: sell withdraws $500 AUD at benchmark $25 → 20 units sold. (130)
    trades = _trades([
        {"code": "A", "exchange": "ASX", "date": "2024-01-01", "type": "buy",
         "quantity": 100, "unit_price": 10.0, "platform": "P"},  # $1000 out
        {"code": "A", "exchange": "ASX", "date": "2024-02-01", "type": "buy",
         "quantity": 50, "unit_price": 20.0, "platform": "P"},   # $1000 out
        {"code": "A", "exchange": "ASX", "date": "2024-03-01", "type": "sell",
         "quantity": 25, "unit_price": 20.0, "platform": "P"},   # $500 in
    ])
    bench_prices = {
        "2024-01-01": 10.0,
        "2024-02-01": 20.0,
        "2024-03-01": 25.0,
        "2024-06-01": 40.0,
    }
    shadow = shadow_portfolio(
        trades, bench_prices, _aud_fx, [date(2024, 6, 1)]
    )
    # Final units = 100 + 50 - (500/25) = 150 - 20 = 130. Value @ $40 = $5200.
    assert shadow.iloc[0]["value_aud"] == pytest.approx(5200.0)


def test_shadow_portfolio_forward_fills_weekend_price():
    # Trade on a date with no exact benchmark price uses nearest prior close.
    trades = _trades([
        {"code": "A", "exchange": "ASX", "date": "2024-01-06", "type": "buy",
         "quantity": 100, "unit_price": 10.0, "platform": "P"},  # $1000 out
    ])
    bench_prices = {"2024-01-05": 10.0, "2024-01-08": 20.0}  # no 6th (weekend)
    shadow = shadow_portfolio(trades, bench_prices, _aud_fx, [date(2024, 1, 8)])
    # Bought at prior close $10 → 100 units; valued at $20 → $2000.
    assert shadow.iloc[0]["value_aud"] == pytest.approx(2000.0)


# --------------------------------------------------------------------------
# Time-weighted return (chain-linking with a mid-period contribution)
# --------------------------------------------------------------------------

def test_twr_strips_out_midperiod_contribution():
    # Start $100. Grows 10% to $110. Then a $50 contribution lands, and the
    # snapshot right after shows $160. No market move in the second sub-period.
    # TWR should be the 10% growth only, NOT (160-100)/100 = 60%.
    value_series = pd.DataFrame({
        "date": [date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1)],
        "value": [100.0, 110.0, 160.0],
    })
    # A $50 buy (investor convention: negative amount_aud) between Feb and Mar.
    cashflows = pd.DataFrame({
        "date": [date(2024, 2, 15)],
        "amount_aud": [-50.0],
    })
    twr = time_weighted_return(value_series, cashflows,
                               window_start=date(2024, 1, 1), as_of=date(2024, 3, 1))
    # Sub 1: 100→110 = +10%. Sub 2: (160 - 50 contribution - 110)/110 = 0%.
    assert twr["total_return"] == pytest.approx(0.10, abs=1e-9)
    # "Show the working" inputs surfaced to the dashboard.
    assert twr["start_value"] == pytest.approx(100.0)
    assert twr["end_value"] == pytest.approx(160.0)
    assert twr["net_contributions"] == pytest.approx(50.0)


def test_twr_no_cashflow_is_simple_return():
    value_series = pd.DataFrame({
        "date": [date(2024, 1, 1), date(2025, 1, 1)],
        "value": [100.0, 120.0],
    })
    cashflows = pd.DataFrame({"date": [], "amount_aud": []})
    twr = time_weighted_return(value_series, cashflows)
    assert twr["total_return"] == pytest.approx(0.20, abs=1e-9)
    # ~1 year → annualised ≈ 20%.
    assert twr["annualised"] == pytest.approx(0.20, abs=2e-3)


def test_twr_empty_series_returns_none():
    empty = pd.DataFrame(columns=["date", "value"])
    twr = time_weighted_return(empty, pd.DataFrame({"date": [], "amount_aud": []}))
    assert twr["total_return"] is None
    assert twr["start_value"] is None
    assert twr["end_value"] is None
    assert twr["net_contributions"] is None


def test_twr_worse_than_total_loss_has_no_annual_rate():
    # A $10k buy that shows up as $1k of value (a mistyped trade price) is a
    # sub-period return below -100%: no real annual rate, so None, not complex.
    value_series = pd.DataFrame({
        "date": [date(2024, 1, 1), date(2024, 6, 1), date(2025, 1, 1)],
        "value": [1000.0, 2000.0, 2100.0],
    })
    cashflows = pd.DataFrame({"date": [date(2024, 6, 1)], "amount_aud": [-10000.0]})
    twr = time_weighted_return(value_series, cashflows)
    assert twr["total_return"] < -1
    assert twr["annualised"] is None
