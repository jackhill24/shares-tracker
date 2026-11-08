"""Corporate-action (split) adjustment through holdings and CGT (H4)."""
import pandas as pd
import pytest

from src.compute import compute_holdings
from src.cgt import compute_realised_gains, open_cost_base_aud_as_of
from datetime import date


def _trades(rows):
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    if "fees" not in df.columns:
        df["fees"] = 0.0
    return df.sort_values("date").reset_index(drop=True)


def _ca(rows):
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df


def _flat_fx(currency, date_str):
    return 1.0 if currency == "AUD" else 1.5


BUY_SPLIT_SELL = [
    {"code": "SPL", "exchange": "ASX", "date": "2021-01-01", "type": "buy",
     "quantity": 100, "unit_price": 10.0, "platform": "P"},
    {"code": "SPL", "exchange": "ASX", "date": "2022-06-01", "type": "sell",
     "quantity": 100, "unit_price": 6.0, "platform": "P"},
]
SPLIT_2_FOR_1 = [{"code": "SPL", "exchange": "ASX", "date": "2021-06-01", "ratio": 2.0}]


def test_split_adjusts_holdings_units_and_cost():
    trades = _trades(BUY_SPLIT_SELL[:1])  # buy only
    h = compute_holdings(trades, _ca(SPLIT_2_FOR_1))
    assert h.iloc[0]["units"] == pytest.approx(200.0)
    assert h.iloc[0]["avg_cost_local"] == pytest.approx(5.0)
    # Total cost preserved.
    assert h.iloc[0]["total_cost_local"] == pytest.approx(1000.0)


def test_split_flows_through_cgt_parcel_queue():
    trades = _trades(BUY_SPLIT_SELL)
    realised, open_df = compute_realised_gains(trades, _flat_fx, _ca(SPLIT_2_FOR_1))
    r = realised.iloc[0]
    # 200 units @ $5 after split; sell 100 @ $6 → cost 500, proceeds 600, gain 100.
    assert r["units"] == pytest.approx(100.0)
    assert r["cost_base_aud"] == pytest.approx(500.0)
    assert r["gain_aud"] == pytest.approx(100.0)
    # 100 units remain open.
    assert open_df.iloc[0]["units"] == pytest.approx(100.0)


def test_split_preserves_acquisition_date_for_discount():
    trades = _trades(BUY_SPLIT_SELL)
    realised, _ = compute_realised_gains(trades, _flat_fx, _ca(SPLIT_2_FOR_1))
    # Held from 2021-01-01 to 2022-06-01 > 12 months → discount eligible,
    # measured from the ORIGINAL buy, not the split date.
    assert bool(realised.iloc[0]["discount_eligible"]) is True


def test_no_corporate_actions_is_unchanged():
    trades = _trades(BUY_SPLIT_SELL)
    r_plain, _ = compute_realised_gains(trades, _flat_fx, None)
    # Without the split: 100 units @ $10, sell 100 @ $6 → gain -400.
    assert r_plain.iloc[0]["gain_aud"] == pytest.approx(-400.0)


def test_open_cost_base_uses_trade_date_fx():
    # Foreign buy: FX at buy matters, not FX at the as-of date.
    trades = _trades([
        {"code": "USX", "exchange": "NASDAQ", "date": "2021-01-01", "type": "buy",
         "quantity": 10, "unit_price": 100.0, "platform": "P"},
    ])
    def fx(currency, date_str):
        return 2.0 if date_str == "2021-01-01" else 99.0  # only buy-date used
    cost = open_cost_base_aud_as_of(trades, date(2023, 1, 1), fx)
    # 10 * 100 * 2.0 = 2000 (buy-date FX), independent of the as-of date FX.
    assert cost[("USX", "NASDAQ", "P")] == pytest.approx(2000.0)
