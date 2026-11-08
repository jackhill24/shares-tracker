"""Tests for the pure-HTML fragment builders in src/render_tables.py.

These check structure (every expandable parent row has matching child rows),
escaping of user-supplied strings, and — most importantly — that displayed
figures reconcile with the frames they were built from.
"""
import re
from datetime import date

import pandas as pd
import pytest

from src.cgt import fy_working_steps, summarise_by_fy
from src.render_tables import (
    cgt_fy_table_html,
    cgt_tax_return_summary_html,
    disposals_by_stock_html,
    if_sold_today_table_html,
    income_fy_table_html,
    per_holding_xirr_table_html,
    performance_table_frag_html,
)


def _parcel(**overrides) -> dict:
    base = dict(
        code="VAS", exchange="ASX", currency="AUD",
        acquired=date(2020, 1, 15), disposed=date(2024, 9, 1),
        units=10.0, unit_cost_local=80.0, unit_proceeds_local=100.0,
        cost_base_aud=800.0, proceeds_aud=1000.0, gain_aud=200.0,
        held_days=1690, discount_eligible=True,
    )
    return {**base, **overrides}


def _parent_child_ids(html: str) -> tuple[set, set]:
    parents = set(re.findall(r'data-target="([\w-]+)"', html))
    children = set(re.findall(r'class="child ([\w-]+)"', html))
    return parents, children


# --------------------------------------------------------------------------
# CGT FY summary + working block
# --------------------------------------------------------------------------

def _fy_summary_and_steps():
    realised = pd.DataFrame([
        _parcel(gain_aud=3000.0, discount_eligible=False),
        _parcel(gain_aud=10000.0, discount_eligible=True),
        _parcel(gain_aud=-4000.0, discount_eligible=False),
    ])
    fy = summarise_by_fy(realised)
    return realised, fy, fy_working_steps(fy)


def test_cgt_table_every_fy_row_expands():
    _, fy, steps = _fy_summary_and_steps()
    html = cgt_fy_table_html(fy, steps, current_fy="FY24/25")
    parents, children = _parent_child_ids(html)
    assert len(parents) == len(fy)
    assert parents == children


def test_cgt_working_block_shows_exact_taxable_figure():
    # 3k non-disc + 10k disc − 4k loss → non-disc 0, disc 9k → taxable 4,500.
    _, fy, steps = _fy_summary_and_steps()
    html = cgt_fy_table_html(fy, steps, current_fy="FY24/25")
    assert "$0.00 + $4,500.00 = $4,500.00" in html
    assert "× 50% = $4,500.00" in html


def test_cgt_current_fy_row_highlighted():
    _, fy, steps = _fy_summary_and_steps()
    html = cgt_fy_table_html(fy, steps, current_fy=fy.iloc[0]["fy"])
    assert "cur-fy" in html


# --------------------------------------------------------------------------
# Disposals by stock (tax-return detail)
# --------------------------------------------------------------------------

def test_disposals_by_stock_subtotals_match_parcels():
    realised = pd.DataFrame([
        _parcel(code="VAS", gain_aud=200.0, cost_base_aud=800.0, proceeds_aud=1000.0),
        _parcel(code="VAS", gain_aud=-50.0, cost_base_aud=550.0, proceeds_aud=500.0,
                discount_eligible=False),
        _parcel(code="NDQ", gain_aud=300.0, cost_base_aud=700.0, proceeds_aud=1000.0),
    ])
    html = disposals_by_stock_html(realised)
    # Per-stock subtotal for VAS: net gain 200 − 50 = +150; cost base 1,350.
    assert "+$150.00" in html
    assert "$1,350.00" in html
    # FY grouping uses the same labels as summarise_by_fy (disposed 2024-09-01).
    assert "FY24/25" in html
    parents, children = _parent_child_ids(html)
    assert len(parents) == 2  # one per stock
    assert parents == children


def test_disposals_by_stock_splits_gross_gains_and_losses():
    # A stock with both a winning and a losing parcel must show BOTH gross
    # figures, not just the net — this is the whole point of the per-parcel view.
    realised = pd.DataFrame([
        _parcel(code="RHC", gain_aud=-859.66, cost_base_aud=2494.95, proceeds_aud=1635.29),
        _parcel(code="RHC", gain_aud=124.22, cost_base_aud=990.75, proceeds_aud=1114.97),
    ])
    html = disposals_by_stock_html(realised)
    # Gross gains column shows +124.22, gross losses shows −859.66, net −735.44.
    assert "$124.22" in html
    assert "$859.66" in html or "−$859.66" in html
    assert "−$735.44" in html
    # The FY header sums the gross columns (gross gains 124.22, losses −859.66).
    assert "gross gains $124.22" in html
    assert "gross losses −$859.66" in html


# --------------------------------------------------------------------------
# ATO tax-return summary (current-FY headline figures)
# --------------------------------------------------------------------------

def _labelled_vals(html: str) -> dict:
    import re
    pairs = re.findall(
        r'taxlabel">([^<]+)</span><span class="taxval">(?:<b>)?([^<]+)', html
    )
    return {lbl: val for lbl, val in pairs}


def test_tax_summary_reports_gross_gains_and_net_gain():
    # 3k non-disc gain + 10k disc gain − 4k loss, all one FY.
    # Gross gains 13k, gross losses 4k (all applied), net gain 4,500.
    realised = pd.DataFrame([
        _parcel(gain_aud=3000.0, discount_eligible=False),
        _parcel(gain_aud=10000.0, discount_eligible=True),
        _parcel(gain_aud=-4000.0, discount_eligible=False),
    ])
    fy = summarise_by_fy(realised)
    html = cgt_tax_return_summary_html(fy, current_fy=fy.iloc[0]["fy"])
    vals = _labelled_vals(html)
    assert vals["Total current year capital gains"] == "$13,000.00"
    assert vals["Total current year capital losses"] == "$4,000.00"
    assert vals["Total current year capital losses applied"] == "$4,000.00"
    assert vals["Net capital gain"] == "$4,500.00"


def test_tax_summary_current_year_loss_partly_carried():
    # 1k gain, 3k current-year loss → apply 1k, carry 2k, net 0.
    realised = pd.DataFrame([
        _parcel(gain_aud=1000.0, discount_eligible=False, held_days=100),
        _parcel(gain_aud=-3000.0, discount_eligible=False, held_days=90),
    ])
    fy = summarise_by_fy(realised)
    html = cgt_tax_return_summary_html(fy, current_fy=fy.iloc[0]["fy"])
    vals = _labelled_vals(html)
    assert vals["Total current year capital losses applied"] == "$1,000.00"
    assert vals["Net capital gain"] == "$0.00"
    assert vals["Net capital losses carried forward to later years"] == "$2,000.00"


def test_tax_summary_prior_year_losses_applied_shown_separately():
    # FY1: 5k loss only. FY2: 2k gain absorbs 2k carried-in loss.
    realised = pd.DataFrame([
        _parcel(disposed=date(2021, 3, 1), gain_aud=-5000.0, discount_eligible=False),
        _parcel(disposed=date(2022, 3, 1), gain_aud=2000.0, discount_eligible=False,
                held_days=120),
    ])
    fy = summarise_by_fy(realised)
    html = cgt_tax_return_summary_html(fy, current_fy="FY21/22")
    vals = _labelled_vals(html)
    # This FY had no own losses; the 2k applied is entirely prior-year.
    assert vals["Total current year capital losses applied"] == "$0.00"
    assert vals["Net capital losses from earlier years applied"] == "$2,000.00"
    assert vals["Net capital gain"] == "$0.00"


def test_tax_summary_absent_when_no_current_fy_disposals():
    realised = pd.DataFrame([_parcel(disposed=date(2021, 3, 1), gain_aud=100.0)])
    fy = summarise_by_fy(realised)
    assert cgt_tax_return_summary_html(fy, current_fy="FY99/00") == ""
    assert cgt_tax_return_summary_html(None, current_fy="FY24/25") == ""
    assert cgt_tax_return_summary_html(pd.DataFrame(), current_fy="FY24/25") == ""


def test_disposals_by_stock_escapes_code():
    realised = pd.DataFrame([_parcel(code="A&B<X>")])
    html = disposals_by_stock_html(realised)
    assert "A&amp;B&lt;X&gt;" in html
    assert "A&B<X>" not in html


def test_disposals_by_stock_empty_is_blank():
    assert disposals_by_stock_html(None) == ""
    assert disposals_by_stock_html(pd.DataFrame()) == ""


# --------------------------------------------------------------------------
# Performance windows + per-holding XIRR
# --------------------------------------------------------------------------

def _performance() -> dict:
    detail = {
        "start_date": date(2024, 1, 1), "end_date": date(2025, 1, 1),
        "days": 366, "total_return": 0.124,
        "start_value": 100000.0, "end_value": 142400.0,
        "net_contributions": 30000.0,
    }
    return {
        "bench_labels": {"VAS.AX": "ASX 300"},
        "per_holding_xirr": {"VAS": 0.11, "GONE": None},
        "windows": [{
            "window": "12M", "days": 366,
            "portfolio_twr": 0.121, "portfolio_total": 0.124,
            "portfolio_detail": detail,
            "benchmarks": [{
                "ticker": "VAS.AX", "label": "ASX 300",
                "annualised": 0.09, "total_return": 0.092, "excess": 0.031,
                "detail": {**detail, "total_return": 0.092, "end_value": 139200.0},
            }],
        }],
    }


def test_performance_rows_expand_and_substitute_annualisation():
    html = performance_table_frag_html(_performance())
    parents, children = _parent_child_ids(html)
    assert len(parents) == 1
    assert parents == children
    # Annualisation formula substituted with the window's actual numbers.
    assert "(1 + 0.1240)^(365 / 366) − 1" in html
    # Excess arithmetic spelled out.
    assert "= +3.1% p.a." in html


def test_per_holding_xirr_none_renders_dash():
    perf = _performance()
    by_code = pd.DataFrame({"code": ["VAS"], "market_value_aud": [50000.0]})
    html = per_holding_xirr_table_html(perf["per_holding_xirr"], by_code)
    assert "+11.0%" in html
    assert "—" in html  # the sold-out holding with no XIRR
    assert "GONE" in html


# --------------------------------------------------------------------------
# If sold today
# --------------------------------------------------------------------------

def test_if_sold_today_shows_saving_and_parcels():
    parcels = [{
        "acquired": date(2020, 1, 1), "units": 10.0,
        "cost_base_aud": 800.0, "proceeds_aud": 1000.0, "gain_aud": 200.0,
        "discount_eligible": True, "taxable_gain_aud": 100.0,
    }]
    disposals = {"VAS:ASX": {
        "fifo": {"parcels": parcels, "total_gain_aud": 200.0,
                 "total_taxable_gain_aud": 100.0, "units_disposed": 10.0},
        "min_tax": {"parcels": parcels, "total_gain_aud": 200.0,
                    "total_taxable_gain_aud": 60.0, "units_disposed": 10.0},
    }}
    html = if_sold_today_table_html(disposals)
    parents, children = _parent_child_ids(html)
    assert parents == children and len(parents) == 1
    assert "$40.00" in html  # FIFO 100 − min-tax 60 saving
    assert if_sold_today_table_html({}) == ""


# --------------------------------------------------------------------------
# Dividend income
# --------------------------------------------------------------------------

def test_income_table_franking_columns_conditional():
    with_franking = pd.DataFrame({
        "fy": ["FY23/24"], "dividend_income_aud": [1000.0],
        "franking_credits_aud": [300.0], "grossed_up_aud": [1300.0],
    })
    html = income_fy_table_html(with_franking)
    assert "Franking Credits" in html and "$1,300" in html

    without = with_franking.assign(franking_credits_aud=0.0)
    html2 = income_fy_table_html(without)
    assert "Franking Credits" not in html2
    assert "$1,000" in html2
