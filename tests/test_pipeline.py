"""The browser-drivable pipeline (src/pipeline.py) and its price cache seeding.

The browser edition fetches what market_requirements lists, loads it with
seed_cache and builds with fetching off. These tests pin that the list is
complete (a build from exactly that data never misses the cache), that
seed_cache stores what get_historical_* would, and that src/ imports
without yfinance (Pyodide has none).
"""
import io
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from src import db, market_data
from src.allocation import AllocationConfig, Bucket
from src.config import config_from_dict
from src.ingest import load_trades, validate_trades
from src.pipeline import NoOpenPositions, build_dashboard, market_requirements

ROOT = Path(__file__).resolve().parent.parent
TODAY = date.today()
MISS = "isn't cached and fetching is off"


def _d(days_ago: int) -> str:
    return (TODAY - timedelta(days=days_ago)).isoformat()


# Made-up trades: an ASX holding, a NASDAQ holding, and an LSE position that
# was closed — its ticker and currency still feed the snapshot history.
TRADES_CSV = f"""code,exchange,date,type,quantity,unit_price,platform,fees
AAA,ASX,{_d(1900)},buy,100,10.00,Test Broker,10.00
ZZZ,NASDAQ,{_d(1500)},buy,20,50.00,Test Broker,
LLL,LSE,{_d(1400)},buy,30,4.00,Test Broker,
LLL,LSE,{_d(700)},sell,30,5.00,Test Broker,
AAA,ASX,{_d(400)},buy,50,11.00,Test Broker,
"""


@pytest.fixture
def trades() -> pd.DataFrame:
    df = load_trades(io.StringIO(TRADES_CSV))
    validate_trades(df)
    return df


@pytest.fixture
def config():
    return config_from_dict({"benchmarks": [{"ticker": "BEN.AX", "label": "Bench"}]})


@pytest.fixture
def offline():
    market_data.set_provider("none")
    yield
    market_data.set_provider("yfinance")


def _fetch(req: dict) -> dict:
    """What the web page would hand seed_cache for one request: a made-up
    close for every weekday in the range (FX as the AUDXXX=X pair quotes it)."""
    closes = {}
    d, end = date.fromisoformat(req["start"]), date.fromisoformat(req["end"])
    while d <= end:
        if d.weekday() < 5:
            wobble = (d.toordinal() % 40) / 100
            closes[d.isoformat()] = (0.6 + wobble / 10) if req["kind"] == "fx" else 10 + wobble
        d += timedelta(days=1)
    return {**req, "closes": closes, "last": closes[max(closes)] if closes else None}


def _build(trades, config, fetched, **kw):
    conn = db.init_db(":memory:")
    quotes, fx = market_data.seed_cache(conn, fetched)
    cfg = AllocationConfig([Bucket("core", "Core", 100.0)], holdings={"AAA": "core"})
    return build_dashboard(
        trades, pd.DataFrame(), cfg, config, conn, quotes, fx,
        public=True, title="Test", **kw,
    )


def test_requirements_list_what_the_pipeline_reads(trades, config):
    req = market_requirements(trades, pd.DataFrame(), config, today=TODAY)
    assert req["holdings"] == {"AAA.AX": "ASX", "ZZZ": "NASDAQ"}
    assert req["currencies"] == ["USD"]
    by_key = {(r["kind"], r["key"]): r for r in req["requests"]}
    assert set(by_key) == {
        ("price", "AAA.AX"), ("price", "ZZZ"), ("price", "LLL.L"),
        ("price", "BEN.AX"), ("fx", "USD"), ("fx", "GBP"),
    }
    assert all(r["end"] == TODAY.isoformat() for r in req["requests"])
    # Everything from the first trade, which is older than the risk window.
    assert {r["start"] for r in req["requests"]} == {_d(1900)}
    assert by_key[("fx", "USD")]["symbol"] == "AUDUSD=X"
    json.dumps(req)  # handed to the page's JavaScript as is


def test_held_tickers_reach_back_over_the_risk_window(config):
    recent = load_trades(io.StringIO(
        "code,exchange,date,type,quantity,unit_price,platform,fees\n"
        f"ZZZ,NASDAQ,{_d(100)},buy,5,20.00,Test Broker,\n"
        f"YYY,NASDAQ,{_d(90)},buy,5,20.00,Test Broker,\n"
        f"YYY,NASDAQ,{_d(80)},sell,5,21.00,Test Broker,\n"
    ))
    req = market_requirements(recent, pd.DataFrame(), config, today=TODAY)
    starts = {(r["kind"], r["key"]): r["start"] for r in req["requests"]}
    risk_start = _d(365 * 3)
    assert starts[("price", "ZZZ")] == risk_start   # held
    assert starts[("fx", "USD")] == risk_start      # held currency
    assert starts[("price", "YYY")] == _d(100)      # sold: snapshots only
    assert starts[("price", "BEN.AX")] == _d(100)   # benchmark


def test_build_from_seeded_cache_never_misses(trades, config, offline, capsys):
    req = market_requirements(trades, pd.DataFrame(), config, today=TODAY)
    html, summary = _build(trades, config, [_fetch(r) for r in req["requests"]])
    assert MISS not in capsys.readouterr().out
    assert summary["num_positions"] == 2
    assert summary["data_warning"] is None
    assert summary["unassigned"] == 1  # ZZZ
    assert 'id="sec-history"' in html and 'id="sec-risk"' in html


def test_a_short_list_shows_up_as_a_miss(trades, config, offline, capsys):
    req = market_requirements(trades, pd.DataFrame(), config, today=TODAY)
    fetched = [_fetch(r) for r in req["requests"] if r["key"] != "LLL.L"]
    _build(trades, config, fetched)
    assert "price history for LLL.L" in capsys.readouterr().out



def test_young_portfolio_never_misses(config, offline, capsys):
    # Younger than the weekly snapshot window, with a currency no longer held:
    # its FX is wanted from the first trade only, not from a year ago.
    young = load_trades(io.StringIO(
        "code,exchange,date,type,quantity,unit_price,platform,fees\n"
        f"AAA,ASX,{_d(200)},buy,100,10.00,Test Broker,\n"
        f"LLL,LSE,{_d(150)},buy,30,10.00,Test Broker,\n"
        f"LLL,LSE,{_d(60)},sell,30,10.00,Test Broker,\n"
    ))
    req = market_requirements(young, pd.DataFrame(), config, today=TODAY)
    _build(young, config, [_fetch(r) for r in req["requests"]])
    assert MISS not in capsys.readouterr().out

def test_web_build_options(trades, config, offline):
    req = market_requirements(trades, pd.DataFrame(), config, today=TODAY)
    html, _ = _build(
        trades, config, [_fetch(r) for r in req["requests"]],
        storage_key="demo", web=True,
    )
    assert "var STORAGE_KEY = \"demo\";" in html
    assert "Import YAML" in html
    # No sectors passed: the Sector map view and By Sector donut drop out.
    assert 'data-view="sector"' not in html and "By Sector" not in html


def test_missing_quote_becomes_a_dashboard_warning(trades, config, offline):
    req = market_requirements(trades, pd.DataFrame(), config, today=TODAY)
    fetched = [_fetch(r) for r in req["requests"]]
    for f in fetched:
        if f["key"] == "ZZZ":
            f["last"] = None
    _, summary = _build(trades, config, fetched)
    assert "ZZZ" in summary["data_warning"]


def test_no_open_positions_raises(config, offline):
    closed = load_trades(io.StringIO(
        "code,exchange,date,type,quantity,unit_price,platform,fees\n"
        f"AAA,ASX,{_d(100)},buy,5,20.00,Test Broker,\n"
        f"AAA,ASX,{_d(50)},sell,5,21.00,Test Broker,\n"
    ))
    assert market_requirements(closed, pd.DataFrame(), config)["holdings"] == {}
    with pytest.raises(NoOpenPositions):
        _build(closed, config, [])


def test_seed_cache_stores_like_a_fetch():
    conn = db.init_db(":memory:")
    quotes, fx = market_data.seed_cache(conn, [
        {"kind": "price", "key": "AAA.AX", "start": "2024-01-01", "end": "2024-01-31",
         "closes": {"2024-01-02": 10.5, "2024-01-03": None}, "last": 11.0},
        {"kind": "fx", "key": "USD", "start": "2024-01-01", "end": "2024-01-31",
         "closes": {"2024-01-02": 0.64}, "last": 0.625},
        # An empty range (pre-listing) still counts as fetched.
        {"kind": "price", "key": "NEW.AX", "start": "2024-01-01", "end": "2024-01-31",
         "closes": {}, "last": None},
    ])
    assert quotes == {"AAA.AX": 11.0}
    assert fx == {"AUD": 1.0, "USD": 1.6}  # stored inverted: AUD per USD
    assert db.get_prices_in_range(conn, "AAA.AX", "2024-01-01", "2024-01-31") == {"2024-01-02": 10.5}
    assert db.get_fx_at_or_before(conn, "USD", "2024-01-05") == pytest.approx(1 / 0.64)
    for kind, key in (("price", "AAA.AX"), ("fx", "USD"), ("price", "NEW.AX")):
        assert db.range_missing_subranges(conn, kind, key, "2024-01-01", "2024-01-31") == []


def test_offline_provider_fetches_nothing(offline, capsys):
    conn = db.init_db(":memory:")
    assert market_data.get_historical_prices(conn, "AAA.AX", "2024-01-01", "2024-01-31") == {}
    assert pd.isna(market_data.get_historical_fx(conn, "USD", ["2024-01-05"])["2024-01-05"])
    prices = market_data.get_current_prices(["AAA.AX"])
    assert pd.isna(prices["AAA.AX"])
    assert pd.isna(market_data.get_fx_rates_to_aud({"AUD", "USD"})["USD"])
    assert MISS in capsys.readouterr().out
    # A miss isn't recorded as fetched, so it's reported again next time.
    assert db.get_fetch_ranges(conn, "price", "AAA.AX") == []


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError):
        market_data.set_provider("yahoo")


# Pyodide can't start a subprocess; yfinance isn't installed there at all, so
# every import in that test run already proves the same thing.
@pytest.mark.skipif(sys.platform == "emscripten", reason="no subprocess in Pyodide")
def test_src_imports_without_yfinance():
    code = (
        "import sys; sys.modules['yfinance'] = None\n"
        "import pkgutil, importlib, src\n"
        "for m in pkgutil.iter_modules(src.__path__):\n"
        "    importlib.import_module('src.' + m.name)\n"
        "import main\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
