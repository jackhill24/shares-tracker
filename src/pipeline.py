"""The dashboard pipeline, shared by the command line and the browser edition.

``build_dashboard`` turns trades, settings, live quotes and a price cache into
the dashboard's HTML. It reads and writes no files and never asks for prices
itself: every price lookup goes through src/market_data.py, which reads the
SQLite cache first. The two callers fill that cache differently —

  * main.py leaves the provider on yfinance, so cache misses are fetched as
    the pipeline goes (and remembered in portfolio.db between runs).
  * The browser edition can't run yfinance. It asks ``market_requirements``
    what the pipeline will read, fetches all of it up front, loads it into an
    in-memory database with ``seed_cache`` and sets the provider to "none".

So ``market_requirements`` must list everything ``build_dashboard`` reads:
change one, change the other (tests/test_pipeline.py checks they agree).
"""
from datetime import date, timedelta

import pandas as pd

from src.allocation import (
    AllocationConfig,
    bucket_summary,
    fingerprint,
    parcels_payload,
)
from src.cgt import compute_realised_gains, simulate_disposal, summarise_by_fy
from src.compute import aggregate, compute_holdings, enrich_with_market_data
from src.config import Config
from src.db import get_fx_at_or_before, load_snapshot_totals
from src.income import compute_dividend_income, summarise_income_by_fy, trailing_yield
from src.market_data import (
    EXCHANGE_MAP,
    fx_pair,
    get_dividends,
    get_historical_fx,
    get_historical_prices,
    to_yf_ticker,
)
from src.performance import build_performance
from src.render import render_html
from src.risk import compute_risk_metrics
from src.snapshots import backfill_snapshots, maybe_take_snapshot

# Risk metrics use daily prices and FX over this trailing window.
RISK_WINDOW = timedelta(days=365 * 3)


class NoOpenPositions(ValueError):
    """The trades leave nothing held, so there's no dashboard to build."""


def market_requirements(
    trades: pd.DataFrame,
    corporate_actions: pd.DataFrame,
    config: Config,
    today: date | None = None,
) -> dict:
    """What ``build_dashboard(public=True)`` will read from the price cache.

    Returns a JSON-friendly dict:

      holdings    {yf_ticker: exchange} for open positions — these need a live
                  quote (the ``last`` of their price request).
      currencies  foreign currencies held — these need a live FX rate (the
                  ``last`` of their fx request).
      requests    one per symbol to fetch, each
                  ``{"kind": "price"|"fx", "key", "symbol", "start", "end"}``.
                  ``key`` is the ticker, or the currency for fx (whose
                  ``symbol`` is its AUDXXX=X pair). Ranges are inclusive dates.

    Every quote and FX rate needed is the latest close of a request already in
    the list, so one fetch per request covers it. Dividends (only the private
    edition's income section reads them) and sectors aren't included.
    """
    today = today or date.today()
    today_str = today.isoformat()
    earliest = trades["date"].min().date().isoformat()
    risk_start = (today - RISK_WINDOW).isoformat()

    holdings = compute_holdings(trades, corporate_actions)
    held = {r["yf_ticker"]: r["exchange"] for _, r in holdings.iterrows()}
    held_currencies = sorted(
        c for c in set(holdings["currency"]) if c != "AUD"
    ) if not holdings.empty else []

    # Prices: every ticker ever traded, from the first trade (snapshot
    # backfill); held ones also over the risk window, which can reach further
    # back; benchmarks from the first trade (performance).
    price_start: dict[str, str] = {}

    def need_price(ticker: str, start: str) -> None:
        price_start[ticker] = min(start, price_start.get(ticker, start))

    pairs = trades[["code", "exchange"]].drop_duplicates()
    for _, r in pairs.iterrows():
        need_price(to_yf_ticker(r["code"], r["exchange"]), earliest)
    for ticker in held:
        need_price(ticker, risk_start)
    for b in config.benchmarks:
        need_price(b.ticker, earliest)

    # FX: every foreign currency traded, from the first trade (snapshots and
    # trade-date cost bases); held ones also over the risk window.
    fx_start: dict[str, str] = {}
    for ex in trades["exchange"].unique():
        cur = EXCHANGE_MAP[ex]["currency"]
        if cur != "AUD" and fx_pair(cur):
            fx_start[cur] = earliest
    for cur in held_currencies:
        if fx_pair(cur):
            fx_start[cur] = min(risk_start, fx_start.get(cur, risk_start))

    requests = [
        {"kind": "price", "key": t, "symbol": t, "start": s, "end": today_str}
        for t, s in sorted(price_start.items())
    ] + [
        {"kind": "fx", "key": c, "symbol": fx_pair(c), "start": s, "end": today_str}
        for c, s in sorted(fx_start.items())
    ]
    return {
        "holdings": dict(sorted(held.items())),
        "currencies": held_currencies,
        "requests": requests,
    }


def build_dashboard(
    trades: pd.DataFrame,
    corporate_actions: pd.DataFrame,
    allocation_cfg: AllocationConfig,
    config: Config,
    conn,
    quotes: dict[str, float],
    fx: dict[str, float],
    *,
    public: bool,
    title: str,
    storage_key: str | None = None,
    web: bool = False,
    franking: pd.DataFrame | None = None,
    sectors: dict[str, str] | None = None,
    allocation_warnings: list[str] | None = None,
    allocation_file: str = "allocation.yaml",
    allocation_download: str = "shares-tracker-allocation.yaml",
) -> tuple[str, dict]:
    """Build the dashboard → (html, summary).

    ``trades`` must already have passed ``validate_trades``. ``quotes`` are
    live prices by yf_ticker and ``fx`` live AUD-per-unit rates by currency
    (AUD included). ``sectors`` ({yf_ticker: sector}) adds the Sector views;
    without it they're left off. ``franking`` only matters to the private
    edition's income section. ``allocation_file``/``allocation_download`` name
    the YAML in the Targets section's local-mode messages; ``web`` and
    ``storage_key`` are passed through to render_html. Raises NoOpenPositions
    when nothing is held. The summary carries the figures main.py prints.
    """
    # Compute holdings (split-adjusted)
    holdings = compute_holdings(trades, corporate_actions)
    if holdings.empty:
        raise NoOpenPositions("No open positions found. Check your trades CSV.")
    print(f"  {len(holdings)} open positions computed.")

    tickers = sorted(set(holdings["yf_ticker"].tolist()))
    currencies = set(holdings["currency"].tolist())

    # Enrich and aggregate
    enriched = enrich_with_market_data(holdings, quotes, fx)
    aggs = aggregate(enriched)
    totals = aggs["totals"]

    # Sector per ticker → "By Sector" allocation and the map's Sector view.
    if sectors is not None:
        enriched["sector"] = enriched["yf_ticker"].map(sectors)

    # Pre-cache FX for all foreign-currency trade dates (used by fx_provider)
    foreign_trade_dates: dict[str, list[str]] = {}
    for _, t in trades.iterrows():
        cur = EXCHANGE_MAP[t["exchange"]]["currency"]
        if cur != "AUD":
            foreign_trade_dates.setdefault(cur, []).append(t["date"].strftime("%Y-%m-%d"))
    for currency, dts in sorted(foreign_trade_dates.items()):
        print(f"  Pre-caching FX history for {currency}...")
        get_historical_fx(conn, currency, sorted(set(dts)))

    # Gap-fill snapshots every run (H6). backfill_snapshots is idempotent —
    # it skips dates already present and heals any missing dates in the expected
    # cadence from the cached price/FX history.
    print("\nBackfilling any missing historical snapshots...")
    backfill_snapshots(conn, trades, corporate_actions)

    # Take today's snapshot if due
    print("\nChecking snapshot schedule...")
    maybe_take_snapshot(conn, enriched, totals)

    # Load snapshot history for charts/brief
    snapshot_totals_df = load_snapshot_totals(conn)

    # Compute realised CGT via FIFO using date-appropriate FX
    # (fx_provider is also used by the performance figures below).
    if not public:
        print("\nComputing realised capital gains (FIFO)...")

    def fx_provider(currency: str, date_str: str) -> float:
        rate = get_fx_at_or_before(conn, currency, date_str)
        if rate is None:
            # CGT with unknown FX is meaningless — fail loudly rather than
            # producing a silently-wrong tax figure. (H7)
            raise ValueError(
                f"No FX rate available for {currency} at or before {date_str}. "
                f"Cannot compute CGT cost base/proceeds without it."
            )
        return rate

    realised_df = open_parcels_df = fy_summary_df = None
    if not public:
        realised_df, open_parcels_df = compute_realised_gains(
            trades, fx_provider, corporate_actions
        )
        fy_summary_df = summarise_by_fy(realised_df)
        if not fy_summary_df.empty:
            print(f"  {len(realised_df)} disposal events across {len(fy_summary_df)} financial years.")

    # --- Phase 3: benchmarks & true performance ---
    print("\nComputing performance vs benchmarks...")
    earliest = trades["date"].min().date()
    today_str = date.today().isoformat()
    bench_price_series: dict[str, dict[str, float]] = {}
    for b in config.benchmarks:
        print(f"  Fetching benchmark history for {b.ticker} ({b.label})...")
        bench_price_series[b.ticker] = get_historical_prices(
            conn, b.ticker, earliest.isoformat(), today_str
        )

    performance = build_performance(
        trades, snapshot_totals_df, enriched, totals,
        bench_price_series, config.benchmarks, fx_provider,
    )
    if performance["portfolio_xirr"] is not None:
        print(f"  Portfolio XIRR (all-time): {performance['portfolio_xirr'] * 100:.2f}%")

    # --- Phase 5: income & risk analytics ---
    # yf_ticker → (code, exchange), needed by the income/risk replay.
    ticker_meta = {
        r["yf_ticker"]: (r["code"], r["exchange"])
        for _, r in holdings.iterrows()
    }

    # Forgiving historical FX: nearest prior cached rate (forward-fill), or NaN
    # if none — used for daily series where a hard fail would be too brittle.
    def hist_fx(currency: str, date_str: str) -> float:
        if currency == "AUD":
            return 1.0
        rate = get_fx_at_or_before(conn, currency, date_str)
        return rate if rate is not None else float("nan")

    # Pre-cache FX daily history over the 3-year risk window for foreign holdings.
    risk_start = (date.today() - RISK_WINDOW).isoformat()
    risk_days = pd.date_range(risk_start, today_str, freq="D").strftime("%Y-%m-%d").tolist()
    for cur in sorted(c for c in currencies if c != "AUD"):
        get_historical_fx(conn, cur, risk_days)

    # Daily price history per holding over the risk window (cache-first).
    print("\nComputing risk analytics..." if public else "\nComputing income & risk analytics...")
    holding_price_series: dict[str, dict[str, float]] = {}
    for tkr in tickers:
        holding_price_series[tkr] = get_historical_prices(conn, tkr, risk_start, today_str)

    # E1. Dividend income by FY.
    income_df = income_fy_df = income_yield = None
    if not public:
        dividends_by_ticker = {
            tkr: get_dividends(conn, tkr, earliest.isoformat(), today_str)
            for tkr in tickers
        }
        income_df = compute_dividend_income(
            trades, dividends_by_ticker, ticker_meta, hist_fx, corporate_actions,
            franking if franking is not None else pd.DataFrame(),
        )
        income_fy_df = summarise_income_by_fy(income_df)
        income_yield = trailing_yield(
            income_df, totals["total_value_aud"], totals.get("total_cost_aud", 0.0),
        )
        if not income_df.empty:
            print(f"  {len(income_df)} dividend event(s); TTM income ${income_yield['ttm_income_aud']:,.0f}.")

    # E2. Risk metrics vs the primary benchmark.
    primary_bench = config.benchmarks[0] if config.benchmarks else None
    bench_value_series = None
    if primary_bench is not None:
        bp = bench_price_series.get(primary_bench.ticker, {})
        if bp:
            bser = pd.Series(bp)
            bser.index = pd.to_datetime(bser.index)
            bench_value_series = bser.sort_index()
    risk_metrics = compute_risk_metrics(
        trades, ticker_meta, holding_price_series, hist_fx,
        config.rba_cash_rate,
        benchmark_value_series=bench_value_series,
        benchmark_label=primary_bench.label if primary_bench else None,
        corporate_actions=corporate_actions,
    )
    if risk_metrics["annualised_volatility"] is not None:
        print(
            f"  Volatility {risk_metrics['annualised_volatility'] * 100:.1f}% p.a.; "
            f"max drawdown {risk_metrics['max_drawdown'] * 100:.1f}%."
        )

    # E3. What-if disposal ("if sold today") per current holding.
    fx_now = {c: fx.get(c, float("nan")) for c in currencies}
    disposals: dict[str, dict] = {}
    if not public:
        for (code, exchange), grp in enriched.groupby(["code", "exchange"]):
            units_now = float(grp["units"].sum())
            price_local = float(grp.iloc[0]["current_price_local"]) \
                if "current_price_local" in grp else float("nan")
            cur = EXCHANGE_MAP[exchange]["currency"]
            if units_now <= 0 or pd.isna(price_local):
                continue
            disposals[f"{code}:{exchange}"] = simulate_disposal(
                open_parcels_df, code, exchange, units_now, price_local,
                fx_now.get(cur, float("nan")),
            )

    analytics = {
        "income_df": income_df,
        "income_fy_df": income_fy_df,
        "income_yield": income_yield,
        "risk": risk_metrics,
        "disposals": disposals,
    }

    # E4. Target allocation: strategy buckets vs targets (Targets section).
    alloc_summary = bucket_summary(aggs["by_code"], allocation_cfg)
    allocation = {
        "config": allocation_cfg,
        "fingerprint": fingerprint(allocation_cfg),
        "summary": alloc_summary,
        "warnings": list(allocation_warnings or []),
        # Per-parcel CGT inputs, only needed by the rebalancing scenarios.
        "parcels": {} if public else parcels_payload(open_parcels_df, enriched),
        "file_name": allocation_file,
        "download_name": allocation_download,
    }

    # Generate brief (imported here: the public edition ships without it)
    brief_md = None
    if not public:
        from src.brief import generate_brief
        brief_md = generate_brief(
            enriched, aggs, fy_summary_df, open_parcels_df, snapshot_totals_df,
            performance=performance, analytics=analytics, allocation=allocation,
        )

    # M5: warn on the dashboard if any current position has unknown price/FX —
    # totals under-sum silently otherwise.
    incomplete = enriched[
        enriched["current_price_aud"].isna() | enriched["market_value_aud"].isna()
    ]
    data_warning = None
    if not incomplete.empty:
        codes = ", ".join(sorted(incomplete["code"].unique()))
        data_warning = (
            f"Live price/FX data was unavailable for {codes}. Totals below "
            f"exclude these positions and understate your true value."
        )

    print("\nRendering dashboard...")
    html = render_html(
        enriched, aggs, brief_md, snapshot_totals_df,
        fy_summary_df, realised_df=realised_df, data_warning=data_warning,
        performance=performance, analytics=analytics, title=title,
        allocation=allocation, public=public, storage_key=storage_key, web=web,
    )

    buckets = alloc_summary["buckets"]
    summary = {
        "total_value_aud": float(totals["total_value_aud"]),
        "total_pl_aud": float(totals["total_pl_aud"]),
        "total_pl_pct": float(totals["total_pl_pct"]),
        "num_positions": int(totals["num_positions"]),
        "buckets_off": int((buckets["status"] != "on").sum())
        if alloc_summary["assigned_total"] > 0 else 0,
        "unassigned": len(alloc_summary["unassigned"]),
        "unassigned_value": float(alloc_summary["unassigned_value"]),
        "data_warning": data_warning,
    }
    return html, summary
