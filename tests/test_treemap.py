"""Tests for the holdings-map treemap views in src/render.py.

Checks each view's hierarchy (Portfolio → [group →] ticker), that group tiles
carry the true group P/L % rather than an average of their members, and that
holdings without a bucket / sector still appear on the map.
"""
import math

import pandas as pd
import pytest

from src.allocation import AllocationConfig, Bucket
from src.compute import aggregate_by_code
from src.render import treemap, treemap_groupings


@pytest.fixture
def holdings() -> pd.DataFrame:
    rows = [
        # code, exchange, platform, sector, cost, value
        ("VAS", "ASX", "CommSec", "ETFs / Funds", 9000.0, 11000.0),
        ("MSFT", "NASDAQ", "Stake", "Technology", 4000.0, 6000.0),
        ("ZIP", "ASX", "CommSec", "Technology", 1000.0, 500.0),
        ("ETH", "CRYPTO-AUD", "Swyftx", None, 1000.0, 1500.0),
    ]
    return pd.DataFrame([
        dict(code=c, exchange=e, platform=p, sector=s, currency="AUD", units=1.0,
             cost_base_aud=cb, market_value_aud=mv, unrealised_pl_aud=mv - cb,
             avg_cost_local=cb, current_price_local=mv)
        for c, e, p, s, cb, mv in rows
    ])


@pytest.fixture
def allocation() -> dict:
    cfg = AllocationConfig(
        buckets=[Bucket("core", "Core", 60), Bucket("spec", "Speculative", 40)],
        holdings={"VAS": "core", "ZIP": "spec", "ETH": "spec"},  # MSFT unassigned
    )
    return {"config": cfg}


def _trace(fig, name):
    return next(t for t in fig.data if t.name == name)


def test_groupings_cover_every_code(holdings, allocation):
    by_code = aggregate_by_code(holdings)
    g = treemap_groupings(holdings, by_code, allocation)
    assert list(g) == ["total", "allocation", "sector"]
    assert g["total"] is None
    assert g["allocation"] == {
        "VAS": "Core", "MSFT": "Unassigned", "ZIP": "Speculative", "ETH": "Speculative",
    }
    assert g["sector"]["ETH"] == "Unclassified"
    assert g["sector"]["ZIP"] == "Technology"


def test_views_dropped_without_data(holdings):
    by_code = aggregate_by_code(holdings)
    g = treemap_groupings(holdings.drop(columns="sector"), by_code, None)
    assert list(g) == ["total"]
    assert [t.name for t in treemap(by_code, g).data] == ["total"]


def test_only_total_view_starts_visible(holdings, allocation):
    by_code = aggregate_by_code(holdings)
    fig = treemap(by_code, treemap_groupings(holdings, by_code, allocation))
    assert {t.name: t.visible for t in fig.data} == {
        "total": True, "allocation": False, "sector": False,
    }


def test_grouped_hierarchy_and_group_pl(holdings, allocation):
    by_code = aggregate_by_code(holdings)
    fig = treemap(by_code, treemap_groupings(holdings, by_code, allocation))
    t = _trace(fig, "sector")
    nodes = dict(zip(t.ids, zip(t.parents, t.values, t.marker.colors)))

    assert nodes["sector"][0] == ""
    assert nodes["sector/Technology"][0] == "sector"
    assert nodes["sector/Technology/MSFT"][0] == "sector/Technology"
    assert nodes["sector/Unclassified/ETH"][0] == "sector/Unclassified"

    # Leaves carry the value; parents are sized by Plotly from their children.
    assert nodes["sector/Technology/MSFT"][1] == 6000.0
    assert nodes["sector/Technology"][1] == 0

    # Technology: P/L (2000 − 500) ÷ cost (4000 + 1000) = +30%, not the
    # members' average of (+50% − 50%) / 2 = 0%.
    assert nodes["sector/Technology"][2] == pytest.approx(30.0)
    # Whole portfolio: (19000 − 15000) ÷ 15000.
    assert nodes["sector"][2] == pytest.approx(4000 / 15000 * 100, abs=0.01)


def test_total_view_is_flat(holdings):
    by_code = aggregate_by_code(holdings)
    t = _trace(treemap(by_code), "total")
    assert set(t.parents) == {"", "total"}
    assert sum(t.values) == pytest.approx(19000.0)


def test_labels_escaped(holdings):
    by_code = aggregate_by_code(holdings)
    cfg = AllocationConfig(buckets=[Bucket("x", "R&D <b>", 100)],
                           holdings={c: "x" for c in by_code["code"]})
    fig = treemap(by_code, treemap_groupings(holdings, by_code, {"config": cfg}))
    t = _trace(fig, "allocation")
    assert "R&amp;D &lt;b&gt;" in t.labels
    assert not any("<b>R&D" in h for h in t.hovertext)


def test_nan_pl_pct_left_uncoloured(holdings):
    holdings.loc[holdings["code"] == "ETH", "cost_base_aud"] = 0.0
    by_code = aggregate_by_code(holdings)
    t = _trace(treemap(by_code), "total")
    eth = list(t.ids).index("total/ETH")
    assert t.marker.colors[eth] is None
    assert all(c is None or math.isfinite(c) for c in t.marker.colors)
