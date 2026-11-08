"""Does the deployed relay give the dashboard the same prices as yfinance?

Fills two in-memory price caches over the last 12 months — one through the
desktop tracker's own yfinance functions, one from the relay through
seed_cache (the browser edition's path) — and compares the tables the
dashboard reads: closes per ticker and AUD rates per currency. Today is left
out: a market that's open moves between the two fetches.

  python3.12 worker/parity_check.py https://shares-tracker-prices.<you>.workers.dev
"""
import json
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

# The tracker's root: worker/'s parent in the public repo, two levels up in
# the private one (public_overlay/worker).
sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents
                            if (p / "src" / "market_data.py").exists())))
from src import market_data  # noqa: E402
from src.db import init_db  # noqa: E402

PRICES = ["VAS.AX", "AAPL", "BTC-USD"]
FX = ["USD"]  # fetched as AUDUSD=X
TOLERANCE = 1e-6

try:  # python.org's macOS Python has no CA certificates of its own
    import certifi
    _TLS = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _TLS = ssl.create_default_context()


def relay(base: str, symbol: str, start: str, end: str) -> dict:
    url = f"{base.rstrip('/')}/chart?" + urllib.parse.urlencode(
        {"symbol": symbol, "start": start, "end": end})
    # The relay only answers its own site or localhost. Cloudflare turns away
    # Python's default User-Agent (error 1010), so say what this is.
    req = urllib.request.Request(url, headers={
        "Origin": "http://localhost", "User-Agent": "shares-tracker parity check"})
    with urllib.request.urlopen(req, timeout=30, context=_TLS) as r:
        return json.load(r)


def table(conn, sql: str, key: str) -> dict[str, float]:
    return dict(conn.execute(sql, (key,)).fetchall())


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    end = (date.today() - timedelta(days=1)).isoformat()
    start = (date.today() - timedelta(days=366)).isoformat()

    yf_db = init_db(":memory:")
    for t in PRICES:
        market_data.get_historical_prices(yf_db, t, start, end)
    for c in FX:
        market_data.get_historical_fx(yf_db, c, [start, end])

    fetched = [
        {"kind": "price", "key": t, "start": start, "end": end, **relay(sys.argv[1], t, start, end)}
        for t in PRICES
    ] + [
        {"kind": "fx", "key": c, "start": start, "end": end,
         **relay(sys.argv[1], market_data.fx_pair(c), start, end)}
        for c in FX
    ]
    web_db = init_db(":memory:")
    market_data.seed_cache(web_db, fetched)

    ok = True
    checks = [(t, "SELECT date, close_local FROM price_history WHERE ticker = ?") for t in PRICES]
    checks += [(c, "SELECT date, rate_to_aud FROM fx_history WHERE currency = ?") for c in FX]
    for key, sql in checks:
        a, b = table(yf_db, sql, key), table(web_db, sql, key)
        only_yf, only_relay = sorted(set(a) - set(b)), sorted(set(b) - set(a))
        diff = max((abs(a[d] - b[d]) for d in set(a) & set(b)), default=0.0)
        good = not only_yf and not only_relay and diff <= TOLERANCE and a
        ok &= bool(good)
        print(f"{'ok  ' if good else 'FAIL'} {key:8} {len(a)} days (yfinance) vs {len(b)} (relay), "
              f"largest difference {diff:.2e}"
              + (f"; only yfinance: {only_yf[:5]}" if only_yf else "")
              + (f"; only relay: {only_relay[:5]}" if only_relay else ""))
    print("\nParity: PASS" if ok else "\nParity: FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
