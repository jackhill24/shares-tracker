"""The browser edition's entry points (src/webapp.py).

The web page shows ``errors`` to people who have never seen a traceback, so
these pin that a broken CSV gives readable messages naming spreadsheet rows,
and that a good one round-trips requirements → fetched prices → dashboard.
"""
import json
from datetime import date, timedelta

import pytest

from src import market_data, webapp

TODAY = date.today()


def _d(days_ago: int) -> str:
    return (TODAY - timedelta(days=days_ago)).isoformat()


HEADER = "code,exchange,date,type,quantity,unit_price,platform,fees\n"
# Made-up trades, deliberately not in date order: the file's row numbers must
# survive load_trades' sort.
GOOD = HEADER + (
    f"ZZZ,NASDAQ,{_d(500)},buy,20,50.00,Test Broker,\n"
    f"AAA,ASX,{_d(900)},buy,100,10.00,Test Broker,9.50\n"
    f"AAA,ASX,{_d(300)},buy,50,11.00,Test Broker,\n"
)


@pytest.fixture(autouse=True)
def restore_provider():
    yield
    market_data.set_provider("yfinance")


def _fetch(req: dict) -> dict:
    """A made-up close for every weekday, shaped as the page passes it on."""
    closes = {}
    d, end = date.fromisoformat(req["start"]), date.fromisoformat(req["end"])
    while d <= end:
        if d.weekday() < 5:
            closes[d.isoformat()] = 0.65 if req["kind"] == "fx" else 10 + (d.toordinal() % 30) / 10
        d += timedelta(days=1)
    return {**req, "closes": closes, "last": closes[max(closes)] if closes else None}


def test_good_csv_round_trips_to_a_dashboard():
    req = json.loads(webapp.requirements(GOOD, None))
    assert req["ok"] and req["trades"] == 3
    needs = req["requirements"]
    assert set(needs["holdings"]) == {"AAA.AX", "ZZZ"}
    fetched = [_fetch(r) for r in needs["requests"]]

    out = json.loads(webapp.build(
        GOOD, json.dumps({"benchmarks": [{"ticker": "VAS.AX", "label": "ASX 300"}]}),
        json.dumps({"buckets": [{"id": "core", "label": "Core", "target": 100}],
                    "holdings": {"AAA": "core"}}),
        json.dumps(fetched), "mine", "My Portfolio",
    ))
    assert out["ok"], out
    assert out["summary"]["num_positions"] == 2
    assert out["summary"]["unassigned"] == 1  # ZZZ has no bucket
    assert '"mode": "web"' in out["html"]
    assert 'var STORAGE_KEY = "mine";' in out["html"]
    assert not any("isn't cached" in w for w in out["warnings"])


def test_settings_change_what_is_fetched():
    settings = json.dumps({"benchmarks": [{"ticker": "IOZ.AX", "label": "ASX 200"}]})
    needs = json.loads(webapp.requirements(GOOD, settings))["requirements"]
    assert "IOZ.AX" in {r["symbol"] for r in needs["requests"]}
    assert "VAS.AX" not in {r["symbol"] for r in needs["requests"]}


def test_missing_column():
    out = json.loads(webapp.requirements(GOOD.replace(",platform", ""), None))
    assert not out["ok"]
    assert out["errors"] == ["The file is missing columns: platform"]


def test_us_dates_name_the_spreadsheet_rows():
    csv = HEADER + (
        "AAA,ASX,2024-01-05,buy,10,1.00,Test Broker,\n"
        "AAA,ASX,12/25/2024,buy,10,1.00,Test Broker,\n"
    )
    out = json.loads(webapp.requirements(csv, None))
    assert not out["ok"]
    assert "row 3: '12/25/2024'" in out["errors"][0]
    assert "trades.csv" not in out["errors"][0]


def test_sell_exceeding_holdings_names_its_row_in_the_file():
    # The bad sell is row 2 of the spreadsheet but the latest trade by date.
    csv = HEADER + (
        f"AAA,ASX,{_d(10)},sell,500,12.00,Test Broker,\n"
        f"AAA,ASX,{_d(100)},buy,100,10.00,Test Broker,\n"
        f"BBB,ASX,{_d(50)},buy,-5,10.00,Test Broker,\n"
    )
    out = json.loads(webapp.requirements(csv, None))
    assert not out["ok"]
    assert len(out["errors"]) == 2
    assert out["errors"][0].startswith("Row 2: sell of 500 AAA")
    assert out["errors"][1].startswith("Row 4: quantity must be positive")


def test_unreadable_numbers_name_the_row():
    csv = HEADER + f"AAA,ASX,{_d(10)},buy,ten,1.00,Test Broker,\n"
    out = json.loads(webapp.requirements(csv, None))
    assert not out["ok"]
    assert "row 2" in out["errors"][0]


def test_empty_and_header_only_files():
    assert json.loads(webapp.requirements("  \n", None))["errors"] == ["The file is empty."]
    out = json.loads(webapp.requirements(HEADER, None))
    assert out["errors"] == ["The file has a header row but no trades."]


def test_nothing_held():
    csv = HEADER + (
        f"AAA,ASX,{_d(100)},buy,10,1.00,Test Broker,\n"
        f"AAA,ASX,{_d(10)},sell,10,2.00,Test Broker,\n"
    )
    out = json.loads(webapp.requirements(csv, None))
    assert out == {"ok": False, "errors": [webapp.NOTHING_HELD], "warnings": []}


def test_no_saved_settings_means_defaults():
    # The page sends JSON null until Settings has been saved.
    for settings in (None, "null", "{}"):
        needs = json.loads(webapp.requirements(GOOD, settings))["requirements"]
        assert {"VAS.AX", "IVV.AX"} <= {r["symbol"] for r in needs["requests"]}


def test_bad_settings_are_a_user_error():
    out = json.loads(webapp.requirements(GOOD, json.dumps({"benchmarks": [{"label": "x"}]})))
    assert not out["ok"] and "Settings" in out["errors"][0]


def test_parse_targets():
    out = json.loads(webapp.parse_targets(
        "buckets:\n  - id: core\n    label: Core\n    target: 100\n"
        "holdings:\n  AAA: core\n  BBB: nowhere\n"
    ))
    assert out["ok"]
    assert out["targets"]["holdings"] == {"AAA": "core"}
    assert out["warnings"]  # BBB's unknown bucket is reported, not fatal
    bad = json.loads(webapp.parse_targets("buckets: [unclosed"))
    assert not bad["ok"] and bad["errors"][0].startswith("Couldn't read that YAML file")


def test_demo_bundle():
    out = json.loads(webapp.demo())
    assert out["ok"]
    assert out["csv"].startswith("code,exchange,date")
    assert out["targets"]["buckets"]
    assert json.loads(webapp.requirements(out["csv"], None))["ok"]
