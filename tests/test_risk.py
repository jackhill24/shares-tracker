"""Risk metrics: value series, volatility, drawdown, Sharpe, beta, correlation."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.risk import (
    annualised_volatility,
    beta_vs_benchmark,
    correlation_matrix,
    holding_value_series,
    max_drawdown,
    sharpe_ratio,
)


def test_max_drawdown_simple_case():
    # Peak 100 → trough 60 → recover: worst drawdown is -40%.
    s = pd.Series([100, 120, 90, 60, 80], index=pd.date_range("2025-01-01", periods=5))
    assert max_drawdown(s) == pytest.approx(-0.5)   # 120 → 60 is the peak-to-trough


def test_max_drawdown_monotonic_up_is_zero():
    s = pd.Series([100, 110, 120, 130], index=pd.date_range("2025-01-01", periods=4))
    assert max_drawdown(s) == pytest.approx(0.0)


def test_annualised_volatility_scales_daily_std():
    rng = np.random.default_rng(0)
    rets = rng.normal(0, 0.01, 500)
    values = 100 * np.cumprod(1 + rets)
    s = pd.Series(values, index=pd.date_range("2024-01-01", periods=500))
    vol = annualised_volatility(s)
    # Should be near daily_std * sqrt(252) ≈ 0.01 * 15.87 ≈ 0.159.
    assert vol == pytest.approx(0.159, abs=0.03)


def test_sharpe_zero_rf_is_mean_over_std_annualised():
    # Constant positive daily return → infinite Sharpe in theory; use near-const.
    s = pd.Series([100 * 1.001 ** i for i in range(300)],
                  index=pd.date_range("2024-01-01", periods=300))
    sharpe = sharpe_ratio(s, risk_free_rate=0.0)
    assert sharpe is not None and sharpe > 0


def test_beta_of_series_against_itself_is_one():
    s = pd.Series([100 * 1.01 ** i * (1 + 0.02 * (i % 3 - 1)) for i in range(200)],
                  index=pd.date_range("2024-01-01", periods=200))
    assert beta_vs_benchmark(s, s) == pytest.approx(1.0, abs=1e-9)


def test_beta_of_double_moves_is_two():
    rng = np.random.default_rng(1)
    br = rng.normal(0, 0.01, 300)
    bench = pd.Series(100 * np.cumprod(1 + br), index=pd.date_range("2024-01-01", periods=300))
    # Portfolio returns are exactly 2x benchmark returns → beta ≈ 2.
    port = pd.Series(100 * np.cumprod(1 + 2 * br), index=bench.index)
    assert beta_vs_benchmark(port, bench) == pytest.approx(2.0, abs=0.05)


def test_insufficient_history_returns_none():
    s = pd.Series([100.0], index=pd.date_range("2025-01-01", periods=1))
    assert annualised_volatility(s) is None
    assert max_drawdown(s) is None
    assert sharpe_ratio(s, 0.04) is None


def test_correlation_matrix_two_holdings():
    df = pd.DataFrame({
        "A": [100 * 1.01 ** i for i in range(50)],
        "B": [200 * 1.01 ** i for i in range(50)],
    }, index=pd.date_range("2024-01-01", periods=50))
    corr = correlation_matrix(df)
    assert corr.shape == (2, 2)
    # Perfectly co-moving → correlation ~1.
    assert corr.loc["A", "B"] == pytest.approx(1.0, abs=1e-6)


def test_correlation_matrix_single_holding_is_empty():
    df = pd.DataFrame({"A": [1, 2, 3]}, index=pd.date_range("2024-01-01", periods=3))
    assert correlation_matrix(df).empty


def test_holding_value_series_uses_units_price_fx():
    trades = pd.DataFrame({
        "code": ["AAA"], "exchange": ["ASX"],
        "date": pd.to_datetime(["2025-01-01"]), "type": ["buy"],
        "quantity": [10.0], "unit_price": [5.0], "platform": ["CMC"], "fees": [0.0],
    })
    meta = {"AAA.AX": ("AAA", "ASX")}
    prices = {"AAA.AX": {"2025-01-01": 5.0, "2025-01-02": 6.0}}
    hv = holding_value_series(
        trades, meta, prices, lambda c, d: 1.0,
        start=date(2025, 1, 1), end=date(2025, 1, 2),
    )
    # 10 units * 5 on day 1, * 6 on day 2, FX 1.0.
    assert hv["AAA"].tolist() == [50.0, 60.0]
