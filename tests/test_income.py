"""Dividend income: units-held replay, FX conversion, FY grouping, franking."""
from datetime import date

import pandas as pd
import pytest

from src.income import (
    compute_dividend_income,
    summarise_income_by_fy,
    trailing_yield,
    units_held_on,
    units_held_series,
)


def _trades():
    # 100 VAS bought 2024-08-01, 40 sold 2025-02-01 → 60 held after.
    return pd.DataFrame({
        "code": ["VAS", "VAS"],
        "exchange": ["ASX", "ASX"],
        "date": pd.to_datetime(["2024-08-01", "2025-02-01"]),
        "type": ["buy", "sell"],
        "quantity": [100.0, 40.0],
        "unit_price": [90.0, 95.0],
        "platform": ["CMC", "CMC"],
        "fees": [0.0, 0.0],
    })


def test_units_held_on_reflects_buys_and_sells():
    t = _trades()
    assert units_held_on(t, "VAS", "ASX", date(2024, 7, 1)) == 0.0    # before buy
    assert units_held_on(t, "VAS", "ASX", date(2024, 8, 1)) == 100.0  # on buy day
    assert units_held_on(t, "VAS", "ASX", date(2025, 1, 1)) == 100.0  # before sell
    assert units_held_on(t, "VAS", "ASX", date(2025, 2, 1)) == 60.0   # after sell


def test_units_held_on_applies_split():
    t = _trades()
    ca = pd.DataFrame({
        "code": ["VAS"], "exchange": ["ASX"],
        "date": pd.to_datetime(["2024-12-01"]), "ratio": [2.0],
    })
    # 100 held pre-split → 200 after the 2-for-1 on 2024-12-01.
    assert units_held_on(t, "VAS", "ASX", date(2025, 1, 1), ca) == 200.0


def _split_trades() -> pd.DataFrame:
    return pd.DataFrame({
        "code": ["X", "X", "X"], "exchange": ["ASX"] * 3,
        "date": pd.to_datetime(["2024-01-01", "2025-01-01", "2025-06-01"]),
        "type": ["buy", "buy", "buy"], "quantity": [100.0, 5.0, 10.0],
    })


def test_units_bought_after_split_are_not_scaled():
    ca = pd.DataFrame({"code": ["X"], "exchange": ["ASX"],
                       "date": pd.to_datetime(["2025-01-01"]), "ratio": [2.0]})
    # 100 + same-day 5 → 210 at the split (as compute_holdings orders it);
    # the 10 bought afterwards are already post-split units.
    assert units_held_on(_split_trades(), "X", "ASX", date(2025, 1, 1), ca) == 210.0
    assert units_held_on(_split_trades(), "X", "ASX", date(2025, 7, 1), ca) == 220.0


def test_units_held_series_matches_single_day():
    t = _split_trades()
    ca = pd.DataFrame({"code": ["X"], "exchange": ["ASX"],
                       "date": pd.to_datetime(["2025-01-01"]), "ratio": [2.0]})
    days = [date(2023, 12, 31), date(2024, 1, 1), date(2024, 12, 31),
            date(2025, 1, 1), date(2025, 5, 31), date(2025, 6, 1)]
    assert units_held_series(t, "X", "ASX", days, ca) == [0.0, 100.0, 100.0, 210.0, 210.0, 220.0]
    assert units_held_series(t, "X", "ASX", days, ca) == [
        units_held_on(t, "X", "ASX", d, ca) for d in days
    ]


def _flat_fx(currency, date_str):
    return 1.0 if currency == "AUD" else 1.5


def test_income_multiplies_units_by_per_share_and_converts_fx():
    t = _trades()
    # Dividend of $2/share on 2025-01-15 (100 held) and $2 on 2025-03-15 (60 held).
    divs = {"VAS.AX": {"2025-01-15": 2.0, "2025-03-15": 2.0}}
    meta = {"VAS.AX": ("VAS", "ASX")}
    inc = compute_dividend_income(t, divs, meta, _flat_fx)
    assert len(inc) == 2
    jan = inc[inc["ex_date"] == date(2025, 1, 15)].iloc[0]
    mar = inc[inc["ex_date"] == date(2025, 3, 15)].iloc[0]
    assert jan["amount_aud"] == pytest.approx(200.0)   # 100 * 2 * 1.0
    assert mar["amount_aud"] == pytest.approx(120.0)   # 60 * 2 * 1.0


def test_income_drops_exdates_with_nothing_held():
    t = _trades()
    divs = {"VAS.AX": {"2024-01-01": 5.0}}  # before any units held
    meta = {"VAS.AX": ("VAS", "ASX")}
    inc = compute_dividend_income(t, divs, meta, _flat_fx)
    assert inc.empty


def test_foreign_dividend_converts_at_fx():
    t = pd.DataFrame({
        "code": ["AAPL"], "exchange": ["NASDAQ"],
        "date": pd.to_datetime(["2024-01-01"]), "type": ["buy"],
        "quantity": [10.0], "unit_price": [150.0], "platform": ["Stake"], "fees": [0.0],
    })
    divs = {"AAPL": {"2024-06-01": 0.25}}  # USD 0.25/share
    meta = {"AAPL": ("AAPL", "NASDAQ")}
    inc = compute_dividend_income(t, divs, meta, _flat_fx)
    # 10 * 0.25 USD * 1.5 AUD/USD = 3.75 AUD
    assert inc.iloc[0]["amount_aud"] == pytest.approx(3.75)


def test_franking_credit_grossed_up_at_30pct():
    t = _trades()
    divs = {"VAS.AX": {"2025-01-15": 2.0}}   # 100 held → $200 AUD
    meta = {"VAS.AX": ("VAS", "ASX")}
    franking = pd.DataFrame({
        "code": ["VAS"], "exchange": ["ASX"],
        "date": pd.to_datetime(["2025-01-15"]), "franking_pct": [100.0],
    })
    inc = compute_dividend_income(t, divs, meta, _flat_fx, franking_df=franking)
    row = inc.iloc[0]
    # Fully franked: credit = 200 * 1.0 * 0.30/0.70 ≈ 85.71
    assert row["franking_credit_aud"] == pytest.approx(200 * 0.30 / 0.70, rel=1e-6)


def test_summarise_by_fy_groups_correctly():
    t = _trades()
    # Jan 2025 → FY24/25; Aug 2025 → FY25/26.
    divs = {"VAS.AX": {"2025-01-15": 2.0, "2025-08-15": 1.0}}
    meta = {"VAS.AX": ("VAS", "ASX")}
    inc = compute_dividend_income(t, divs, meta, _flat_fx)
    fy = summarise_income_by_fy(inc)
    assert list(fy["fy"]) == ["FY24/25", "FY25/26"]
    assert fy[fy["fy"] == "FY24/25"].iloc[0]["dividend_income_aud"] == pytest.approx(200.0)
    assert fy[fy["fy"] == "FY25/26"].iloc[0]["dividend_income_aud"] == pytest.approx(60.0)


def test_trailing_yield_on_value_and_cost():
    inc = pd.DataFrame({
        "ex_date": [date(2026, 3, 1), date(2024, 3, 1)],  # one inside TTM, one outside
        "amount_aud": [300.0, 999.0],
    })
    y = trailing_yield(inc, current_value_aud=10000.0, cost_base_aud=6000.0,
                       as_of=date(2026, 7, 15))
    assert y["ttm_income_aud"] == pytest.approx(300.0)
    assert y["yield_on_value"] == pytest.approx(0.03)
    assert y["yield_on_cost"] == pytest.approx(0.05)


def test_empty_income_summarises_empty():
    assert summarise_income_by_fy(pd.DataFrame()).empty
