"""What-if disposal simulator: FIFO vs min-tax parcel selection."""
from datetime import date

import pandas as pd
import pytest

from src.cgt import simulate_disposal


def _parcels():
    # Two open AAA parcels:
    #   old, cheap, held >12m  → big discount-eligible gain
    #   new, dear, held <12m   → small non-discount gain
    return pd.DataFrame([
        {"code": "AAA", "exchange": "ASX", "currency": "AUD",
         "acquired": date(2023, 1, 1), "units": 100.0,
         "unit_cost_local": 1.0, "fx_at_buy": 1.0},
        {"code": "AAA", "exchange": "ASX", "currency": "AUD",
         "acquired": date(2026, 6, 1), "units": 100.0,
         "unit_cost_local": 9.0, "fx_at_buy": 1.0},
    ])


def test_fifo_disposes_oldest_first():
    res = simulate_disposal(
        _parcels(), "AAA", "ASX", units=100, price_local=10.0, fx_now=1.0,
        disposed=date(2026, 7, 15),
    )
    fifo = res["fifo"]
    assert fifo["units_disposed"] == 100
    assert len(fifo["parcels"]) == 1
    assert fifo["parcels"][0]["acquired"] == date(2023, 1, 1)
    # Gain = (10 - 1) * 100 = 900; discount-eligible → taxable 450.
    assert fifo["total_gain_aud"] == pytest.approx(900.0)
    assert fifo["total_taxable_gain_aud"] == pytest.approx(450.0)


def test_min_tax_prefers_the_lower_taxable_parcel_first():
    res = simulate_disposal(
        _parcels(), "AAA", "ASX", units=100, price_local=10.0, fx_now=1.0,
        disposed=date(2026, 7, 15),
    )
    min_tax = res["min_tax"]
    # Per-unit taxable: old parcel gain 9 → discounted 4.5; new parcel gain 1 → 1.
    # So the NEW parcel (taxable 1/unit) is disposed first under min-tax.
    assert min_tax["parcels"][0]["acquired"] == date(2026, 6, 1)
    assert min_tax["total_gain_aud"] == pytest.approx(100.0)      # (10-9)*100
    assert min_tax["total_taxable_gain_aud"] == pytest.approx(100.0)  # not eligible


def test_min_tax_beats_or_equals_fifo_on_taxable_gain():
    res = simulate_disposal(
        _parcels(), "AAA", "ASX", units=100, price_local=10.0, fx_now=1.0,
        disposed=date(2026, 7, 15),
    )
    assert res["min_tax"]["total_taxable_gain_aud"] <= res["fifo"]["total_taxable_gain_aud"]


def test_disposal_straddles_two_parcels():
    res = simulate_disposal(
        _parcels(), "AAA", "ASX", units=150, price_local=10.0, fx_now=1.0,
        disposed=date(2026, 7, 15),
    )
    fifo = res["fifo"]
    assert fifo["units_disposed"] == 150
    assert len(fifo["parcels"]) == 2
    assert fifo["parcels"][0]["units"] == 100
    assert fifo["parcels"][1]["units"] == 50


def test_disposal_capped_at_available_units():
    res = simulate_disposal(
        _parcels(), "AAA", "ASX", units=500, price_local=10.0, fx_now=1.0,
        disposed=date(2026, 7, 15),
    )
    assert res["fifo"]["units_disposed"] == 200  # only 200 on hand


def test_foreign_disposal_uses_buy_fx_for_cost_now_fx_for_proceeds():
    parcels = pd.DataFrame([{
        "code": "AAPL", "exchange": "NASDAQ", "currency": "USD",
        "acquired": date(2023, 1, 1), "units": 10.0,
        "unit_cost_local": 100.0, "fx_at_buy": 1.4,
    }])
    res = simulate_disposal(
        parcels, "AAPL", "NASDAQ", units=10, price_local=150.0, fx_now=1.5,
        disposed=date(2026, 7, 15),
    )
    p = res["fifo"]["parcels"][0]
    assert p["cost_base_aud"] == pytest.approx(10 * 100 * 1.4)   # 1400
    assert p["proceeds_aud"] == pytest.approx(10 * 150 * 1.5)    # 2250
    assert p["gain_aud"] == pytest.approx(850.0)
    assert p["taxable_gain_aud"] == pytest.approx(425.0)         # >12m → discounted
