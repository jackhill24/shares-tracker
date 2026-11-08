"""ATO-correctness tests for src/cgt.py.

These encode the intended tax rules (ATO), not the code's prior behaviour:
  - FIFO parcel matching, including partial parcels and re-entry
  - the 50% discount 12-month rule as a calendar-date test (leap-year safe)
  - loss-before-discount ordering, non-discountable gains offset first
  - capital-loss carry-forward across financial years
  - fees in the cost base (buys) and netted from proceeds (sells)

All monetary reasoning is done in AUD via a controllable FX stub so the
golden numbers are hand-verifiable.
"""
import pandas as pd
import pytest

from src.cgt import (
    compute_realised_gains,
    fy_working_steps,
    summarise_by_fy,
    _discount_eligible,
)
from datetime import date


def _trades(rows: list[dict]) -> pd.DataFrame:
    """Build a trades frame with the columns compute_realised_gains reads."""
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    if "fees" not in df.columns:
        df["fees"] = 0.0
    return df.sort_values("date").reset_index(drop=True)


def _aud_fx(currency: str, date_str: str) -> float:
    """AUD 1:1; every foreign currency a flat 1.5 AUD/unit (date-independent)."""
    return 1.0 if currency == "AUD" else 1.5


# --------------------------------------------------------------------------
# 12-month rule (H3)
# --------------------------------------------------------------------------

def test_discount_exactly_12_months_not_eligible():
    # Acquired 10 Jul 2024, disposed 10 Jul 2025 = exactly 12 months → not eligible.
    assert _discount_eligible(date(2024, 7, 10), date(2025, 7, 10)) is False


def test_discount_12_months_plus_one_day_eligible():
    assert _discount_eligible(date(2024, 7, 9), date(2025, 7, 10)) is True


def test_discount_boundary_across_leap_year():
    # Acquired 29 Feb 2024 (leap). +12 months → 28 Feb 2025 (no 29th).
    # 28 Feb 2025 = exactly 12 months, not eligible; 1 Mar 2025 = eligible.
    assert _discount_eligible(date(2024, 2, 29), date(2025, 2, 28)) is False
    assert _discount_eligible(date(2024, 2, 29), date(2025, 3, 1)) is True


# --------------------------------------------------------------------------
# FIFO matching (partial parcels, re-entry)
# --------------------------------------------------------------------------

def test_fifo_sell_straddles_two_parcels():
    trades = _trades([
        {"code": "X", "exchange": "ASX", "date": "2021-01-01", "type": "buy",
         "quantity": 100, "unit_price": 5.0, "platform": "P"},
        {"code": "X", "exchange": "ASX", "date": "2021-06-01", "type": "buy",
         "quantity": 100, "unit_price": 8.0, "platform": "P"},
        {"code": "X", "exchange": "ASX", "date": "2023-01-01", "type": "sell",
         "quantity": 150, "unit_price": 10.0, "platform": "P"},
    ])
    realised, open_df = compute_realised_gains(trades, _aud_fx)
    assert len(realised) == 2
    # FIFO: first 100 from the $5 parcel, then 50 from the $8 parcel.
    first, second = realised.iloc[0], realised.iloc[1]
    assert first["units"] == 100 and first["unit_cost_local"] == 5.0
    assert second["units"] == 50 and second["unit_cost_local"] == 8.0
    # Gains: (10-5)*100 = 500; (10-8)*50 = 100.
    assert first["gain_aud"] == pytest.approx(500.0)
    assert second["gain_aud"] == pytest.approx(100.0)
    # 50 units of the $8 parcel remain open.
    assert open_df.iloc[0]["units"] == pytest.approx(50.0)


def test_fifo_reentry_after_full_close():
    trades = _trades([
        {"code": "Y", "exchange": "ASX", "date": "2021-01-01", "type": "buy",
         "quantity": 100, "unit_price": 10.0, "platform": "P"},
        {"code": "Y", "exchange": "ASX", "date": "2022-01-01", "type": "sell",
         "quantity": 100, "unit_price": 15.0, "platform": "P"},
        {"code": "Y", "exchange": "ASX", "date": "2023-01-01", "type": "buy",
         "quantity": 40, "unit_price": 12.0, "platform": "P"},
    ])
    realised, open_df = compute_realised_gains(trades, _aud_fx)
    # One disposal (the full close); the re-entry parcel is open and untouched.
    assert len(realised) == 1
    assert realised.iloc[0]["gain_aud"] == pytest.approx(500.0)
    assert len(open_df) == 1
    assert open_df.iloc[0]["units"] == pytest.approx(40.0)
    assert open_df.iloc[0]["unit_cost_local"] == pytest.approx(12.0)


# --------------------------------------------------------------------------
# Fees in cost base / proceeds (M1)
# --------------------------------------------------------------------------

def test_fees_added_to_cost_base_and_netted_from_proceeds():
    trades = _trades([
        {"code": "Z", "exchange": "ASX", "date": "2021-01-01", "type": "buy",
         "quantity": 100, "unit_price": 10.0, "platform": "P", "fees": 20.0},
        {"code": "Z", "exchange": "ASX", "date": "2023-01-01", "type": "sell",
         "quantity": 100, "unit_price": 15.0, "platform": "P", "fees": 10.0},
    ])
    realised, _ = compute_realised_gains(trades, _aud_fx)
    r = realised.iloc[0]
    # Cost base = 100*10 + 20 = 1020; proceeds = 100*15 - 10 = 1490.
    assert r["cost_base_aud"] == pytest.approx(1020.0)
    assert r["proceeds_aud"] == pytest.approx(1490.0)
    assert r["gain_aud"] == pytest.approx(470.0)


def test_buy_fee_apportioned_across_partial_matches():
    # A $20 buy fee on 100 units = $0.20/unit; a 60-unit partial sell should
    # carry $12 of that fee into its cost base.
    trades = _trades([
        {"code": "W", "exchange": "ASX", "date": "2021-01-01", "type": "buy",
         "quantity": 100, "unit_price": 10.0, "platform": "P", "fees": 20.0},
        {"code": "W", "exchange": "ASX", "date": "2023-01-01", "type": "sell",
         "quantity": 60, "unit_price": 12.0, "platform": "P", "fees": 0.0},
    ])
    realised, _ = compute_realised_gains(trades, _aud_fx)
    r = realised.iloc[0]
    # Cost base = 60*10 + (20 * 60/100) = 600 + 12 = 612.
    assert r["cost_base_aud"] == pytest.approx(612.0)


# --------------------------------------------------------------------------
# Discount ordering (H1)
# --------------------------------------------------------------------------

def _fy_row_from_gains(gain_rows: list[dict]) -> pd.Series:
    """Build a realised_df directly from (gain_aud, discount_eligible) rows,
    all disposed in the same FY, and return the single FY summary row."""
    base = dict(code="A", exchange="ASX", currency="AUD",
                acquired=date(2020, 1, 1), disposed=date(2024, 3, 1),
                units=1, unit_cost_local=0.0, unit_proceeds_local=0.0,
                cost_base_aud=0.0, proceeds_aud=0.0, held_days=999)
    df = pd.DataFrame([{**base, **g} for g in gain_rows])
    out = summarise_by_fy(df)
    assert len(out) == 1
    return out.iloc[0]


def test_discount_ordering_acceptance_case():
    # $10k discount-eligible gain, $4k loss → (10k-4k)*0.5 = $3k (not $1k).
    r = _fy_row_from_gains([
        {"gain_aud": 10000.0, "discount_eligible": True},
        {"gain_aud": -4000.0, "discount_eligible": False},
    ])
    assert r["est_taxable_gain"] == pytest.approx(3000.0)


def test_discount_ordering_offsets_non_discount_gains_first():
    # $3k non-disc gain, $10k disc gain, $4k loss.
    # Non-disc first: 3k-4k → 0 (1k loss left); disc: 10k-1k = 9k → *0.5 = 4.5k.
    r = _fy_row_from_gains([
        {"gain_aud": 3000.0, "discount_eligible": False},
        {"gain_aud": 10000.0, "discount_eligible": True},
        {"gain_aud": -4000.0, "discount_eligible": False},
    ])
    assert r["est_taxable_gain"] == pytest.approx(4500.0)


# --------------------------------------------------------------------------
# Loss carry-forward across ≥2 FYs (H2)
# --------------------------------------------------------------------------

def test_loss_carries_forward_across_two_fys():
    # FY1: $5k loss, no gains → carry $5k forward, taxable 0.
    # FY2: $2k non-disc gain → fully absorbed, carry $3k, taxable 0.
    # FY3: $10k disc gain → 10k-3k = 7k → *0.5 = $3.5k, carry 0.
    df = pd.DataFrame([
        dict(code="A", exchange="ASX", currency="AUD",
             acquired=date(2019, 1, 1), disposed=date(2021, 3, 1),
             units=1, unit_cost_local=0, unit_proceeds_local=0,
             cost_base_aud=0, proceeds_aud=0, gain_aud=-5000.0,
             held_days=999, discount_eligible=False),
        dict(code="B", exchange="ASX", currency="AUD",
             acquired=date(2021, 8, 1), disposed=date(2022, 3, 1),
             units=1, unit_cost_local=0, unit_proceeds_local=0,
             cost_base_aud=0, proceeds_aud=0, gain_aud=2000.0,
             held_days=120, discount_eligible=False),
        dict(code="C", exchange="ASX", currency="AUD",
             acquired=date(2019, 1, 1), disposed=date(2023, 3, 1),
             units=1, unit_cost_local=0, unit_proceeds_local=0,
             cost_base_aud=0, proceeds_aud=0, gain_aud=10000.0,
             held_days=999, discount_eligible=True),
    ])
    out = summarise_by_fy(df).set_index("fy")
    assert out.loc["FY20/21", "loss_carried_forward"] == pytest.approx(5000.0)
    assert out.loc["FY20/21", "est_taxable_gain"] == pytest.approx(0.0)
    assert out.loc["FY21/22", "loss_carried_forward"] == pytest.approx(3000.0)
    assert out.loc["FY21/22", "carried_loss_applied"] == pytest.approx(2000.0)
    assert out.loc["FY22/23", "est_taxable_gain"] == pytest.approx(3500.0)
    assert out.loc["FY22/23", "carried_loss_applied"] == pytest.approx(3000.0)
    assert out.loc["FY22/23", "loss_carried_forward"] == pytest.approx(0.0)


def test_est_taxable_never_negative():
    r = _fy_row_from_gains([
        {"gain_aud": 1000.0, "discount_eligible": False},
        {"gain_aud": -9000.0, "discount_eligible": False},
    ])
    assert r["est_taxable_gain"] == pytest.approx(0.0)
    assert r["loss_carried_forward"] == pytest.approx(8000.0)


# --------------------------------------------------------------------------
# fy_working_steps — the dashboard's "show the working" must reproduce
# summarise_by_fy exactly, step by step
# --------------------------------------------------------------------------

def test_working_steps_reconcile_ordering_case():
    # Same scenario as test_discount_ordering_offsets_non_discount_gains_first:
    # $3k non-disc gain, $10k disc gain, $4k loss.
    base = dict(code="A", exchange="ASX", currency="AUD",
                acquired=date(2020, 1, 1), disposed=date(2024, 3, 1),
                units=1, unit_cost_local=0.0, unit_proceeds_local=0.0,
                cost_base_aud=0.0, proceeds_aud=0.0, held_days=999)
    df = pd.DataFrame([
        {**base, "gain_aud": 3000.0, "discount_eligible": False},
        {**base, "gain_aud": 10000.0, "discount_eligible": True},
        {**base, "gain_aud": -4000.0, "discount_eligible": False},
    ])
    steps = fy_working_steps(summarise_by_fy(df))
    assert len(steps) == 1
    s = steps[0]
    assert s["non_disc_gains"] == pytest.approx(3000.0)
    assert s["loss_pool"] == pytest.approx(4000.0)
    assert s["loss_to_non_disc"] == pytest.approx(3000.0)  # non-disc absorbed first
    assert s["non_disc_after"] == pytest.approx(0.0)
    assert s["loss_to_disc"] == pytest.approx(1000.0)
    assert s["disc_after"] == pytest.approx(9000.0)
    assert s["discount_amount"] == pytest.approx(4500.0)
    assert s["est_taxable"] == pytest.approx(4500.0)
    # The displayed chain must always rebuild the summary figure exactly.
    assert s["non_disc_after"] + 0.5 * s["disc_after"] == pytest.approx(s["est_taxable"])


def test_working_steps_carry_forward_chain():
    # Same 3-FY scenario as test_loss_carries_forward_across_two_fys.
    df = pd.DataFrame([
        dict(code="A", exchange="ASX", currency="AUD",
             acquired=date(2019, 1, 1), disposed=date(2021, 3, 1),
             units=1, unit_cost_local=0, unit_proceeds_local=0,
             cost_base_aud=0, proceeds_aud=0, gain_aud=-5000.0,
             held_days=999, discount_eligible=False),
        dict(code="B", exchange="ASX", currency="AUD",
             acquired=date(2021, 8, 1), disposed=date(2022, 3, 1),
             units=1, unit_cost_local=0, unit_proceeds_local=0,
             cost_base_aud=0, proceeds_aud=0, gain_aud=2000.0,
             held_days=120, discount_eligible=False),
        dict(code="C", exchange="ASX", currency="AUD",
             acquired=date(2019, 1, 1), disposed=date(2023, 3, 1),
             units=1, unit_cost_local=0, unit_proceeds_local=0,
             cost_base_aud=0, proceeds_aud=0, gain_aud=10000.0,
             held_days=999, discount_eligible=True),
    ])
    steps = fy_working_steps(summarise_by_fy(df))
    assert [s["fy"] for s in steps] == ["FY20/21", "FY21/22", "FY22/23"]
    # Each FY's carried-in loss is the previous FY's carried-forward loss.
    assert steps[0]["carried_in"] == pytest.approx(0.0)
    assert steps[1]["carried_in"] == pytest.approx(steps[0]["loss_carried_forward"])
    assert steps[2]["carried_in"] == pytest.approx(steps[1]["loss_carried_forward"])
    assert steps[1]["carried_in"] == pytest.approx(5000.0)
    assert steps[2]["carried_in"] == pytest.approx(3000.0)
    assert steps[2]["est_taxable"] == pytest.approx(3500.0)


def test_working_steps_raise_on_tampered_summary():
    # If the summary and the reconstruction ever disagree, refuse to render
    # working that contradicts the table it sits under.
    base = dict(code="A", exchange="ASX", currency="AUD",
                acquired=date(2020, 1, 1), disposed=date(2024, 3, 1),
                units=1, unit_cost_local=0.0, unit_proceeds_local=0.0,
                cost_base_aud=0.0, proceeds_aud=0.0, held_days=999)
    df = pd.DataFrame([{**base, "gain_aud": 10000.0, "discount_eligible": True}])
    fy = summarise_by_fy(df)
    fy.loc[0, "est_taxable_gain"] = 9999.0
    with pytest.raises(ValueError, match="diverged"):
        fy_working_steps(fy)


# --------------------------------------------------------------------------
# H7 — refuse to summarise with unknown FX
# --------------------------------------------------------------------------

def test_summarise_raises_on_nan_gain():
    df = pd.DataFrame([
        dict(code="A", exchange="NASDAQ", currency="USD",
             acquired=date(2020, 1, 1), disposed=date(2024, 3, 1),
             units=1, unit_cost_local=0, unit_proceeds_local=0,
             cost_base_aud=0, proceeds_aud=0, gain_aud=float("nan"),
             held_days=999, discount_eligible=True),
    ])
    with pytest.raises(ValueError, match="NaN gain_aud"):
        summarise_by_fy(df)


# --------------------------------------------------------------------------
# Reconciliation invariant
# --------------------------------------------------------------------------

def test_net_pre_discount_reconciles_to_parcel_sum(trades_csv_path, flat_fx_provider):
    from src.ingest import load_trades
    trades = load_trades(trades_csv_path)
    realised, _ = compute_realised_gains(trades, flat_fx_provider)
    fy = summarise_by_fy(realised)

    realised = realised.copy()
    from src.cgt import _fy_label
    realised["fy"] = realised["disposed"].map(_fy_label)
    per_fy_parcel_sum = realised.groupby("fy")["gain_aud"].sum()

    for _, row in fy.iterrows():
        assert row["net_gain_pre_discount"] == pytest.approx(
            per_fy_parcel_sum[row["fy"]]
        )
