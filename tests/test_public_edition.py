"""What the public edition's dashboard shows (and, as importantly, doesn't).

The public edition is the copy shared with friends (scripts/export_public.py).
It renders through the same render_html with public=True and no CGT, income
or brief inputs — these tests pin the resulting section set so a change to
the template can't quietly put a private-only section back.
"""
import pandas as pd
import pytest

from src.allocation import AllocationConfig, Bucket
from src.compute import aggregate
from src.render import render_html


@pytest.fixture
def holdings() -> pd.DataFrame:
    rows = [
        ("AAA", "ASX", "AUD", "Test Broker", "Financials", 100, 1000.0, 1500.0),
        ("BBB", "NASDAQ", "USD", "Test Broker", "Technology", 10, 2000.0, 1800.0),
    ]
    df = pd.DataFrame([dict(
        code=c, exchange=e, currency=cur, platform=p, sector=s, units=u,
        cost_base_aud=cb, market_value_aud=mv, unrealised_pl_aud=mv - cb,
        unrealised_pl_pct=(mv - cb) / cb * 100, avg_cost_local=cb / u,
        current_price_local=mv / u, current_price_aud=mv / u,
    ) for c, e, cur, p, s, u, cb, mv in rows])
    df["weight_pct"] = df["market_value_aud"] / df["market_value_aud"].sum() * 100
    return df


@pytest.fixture
def allocation() -> dict:
    cfg = AllocationConfig(
        buckets=[Bucket("core", "Core", 60.0), Bucket("growth", "Growth", 40.0)],
        holdings={"AAA": "core", "BBB": "growth"},
    )
    return {"config": cfg, "fingerprint": "x", "summary": {}, "warnings": [], "parcels": {}}


@pytest.fixture
def risk() -> dict:
    corr = pd.DataFrame([[1.0, 0.3], [0.3, 1.0]], index=["AAA", "BBB"], columns=["AAA", "BBB"])
    return {
        "annualised_volatility": 0.15, "max_drawdown": -0.2, "sharpe": 0.8,
        "beta": 1.1, "benchmark_label": "ASX 300", "correlation": corr,
    }


def _render(tmp_path, holdings, allocation, risk, public):
    # The pipeline passes None for the CGT/income/brief inputs in the public edition.
    return render_html(
        holdings, aggregate(holdings), None if public else "# brief",
        analytics={"risk": risk}, allocation=allocation, public=public,
    )


def test_public_shows_core_sections(tmp_path, holdings, allocation, risk):
    html = _render(tmp_path, holdings, allocation, risk, public=True)
    for anchor in ("sec-goal", "sec-map", "sec-targets", "sec-risk"):
        assert f'id="{anchor}"' in html
    assert 'id="tg-assign"' in html  # bucket assignment stays


def test_public_hides_private_only_parts(tmp_path, holdings, allocation, risk):
    html = _render(tmp_path, holdings, allocation, risk, public=True)
    assert 'id="tg-cards"' not in html          # "Ways to get to target"
    assert "Ways to get to target" not in html
    assert 'id="corr-chart"' not in html        # correlation heatmap
    assert "exportBrief" not in html            # brief export button + script
    for anchor in ("sec-cgt", "sec-sold", "sec-income"):
        assert f'id="{anchor}"' not in html


def test_private_keeps_everything(tmp_path, holdings, allocation, risk):
    html = _render(tmp_path, holdings, allocation, risk, public=False)
    assert 'id="tg-cards"' in html
    assert 'id="corr-chart"' in html
    assert "exportBrief" in html


def test_storage_key_defaults_to_the_page_path(tmp_path, holdings, allocation, risk):
    html = _render(tmp_path, holdings, allocation, risk, public=True)
    assert "var STORAGE_KEY = location.pathname;" in html
    keyed = render_html(holdings, aggregate(holdings), None, allocation=allocation,
                        public=True, storage_key="mine")
    assert 'var STORAGE_KEY = "mine";' in keyed
    assert "location.pathname" not in keyed.split("var STORAGE_KEY")[1].split("</script>")[0]


def test_targets_web_mode(tmp_path, holdings, allocation, risk):
    local = _render(tmp_path, holdings, allocation, risk, public=True)
    assert "Save to file" in local and "Import YAML" not in local
    assert '"mode": "web"' not in local

    allocation = {**allocation, "warnings": ["AAA is assigned to unknown bucket"]}
    web = render_html(holdings, aggregate(holdings), None, allocation=allocation,
                      public=True, web=True, storage_key="demo")
    markup = web.split("var TARGETS")[0]  # the script is shared by both modes
    assert "Export YAML" in markup and "Import YAML" in markup
    assert 'id="tg-discard"' not in markup and "Save to file" not in markup
    assert '"mode": "web"' in web
    assert "AAA is assigned to unknown bucket" in web  # shown, not printed
