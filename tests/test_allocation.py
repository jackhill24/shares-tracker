"""Target allocation (src/allocation.py): config, summary, new-money maths."""
import os
from datetime import date

import pandas as pd
import pytest

from src.allocation import (
    AllocationConfig,
    Bucket,
    allocation_from_dict,
    bucket_summary,
    contribution_to_band,
    contribution_to_target,
    fingerprint,
    import_downloaded,
    load_allocation,
    parcels_payload,
    parse_allocation,
)
from src.cgt import simulate_disposal

# Exactly what the dashboard's "Save to file" button emits (see render.py's
# toYaml): every key quoted, buckets as a block list.
DASHBOARD_YAML = """\
# Shares tracker — target allocation.
buckets:
  - id: "core"
    label: "Core"
    target: 60
  - id: "spec"
    label: "Speculative \\"punts\\""
    target: 40
tolerance:
  absolute_pp: 5
  relative_pct: 25
holdings:
  "AAA": "core"
  "NO": "spec"
"""


def _by_code(values: dict[str, float]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"code": c, "market_value_aud": v} for c, v in values.items()]
    )


def _cfg(targets: dict[str, float], holdings: dict[str, str], abs_pp=5.0, rel=25.0):
    return AllocationConfig(
        [Bucket(b, b.title(), t) for b, t in targets.items()], abs_pp, rel, holdings
    )


def test_missing_file_uses_default_buckets(tmp_path):
    cfg, warnings = load_allocation(tmp_path / "nope.yaml")
    assert [b.id for b in cfg.buckets] == [
        "core", "quality_growth", "high_conviction", "speculative"
    ]
    assert sum(b.target for b in cfg.buckets) == 100
    assert cfg.holdings == {}
    assert warnings == []


def test_ticker_keys_are_read_verbatim(tmp_path):
    # Unquoted NO would be False and 0700 would be octal 448 under safe_load.
    p = tmp_path / "a.yaml"
    p.write_text(
        "buckets:\n  - {id: core, target: 100}\n"
        "holdings:\n  NO: core\n  0700: core\n  'ON': core\n"
    )
    cfg, warnings = load_allocation(p)
    assert cfg.holdings == {"NO": "core", "0700": "core", "ON": "core"}
    assert warnings == []


def test_unknown_bucket_and_bad_sum_warn(tmp_path):
    p = tmp_path / "a.yaml"
    p.write_text(
        "buckets:\n  - {id: core, target: 70}\n  - {id: spec, target: 20}\n"
        "holdings:\n  AAA: core\n  BBB: moonshots\n"
    )
    cfg, warnings = load_allocation(p)
    assert cfg.holdings == {"AAA": "core"}
    assert any("sum to 90%" in w for w in warnings)
    assert any("BBB" in w and "moonshots" in w for w in warnings)


def test_duplicate_bucket_id_is_an_error(tmp_path):
    p = tmp_path / "a.yaml"
    p.write_text("buckets:\n  - {id: core, target: 50}\n  - {id: core, target: 50}\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_allocation(p)


def test_dashboard_yaml_round_trips(tmp_path):
    p = tmp_path / "a.yaml"
    p.write_text(DASHBOARD_YAML)
    cfg, warnings = load_allocation(p)
    assert warnings == []
    assert cfg.buckets[1].label == 'Speculative "punts"'
    assert cfg.holdings == {"AAA": "core", "NO": "spec"}
    # Same config built by hand (int targets, other key order) → same hash.
    assert fingerprint(cfg) == fingerprint(AllocationConfig(
        [Bucket("core", "Core", 60), Bucket("spec", 'Speculative "punts"', 40)],
        5, 25, {"NO": "spec", "AAA": "core"},
    ))



def test_parse_allocation_matches_the_file_loader(tmp_path):
    # The browser edition parses an imported YAML's text without a file.
    p = tmp_path / "a.yaml"
    p.write_text(DASHBOARD_YAML)
    assert parse_allocation(DASHBOARD_YAML) == load_allocation(p)
    _, warnings = parse_allocation(
        "buckets:\n  - {id: core, target: 100}\nholdings:\n  AAA: gone\n",
        "upload.yaml",
    )
    assert warnings and "upload.yaml" in warnings[0]
    with pytest.raises(ValueError, match="mapping"):
        parse_allocation("- just\n- a list\n")


def test_allocation_from_dict_round_trips_to_dict():
    # The browser stores to_dict() as JSON and passes it back on each build.
    cfg, _ = parse_allocation(DASHBOARD_YAML)
    again, warnings = allocation_from_dict(cfg.to_dict())
    assert again == cfg and warnings == []
    cfg2, warnings = allocation_from_dict({
        "buckets": [{"id": "core", "target": 100}], "holdings": {"0700": "core", "X": "nope"},
    })
    assert cfg2.holdings == {"0700": "core"}
    assert any("X" in w for w in warnings)

def test_summary_excludes_unassigned_and_applies_band():
    cfg = _cfg({"core": 50, "growth": 30, "spec": 20},
               {"VAS": "core", "CSL": "growth", "ZIP": "spec"})
    s = bucket_summary(_by_code({"VAS": 400, "CSL": 300, "ZIP": 300, "NEW": 250}), cfg)

    assert s["assigned_total"] == 1000
    assert s["portfolio_total"] == 1250
    assert s["unassigned"]["code"].tolist() == ["NEW"]
    assert s["unassigned_value"] == 250

    b = s["buckets"].set_index("id")
    assert b.loc["core", "actual_pct"] == pytest.approx(40)
    assert b.loc["core", "drift_aud"] == pytest.approx(-100)
    # Band = min(5pp, 25% of target): core ±5, spec ±5, growth ±5.
    assert b.loc["spec", "band_pp"] == pytest.approx(5)
    assert b.loc["spec", "upper_pct"] == pytest.approx(25)
    assert b.loc["core", "status"] == "under"   # 40 < 45
    assert b.loc["growth", "status"] == "on"    # 30 on target
    assert b.loc["spec", "status"] == "over"    # 30 > 25


def test_relative_band_tightens_small_targets():
    cfg = _cfg({"core": 92, "spec": 8}, {"A": "core", "B": "spec"})
    b = bucket_summary(_by_code({"A": 900, "B": 100}), cfg)["buckets"].set_index("id")
    assert b.loc["spec", "band_pp"] == pytest.approx(2)  # 25% of 8
    assert b.loc["spec", "status"] == "on"                # 10 within 6–10


def test_contribution_to_target_hand_worked():
    # core 400 (50%), spec 300 (20%) of 700 → spec is the binding overweight:
    # it is 20% of a total of 1500, so 800 of new money, all into core.
    cfg = _cfg({"core": 80, "spec": 20}, {"A": "core", "B": "spec"})
    s = bucket_summary(_by_code({"A": 400, "B": 300}), cfg)
    assert contribution_to_target(s) == pytest.approx(800)
    # Band for spec = min(5, 5) → upper 25%: total 1200, i.e. 500 more.
    assert contribution_to_band(s) == pytest.approx(500)


def test_contribution_zero_when_already_on_target():
    cfg = _cfg({"core": 50, "spec": 50}, {"A": "core", "B": "spec"})
    s = bucket_summary(_by_code({"A": 500, "B": 500}), cfg)
    assert contribution_to_target(s) == pytest.approx(0)
    assert contribution_to_band(s) == pytest.approx(0)


def test_held_bucket_with_zero_target_needs_selling():
    cfg = _cfg({"core": 100, "spec": 0}, {"A": "core", "B": "spec"})
    s = bucket_summary(_by_code({"A": 500, "B": 100}), cfg)
    assert contribution_to_target(s) is None
    assert contribution_to_band(s) is None


def test_parcels_payload_matches_simulate_disposal():
    today = date(2026, 7, 15)
    parcels = pd.DataFrame([
        {"code": "AAA", "exchange": "ASX", "currency": "AUD",
         "acquired": date(2023, 1, 1), "units": 100.0,
         "unit_cost_local": 1.0, "fx_at_buy": 1.0},
        {"code": "MSFT", "exchange": "NASDAQ", "currency": "USD",
         "acquired": date(2026, 6, 1), "units": 5.0,
         "unit_cost_local": 400.0, "fx_at_buy": 1.4},
    ])
    enriched = pd.DataFrame([
        {"code": "AAA", "exchange": "ASX", "current_price_local": 10.0, "fx_to_aud": 1.0},
        {"code": "MSFT", "exchange": "NASDAQ", "current_price_local": 420.0, "fx_to_aud": 1.5},
    ])
    out = parcels_payload(parcels, enriched, today)

    for code, exchange, price, fx in (("AAA", "ASX", 10.0, 1.0), ("MSFT", "NASDAQ", 420.0, 1.5)):
        p = out[code][0]
        sim = simulate_disposal(parcels, code, exchange, 1.0, price, fx, disposed=today)["fifo"]
        assert p["gain_per_unit"] == pytest.approx(sim["total_gain_aud"])
        assert p["value_per_unit"] == pytest.approx(price * fx)
    assert out["AAA"][0]["discount_eligible"] is True
    assert out["MSFT"][0]["discount_eligible"] is False


def test_import_downloaded_adopts_newer_download_once(tmp_path):
    data = tmp_path / "data" / "allocation.yaml"
    downloads = tmp_path / "dl"
    downloads.mkdir()
    data.parent.mkdir()
    data.write_text("buckets:\n  - {id: core, target: 100}\n")
    os.utime(data, (1_000, 1_000))

    dl = downloads / "shares-tracker-allocation (1).yaml"
    dl.write_text(DASHBOARD_YAML)
    os.utime(dl, (2_000, 2_000))

    assert import_downloaded(data, downloads, "shares-tracker-allocation*.yaml") == dl
    assert data.read_text() == DASHBOARD_YAML
    assert (data.parent / "allocation.yaml.bak").exists()
    # Same download again → nothing to do.
    assert import_downloaded(data, downloads, "shares-tracker-allocation*.yaml") is None


def test_waterfill_fills_most_underweight_first():
    from src.allocation import waterfill
    # Ratios V/t: a = 100/0.5 = 200, b = 100/0.25 = 400, c = 300/0.25 = 1200.
    buys = waterfill([100, 100, 300], [0.5, 0.25, 0.25], 100)
    assert buys == pytest.approx([100, 0, 0])         # only a is lifted (to 200/0.5 = 400)
    buys = waterfill([100, 100, 300], [0.5, 0.25, 0.25], 250)
    # a alone would overshoot b's ratio, so both rise to level (250+200)/0.75
    # = 600: a → 300, b → 150.
    assert buys == pytest.approx([200, 50, 0])
    assert sum(waterfill([100, 100, 300], [0.5, 0.25, 0.25], 700)) == pytest.approx(700)
    # Enough money to reach target exactly: every bucket at t × 1200.
    buys = waterfill([100, 100, 300], [0.5, 0.25, 0.25], 700)
    assert [v + x for v, x in zip([100, 100, 300], buys)] == pytest.approx([600, 300, 300])
