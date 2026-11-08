from datetime import date, timedelta

from src import db

EXCHANGE_MAP = {
    "ASX":    {"currency": "AUD", "suffix": ".AX"},
    "NASDAQ": {"currency": "USD", "suffix": ""},
    "NYSE":   {"currency": "USD", "suffix": ""},
    "LSE":    {"currency": "GBP", "suffix": ".L"},
    "HKEX":   {"currency": "HKD", "suffix": ".HK"},
    "TSX":    {"currency": "CAD", "suffix": ".TO"},
    "CRYPTO": {"currency": "USD", "suffix": "-USD"},
    "CRYPTO-AUD": {"currency": "AUD", "suffix": "-AUD"},
    "XMIL":   {"currency": "EUR", "suffix": ".MI"},
}

# yfinance FX pair for converting foreign currency → AUD
# "AUDUSD=X" gives USD per 1 AUD — we invert to get AUD per 1 unit foreign
# (see _rate_to_aud, the one place that conversion happens).
_FX_PAIRS = {
    "USD": "AUDUSD=X",
    "GBP": "AUDGBP=X",
    "HKD": "AUDHKD=X",
    "CAD": "AUDCAD=X",
    "EUR": "AUDEUR=X",
}

# What happens on a cache miss. "yfinance" fetches the gap (the command line).
# "none" fetches nothing: the browser edition can't run yfinance, so it fetches
# everything market_requirements lists up front and seeds the cache with it
# (seed_cache) — a miss there means the list is short, so it warns instead.
_PROVIDER = "yfinance"
_PROVIDERS = ("yfinance", "none")


def set_provider(name: str) -> None:
    global _PROVIDER
    if name not in _PROVIDERS:
        raise ValueError(f"Unknown market data provider {name!r}; use one of {_PROVIDERS}.")
    _PROVIDER = name


def _yf():
    """yfinance, imported on first use so src/ imports without it (Pyodide)."""
    import yfinance
    return yfinance


def _offline(what: str) -> bool:
    """True (after warning) when fetching is off and ``what`` isn't cached."""
    if _PROVIDER == "none":
        print(f"  WARNING: {what} isn't cached and fetching is off — skipping.")
        return True
    return False


def fx_pair(currency: str) -> str | None:
    """The Yahoo symbol quoting ``currency`` against AUD, e.g. USD → AUDUSD=X."""
    return _FX_PAIRS.get(currency)


def _rate_to_aud(pair_close) -> float | None:
    """AUDXXX=X quotes XXX per 1 AUD; the cache stores AUD per 1 XXX."""
    return 1.0 / pair_close if pair_close else None


def to_yf_ticker(code: str, exchange: str) -> str:
    suffix = EXCHANGE_MAP[exchange]["suffix"]
    return f"{code}{suffix}"


def get_current_prices(tickers: list[str]) -> dict[str, float]:
    if not tickers:
        return {}

    if _offline(f"live prices for {', '.join(tickers)}"):
        return {t: float("nan") for t in tickers}

    prices = {}
    batch = _yf().Tickers(" ".join(tickers))

    for ticker in tickers:
        obj = batch.tickers.get(ticker)
        price = None
        if obj is not None:
            try:
                price = obj.fast_info["lastPrice"]
            except Exception:
                pass
            if price is None:
                try:
                    price = obj.info.get("regularMarketPrice")
                except Exception:
                    pass
        if price is None:
            print(f"  WARNING: could not fetch price for {ticker}")
            price = float("nan")
        prices[ticker] = price

    return prices


def get_fx_rates_to_aud(currencies: set[str]) -> dict[str, float]:
    """Return a mapping of currency -> AUD value of 1 unit of that currency."""
    rates = {"AUD": 1.0}
    foreign = [c for c in currencies if c != "AUD"]

    if not foreign:
        return rates

    pairs = [_FX_PAIRS[c] for c in foreign if c in _FX_PAIRS]
    unknown = [c for c in foreign if c not in _FX_PAIRS]
    if unknown:
        print(f"  WARNING: no FX pair configured for currencies: {unknown}")

    if pairs and _offline(f"live FX rates for {', '.join(sorted(foreign))}"):
        return rates | {c: float("nan") for c in foreign}
    if pairs:
        batch = _yf().Tickers(" ".join(pairs))
        for currency in foreign:
            pair = _FX_PAIRS.get(currency)
            if pair is None:
                rates[currency] = float("nan")
                continue
            obj = batch.tickers.get(pair)
            aud_per_foreign = None
            if obj is not None:
                try:
                    aud_per_foreign = _rate_to_aud(obj.fast_info["lastPrice"])
                except Exception:
                    pass
                if aud_per_foreign is None:
                    try:
                        aud_per_foreign = _rate_to_aud(obj.info.get("regularMarketPrice"))
                    except Exception:
                        pass
            if aud_per_foreign is None:
                print(f"  WARNING: could not fetch FX rate for {currency}/AUD")
                aud_per_foreign = float("nan")
            rates[currency] = aud_per_foreign

    return rates


def get_sectors(conn, ticker_exchanges: dict[str, str]) -> dict[str, str]:
    """Return {yf_ticker: sector} for each holding, cached in ticker_info.

    Crypto exchanges classify locally as "Crypto" (yfinance has no sector for
    them); ETFs/funds fold into a single "ETFs / Funds" bucket since their
    look-through sectors aren't available. Sectors never change in practice so
    a cached value is trusted forever; a failed fetch falls back to
    "Unclassified" without caching, so it retries next run. ("Unclassified",
    not "Other" — the allocation donut folds its tail into an "Other" slice,
    and a sector literally named Other would collide with it.)
    """
    sectors: dict[str, str] = {}
    for ticker, exchange in sorted(ticker_exchanges.items()):
        if exchange.startswith("CRYPTO"):
            sectors[ticker] = "Crypto"
            continue

        cached = db.get_ticker_sector(conn, ticker)
        if cached is not None:
            sectors[ticker] = cached
            continue
        if _offline(f"the sector for {ticker}"):
            sectors[ticker] = "Unclassified"
            continue

        try:
            info = _yf().Ticker(ticker).info or {}
            quote_type = str(info.get("quoteType") or "").upper()
            if quote_type in ("ETF", "MUTUALFUND"):
                sector = "ETFs / Funds"
            else:
                sector = str(info.get("sector") or "Unclassified")
            db.upsert_ticker_sector(conn, ticker, sector)
        except Exception as e:
            print(f"  WARNING: sector lookup failed for {ticker}: {e}")
            sector = "Unclassified"
        sectors[ticker] = sector

    return sectors


def _exclusive_end(end: str) -> str:
    """yfinance history() treats `end` as exclusive; add a day to include it."""
    return (date.fromisoformat(end) + timedelta(days=1)).isoformat()


def get_historical_prices(conn, ticker: str, start: str, end: str) -> dict[str, float]:
    """Return {date_str: close_local} for [start, end], caching to price_history.

    Coverage is tracked honestly via fetch_ranges (M3): only the sub-ranges of
    [start, end] not already fetched trigger a network call, so a market
    holiday with no rows no longer looks like a permanent gap. Prices are
    UNADJUSTED (auto_adjust=False, raw Close) — dividends are cash not price,
    and units are the raw CSV units (H4).
    """
    for gap_start, gap_end in db.range_missing_subranges(conn, "price", ticker, start, end):
        if _offline(f"price history for {ticker} {gap_start}..{gap_end}"):
            continue
        try:
            hist = _yf().Ticker(ticker).history(
                start=gap_start, end=_exclusive_end(gap_end),
                interval="1d", auto_adjust=False,
            )
        except Exception as e:
            print(f"  WARNING: historical price fetch failed for {ticker}: {e}")
            continue

        if hist is not None and not hist.empty:
            hist.index = hist.index.strftime("%Y-%m-%d")
            price_dict = hist["Close"].dropna().to_dict()
            db.upsert_prices(conn, ticker, price_dict)
        # Record the range as fetched even if empty (holiday/pre-listing) so we
        # don't re-request it every run.
        db.add_fetch_range(conn, "price", ticker, gap_start, gap_end)

    return db.get_prices_in_range(conn, ticker, start, end)


def get_dividends(conn, ticker: str, start: str, end: str) -> dict[str, float]:
    """Return {ex_date_str: dividend_per_share_local} for [start, end], cached.

    Coverage is tracked via fetch_ranges (kind='dividend') exactly like prices:
    only never-fetched sub-ranges hit the network, and a range with no dividends
    (a non-payer, or a quiet quarter) is recorded so it isn't re-requested. The
    amounts are per-share in the ticker's local currency (Phase 5).
    """
    for gap_start, gap_end in db.range_missing_subranges(conn, "dividend", ticker, start, end):
        if _offline(f"dividends for {ticker} {gap_start}..{gap_end}"):
            continue
        try:
            divs = _yf().Ticker(ticker).dividends
        except Exception as e:
            print(f"  WARNING: dividend fetch failed for {ticker}: {e}")
            continue

        if divs is not None and not divs.empty:
            s = divs.copy()
            s.index = s.index.strftime("%Y-%m-%d")
            # Keep only the ex-dates inside the gap being fetched.
            in_gap = {
                d: float(v) for d, v in s.to_dict().items()
                if gap_start <= d <= gap_end and v
            }
            db.upsert_dividends(conn, ticker, in_gap)
        db.add_fetch_range(conn, "dividend", ticker, gap_start, gap_end)

    return db.get_dividends_in_range(conn, ticker, start, end)


def _rates_to_aud(pair_closes: dict[str, float]) -> dict[str, float]:
    return {d: _rate_to_aud(v) for d, v in pair_closes.items() if v}


def get_historical_fx(conn, currency: str, dates: list[str]) -> dict[str, float]:
    """Return {date_str: rate_to_aud} for each requested date.

    AUD short-circuits to 1.0. Missing dates are fetched in one range call,
    inverted (AUDXXX=X gives XXX per AUD), cached, then resolved via
    forward-fill so weekends/holidays map to the nearest prior trading day.
    """
    if currency == "AUD":
        return {d: 1.0 for d in dates}

    if not dates:
        return {}

    # Coverage tracked by fetch_ranges over the full requested span (M3): only
    # sub-ranges never fetched trigger a network call, rather than re-requesting
    # whenever a weekend/holiday date isn't a literal cached row.
    span_start, span_end = min(dates), max(dates)
    pair = _FX_PAIRS.get(currency)
    if pair is None:
        print(f"  WARNING: no FX pair configured for {currency}")
    else:
        for gap_start, gap_end in db.range_missing_subranges(
            conn, "fx", currency, span_start, span_end
        ):
            if _offline(f"FX history for {currency} {gap_start}..{gap_end}"):
                continue
            try:
                hist = _yf().Ticker(pair).history(
                    start=gap_start, end=_exclusive_end(gap_end), interval="1d"
                )
            except Exception as e:
                print(f"  WARNING: historical FX fetch failed for {currency}: {e}")
                continue

            if hist is not None and not hist.empty:
                hist.index = hist.index.strftime("%Y-%m-%d")
                closes = hist["Close"].dropna()
                db.upsert_fx(conn, currency, _rates_to_aud(closes.to_dict()))
            db.add_fetch_range(conn, "fx", currency, gap_start, gap_end)

    result = {}
    for d in dates:
        rate = db.get_fx_at_or_before(conn, currency, d)
        if rate is None:
            print(f"  WARNING: no FX rate for {currency} at or before {d}, using NaN")
            rate = float("nan")
        result[d] = rate
    return result


def seed_cache(conn, fetched: list[dict]) -> tuple[dict[str, float], dict[str, float]]:
    """Load prices fetched elsewhere into the cache → (quotes, fx).

    The browser edition fetches each request from market_requirements itself
    and hands the results here, so the pipeline then finds everything cached
    (with the provider set to "none"). Each item is a request plus what came
    back: ``{"kind": "price"|"fx", "key": ticker or currency, "start", "end",
    "closes": {date: close}, "last": latest price or None}``. FX closes and
    ``last`` are the AUDXXX=X pair's, as Yahoo quotes them; they're stored
    inverted, like get_historical_fx does. Every requested range is recorded
    as fetched even when it came back empty (a holiday, before listing), so it
    isn't reported as a miss.

    Returns the live quotes ``{ticker: last}`` and FX ``{currency: AUD per
    unit}`` (AUD included) that the pipeline takes as ``quotes`` and ``fx``.
    """
    quotes: dict[str, float] = {}
    fx: dict[str, float] = {"AUD": 1.0}
    for item in fetched:
        kind, key = item["kind"], item["key"]
        closes = {d: float(v) for d, v in (item.get("closes") or {}).items() if v is not None}
        last = item.get("last")
        if kind == "price":
            db.upsert_prices(conn, key, closes)
            if last is not None:
                quotes[key] = float(last)
        elif kind == "fx":
            db.upsert_fx(conn, key, _rates_to_aud(closes))
            rate = _rate_to_aud(last)
            if rate is not None:
                fx[key] = rate
        else:
            raise ValueError(f"Unknown fetched item kind {kind!r}.")
        db.add_fetch_range(conn, kind, key, item["start"], item["end"])
    return quotes, fx
