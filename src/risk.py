"""Portfolio risk metrics from cached daily prices (Phase 5).

Builds a daily portfolio value series over a lookback window — for each holding,
units held on the day (replayed from trades, split-adjusted) times that day's
close times the day's FX — then derives:

  * annualised volatility of daily returns,
  * maximum drawdown,
  * Sharpe ratio (excess over the configured risk-free rate),
  * beta vs the primary benchmark,
  * a per-holding daily-return correlation matrix.

All reads are from the DB-backed caches the caller has populated; nothing here
hits the network. Metrics are estimates and degrade gracefully (None) when
there isn't enough history.
"""
from bisect import bisect_right
from datetime import date, timedelta
from typing import Callable

import numpy as np
import pandas as pd

from src.income import units_held_series

_TRADING_DAYS = 252


def _daily_dates(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def holding_value_series(
    trades_df: pd.DataFrame,
    ticker_meta: dict[str, tuple[str, str]],
    price_series: dict[str, dict[str, float]],
    fx_series: Callable[[str, str], float],
    start: date,
    end: date,
    corporate_actions: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Daily AUD value per holding over [start, end].

    ``price_series`` maps yf_ticker → {date_str: close_local}; missing days are
    forward-filled (nearest prior close). ``fx_series`` gives AUD per local unit
    at a date. Returns a wide frame indexed by date with one column per code that
    was ever held in the window (all-zero columns dropped).
    """
    from src.market_data import EXCHANGE_MAP

    days = _daily_dates(start, end)
    day_strs = [d.isoformat() for d in days]

    cols: dict[str, list[float]] = {}
    for ticker, (code, exchange) in ticker_meta.items():
        currency = EXCHANGE_MAP[exchange]["currency"]
        prices = price_series.get(ticker, {})
        if not prices:
            continue
        price_dates = sorted(prices)
        units_by_day = units_held_series(trades_df, code, exchange, days, corporate_actions)

        series = []
        for units, d_str in zip(units_by_day, day_strs):
            # Nearest close at or before the day (forward-fill).
            i = bisect_right(price_dates, d_str)
            px = prices[price_dates[i - 1]] if i else None
            if units <= 0 or px is None:
                series.append(0.0)
            else:
                series.append(units * px * fx_series(currency, d_str))
        # Aggregate multiple tickers for the same code (unlikely) additively.
        if code in cols:
            cols[code] = [a + b for a, b in zip(cols[code], series)]
        else:
            cols[code] = series

    df = pd.DataFrame(cols, index=pd.to_datetime(day_strs))
    # Drop holdings that were never held (all zero) in the window.
    df = df.loc[:, (df != 0).any(axis=0)]
    return df


def _daily_returns(value_series: pd.Series) -> pd.Series:
    """Pct-change returns on trading days (rows where value > 0 both sides)."""
    s = value_series[value_series > 0]
    return s.pct_change().dropna()


def annualised_volatility(portfolio_value: pd.Series) -> float | None:
    r = _daily_returns(portfolio_value)
    if len(r) < 2:
        return None
    return float(r.std(ddof=1) * np.sqrt(_TRADING_DAYS))


def max_drawdown(portfolio_value: pd.Series) -> float | None:
    """Largest peak-to-trough decline as a negative fraction (e.g. -0.32)."""
    s = portfolio_value[portfolio_value > 0]
    if len(s) < 2:
        return None
    running_max = s.cummax()
    drawdown = s / running_max - 1.0
    return float(drawdown.min())


def sharpe_ratio(portfolio_value: pd.Series, risk_free_rate: float) -> float | None:
    """Annualised Sharpe: (mean daily excess return) / daily vol, annualised.

    The annual risk-free rate is converted to a per-trading-day rate before
    subtracting from daily returns.
    """
    r = _daily_returns(portfolio_value)
    if len(r) < 2:
        return None
    rf_daily = risk_free_rate / _TRADING_DAYS
    excess = r - rf_daily
    sd = excess.std(ddof=1)
    if sd == 0:
        return None
    return float(excess.mean() / sd * np.sqrt(_TRADING_DAYS))


def beta_vs_benchmark(
    portfolio_value: pd.Series,
    benchmark_value: pd.Series,
) -> float | None:
    """Beta = cov(port, bench) / var(bench) on overlapping daily returns."""
    pr = _daily_returns(portfolio_value)
    br = _daily_returns(benchmark_value)
    joined = pd.concat([pr.rename("p"), br.rename("b")], axis=1).dropna()
    if len(joined) < 2:
        return None
    var_b = joined["b"].var(ddof=1)
    # A benchmark with (near-)constant returns has no variance to regress
    # against — beta is undefined rather than astronomically large.
    if var_b < 1e-12:
        return None
    cov = joined["p"].cov(joined["b"])
    return float(cov / var_b)


def correlation_matrix(holding_values: pd.DataFrame) -> pd.DataFrame:
    """Pairwise correlation of holdings' daily returns (empty if < 2 holdings)."""
    if holding_values is None or holding_values.shape[1] < 2:
        return pd.DataFrame()
    returns = holding_values.replace(0, np.nan).pct_change()
    returns = returns.dropna(how="all")
    corr = returns.corr()
    return corr.dropna(how="all").dropna(how="all", axis=1)


def compute_risk_metrics(
    trades_df: pd.DataFrame,
    ticker_meta: dict[str, tuple[str, str]],
    price_series: dict[str, dict[str, float]],
    fx_series: Callable[[str, str], float],
    risk_free_rate: float,
    benchmark_value_series: pd.Series | None = None,
    benchmark_label: str | None = None,
    lookback_days: int = 365 * 3,
    corporate_actions: pd.DataFrame | None = None,
    as_of: date | None = None,
) -> dict:
    """Assemble all risk metrics over the trailing ``lookback_days``.

    ``benchmark_value_series`` is the primary benchmark's daily value indexed by
    datetime (its shadow/price series) — used for beta. Returns a dict with the
    scalar metrics, the correlation matrix, and the window bounds. Degenerate
    inputs (too little history) yield None metrics, never an exception.
    """
    as_of = as_of or date.today()
    start = as_of - timedelta(days=lookback_days)

    hv = holding_value_series(
        trades_df, ticker_meta, price_series, fx_series, start, as_of, corporate_actions
    )
    result = {
        "start": start,
        "end": as_of,
        "annualised_volatility": None,
        "max_drawdown": None,
        "sharpe": None,
        "beta": None,
        "benchmark_label": benchmark_label,
        "correlation": pd.DataFrame(),
        "n_days": 0,
    }
    if hv.empty:
        return result

    portfolio_value = hv.sum(axis=1)
    result["n_days"] = int((portfolio_value > 0).sum())
    result["annualised_volatility"] = annualised_volatility(portfolio_value)
    result["max_drawdown"] = max_drawdown(portfolio_value)
    result["sharpe"] = sharpe_ratio(portfolio_value, risk_free_rate)
    result["correlation"] = correlation_matrix(hv)

    if benchmark_value_series is not None and not benchmark_value_series.empty:
        result["beta"] = beta_vs_benchmark(portfolio_value, benchmark_value_series)

    return result
