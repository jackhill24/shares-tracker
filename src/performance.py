"""True performance analytics: benchmarks, XIRR, TWR (Phase 3).

Everything here works in AUD. Cashflows come from the user's actual trades,
converted to AUD at trade-date FX (``fx_provider``). Benchmarks are AUD-quoted
ETFs, so a benchmark's close is already in AUD and needs no conversion — this
is why the config requires AUD benchmarks.

Sign convention for cashflows (investor's perspective, matching XIRR):
  buy  → money leaves the investor → NEGATIVE
  sell → money returns to investor → POSITIVE
The terminal portfolio value is a synthetic POSITIVE cashflow dated today.

Nothing in this module hits the network; benchmark prices are read from the
DB-backed cache the caller has already populated.
"""
from datetime import date, timedelta
from typing import Callable

import numpy as np
import pandas as pd

from src.market_data import EXCHANGE_MAP


# --------------------------------------------------------------------------
# Cashflows
# --------------------------------------------------------------------------

def trade_cashflows(
    trades_df: pd.DataFrame,
    fx_provider: Callable[[str, str], float],
) -> pd.DataFrame:
    """Per-trade AUD cashflows from the investor's perspective.

    Returns a frame [date, code, amount_aud, direction] where amount_aud is
    NEGATIVE for buys (money out) and POSITIVE for sells (money in), converted
    at trade-date FX and including fees (buys add fees to the outlay, sells net
    fees off proceeds).
    """
    records = []
    for _, row in trades_df.sort_values("date").iterrows():
        currency = EXCHANGE_MAP[row["exchange"]]["currency"]
        date_str = row["date"].strftime("%Y-%m-%d")
        fx = fx_provider(currency, date_str)
        fees = float(row.get("fees", 0.0) or 0.0)
        gross_local = row["quantity"] * row["unit_price"]

        if row["type"] == "buy":
            amount_local = gross_local + fees          # total outlay
            amount_aud = -amount_local * fx            # money out
            direction = "buy"
        else:  # sell
            amount_local = gross_local - fees          # net proceeds
            amount_aud = amount_local * fx             # money in
            direction = "sell"

        records.append({
            "date": row["date"].date() if hasattr(row["date"], "date") else row["date"],
            "code": row["code"],
            "amount_aud": amount_aud,
            "direction": direction,
        })

    return pd.DataFrame(records, columns=["date", "code", "amount_aud", "direction"])


def net_contributions_series(
    cashflows: pd.DataFrame,
    snapshot_dates: list[date],
) -> pd.DataFrame:
    """Cumulative net invested capital (buys − sells) in AUD on snapshot dates.

    ``cashflows.amount_aud`` is negative for buys / positive for sells, so the
    net capital the investor has put in is the running sum of ``-amount_aud``.
    The gap between this and portfolio value at the same date is market growth.
    """
    if not snapshot_dates:
        return pd.DataFrame(columns=["date", "net_contributed_aud"])

    cf = cashflows.copy()
    cf["invested"] = -cf["amount_aud"]  # buys positive, sells negative
    rows = []
    for d in sorted(snapshot_dates):
        prior = cf[cf["date"] <= d]
        rows.append({
            "date": d,
            "net_contributed_aud": float(prior["invested"].sum()),
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Shadow benchmark portfolio
# --------------------------------------------------------------------------

def shadow_portfolio(
    trades_df: pd.DataFrame,
    bench_prices: dict[str, float],
    fx_provider: Callable[[str, str], float],
    snapshot_dates: list[date],
) -> pd.DataFrame:
    """Replay the user's AUD cashflows into a single benchmark.

    Every buy invests that AUD amount into the benchmark at the trade date's
    close; every sell withdraws that AUD amount (selling benchmark units at the
    trade date's close). Units can't go negative — a withdrawal is capped at the
    units on hand. Returns [date, value_aud] on each snapshot date.

    ``bench_prices`` maps 'YYYY-MM-DD' → benchmark close (AUD). Prices are
    resolved with forward-fill (nearest prior available close) so weekend/
    holiday trade dates still price.
    """
    price_dates = sorted(bench_prices)

    def _price_at_or_before(d: date) -> float | None:
        d_str = d.isoformat()
        chosen = None
        for pd_str in price_dates:
            if pd_str <= d_str:
                chosen = bench_prices[pd_str]
            else:
                break
        return chosen

    cashflows = trade_cashflows(trades_df, fx_provider)

    # Build the running benchmark-unit ledger keyed by trade date.
    events = cashflows.sort_values("date")
    units = 0.0
    ledger: list[tuple[date, float]] = []  # (date, cumulative units after event)
    for _, cf in events.iterrows():
        price = _price_at_or_before(cf["date"])
        if price is None or price <= 0:
            # Benchmark not yet listed / no price — cashflow can't be replayed.
            ledger.append((cf["date"], units))
            continue
        # amount_aud: negative for buys (invest), positive for sells (withdraw).
        if cf["amount_aud"] < 0:                      # buy → invest
            units += (-cf["amount_aud"]) / price
        else:                                         # sell → withdraw
            withdraw_units = cf["amount_aud"] / price
            units = max(0.0, units - withdraw_units)
        ledger.append((cf["date"], units))

    def _units_as_of(d: date) -> float:
        held = 0.0
        for ev_date, u in ledger:
            if ev_date <= d:
                held = u
            else:
                break
        return held

    rows = []
    for d in sorted(snapshot_dates):
        price = _price_at_or_before(d)
        u = _units_as_of(d)
        value = u * price if price else float("nan")
        rows.append({"date": d, "value_aud": value})
    return pd.DataFrame(rows, columns=["date", "value_aud"])


# --------------------------------------------------------------------------
# XIRR (money-weighted return)
# --------------------------------------------------------------------------

def _xnpv(rate: float, amounts: np.ndarray, years: np.ndarray) -> float:
    return float(np.sum(amounts / (1.0 + rate) ** years))


def xirr(
    dated_cashflows: list[tuple[date, float]],
    guess: float = 0.1,
) -> float | None:
    """Money-weighted annual return solving XNPV(rate) = 0.

    ``dated_cashflows`` is a list of (date, amount) where amounts follow the
    investor sign convention (buys negative, sells/terminal-value positive).
    Requires at least one positive and one negative flow, else the equation has
    no meaningful root and None is returned.

    Solved with Newton's method (no scipy dependency), falling back to bisection
    if Newton fails to converge or leaves the valid domain (rate <= -1).
    """
    if len(dated_cashflows) < 2:
        return None
    amounts = np.array([a for _, a in dated_cashflows], dtype=float)
    if not (np.any(amounts > 0) and np.any(amounts < 0)):
        return None

    t0 = min(d for d, _ in dated_cashflows)
    years = np.array([(d - t0).days / 365.0 for d, _ in dated_cashflows])

    # --- Newton's method ---
    rate = guess
    for _ in range(100):
        try:
            f = _xnpv(rate, amounts, years)
            # Analytic derivative of XNPV w.r.t. rate.
            df = float(np.sum(-years * amounts / (1.0 + rate) ** (years + 1)))
        except (ZeroDivisionError, FloatingPointError, OverflowError):
            break
        if not np.isfinite(f) or not np.isfinite(df) or df == 0:
            break
        new_rate = rate - f / df
        if new_rate <= -1.0:                 # left the valid domain
            break
        if abs(new_rate - rate) < 1e-8:
            return new_rate
        rate = new_rate
    else:
        # 100 iterations without convergence — fall through to bisection.
        pass

    # --- Bisection fallback over a wide bracket ---
    lo, hi = -0.9999, 10.0
    f_lo = _xnpv(lo, amounts, years)
    f_hi = _xnpv(hi, amounts, years)
    if not (np.isfinite(f_lo) and np.isfinite(f_hi)) or f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = _xnpv(mid, amounts, years)
        if abs(f_mid) < 1e-9:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


def portfolio_xirr(
    cashflows: pd.DataFrame,
    current_value_aud: float,
    as_of: date | None = None,
) -> float | None:
    """Whole-portfolio XIRR: trade cashflows + terminal current value today."""
    as_of = as_of or date.today()
    flows = [(row["date"], row["amount_aud"]) for _, row in cashflows.iterrows()]
    if current_value_aud and not pd.isna(current_value_aud):
        flows.append((as_of, float(current_value_aud)))
    return xirr(flows)


def per_holding_xirr(
    cashflows: pd.DataFrame,
    current_values_by_code: dict[str, float],
    as_of: date | None = None,
) -> dict[str, float | None]:
    """Per-``code`` XIRR: that code's cashflows + its terminal value today."""
    as_of = as_of or date.today()
    out: dict[str, float | None] = {}
    for code, grp in cashflows.groupby("code"):
        flows = [(row["date"], row["amount_aud"]) for _, row in grp.iterrows()]
        terminal = current_values_by_code.get(code, 0.0)
        if terminal and not pd.isna(terminal):
            flows.append((as_of, float(terminal)))
        out[code] = xirr(flows)
    return out


# --------------------------------------------------------------------------
# Time-weighted return (TWR)
# --------------------------------------------------------------------------

def _annualise(total_return: float, days: float) -> float | None:
    """Convert a period total-return to an annualised rate. None if < ~1 week,
    or if it lost everything (or more — a sub-period whose contribution dwarfs
    its value, e.g. a mistyped trade price), which has no annual rate: a
    fractional power of a negative growth factor is a complex number."""
    if days < 7 or 1.0 + total_return <= 0:
        return None
    years = days / 365.0
    return (1.0 + total_return) ** (1.0 / years) - 1.0


def time_weighted_return(
    value_series: pd.DataFrame,
    cashflows: pd.DataFrame,
    window_start: date | None = None,
    as_of: date | None = None,
) -> dict[str, float | None]:
    """Chain-linked TWR over [window_start, as_of].

    ``value_series`` is [date, value] (portfolio net worth on snapshot dates, or
    a benchmark's shadow value). External cashflows (buys/sells) split the
    timeline into sub-periods; each sub-period return excludes the cashflow's
    effect by removing the contribution from the ending value before dividing by
    the (pre-cashflow) starting value. Sub-period returns are chain-linked.

    Cashflows here use the investor convention (buy negative). A contribution
    into the portfolio is ``-amount_aud`` (positive for a buy).

    Returns {total_return, annualised, start_date, end_date, days,
    start_value, end_value, net_contributions} — the last three exist so the
    dashboard can show the inputs behind each window's return.
    """
    empty = {"total_return": None, "annualised": None,
             "start_date": None, "end_date": None, "days": None,
             "start_value": None, "end_value": None, "net_contributions": None}
    if value_series is None or value_series.empty:
        return empty

    vs = value_series.copy()
    vs = vs.dropna(subset=["value"]).sort_values("date").reset_index(drop=True)
    if vs.empty:
        return empty

    as_of = as_of or vs["date"].iloc[-1]
    if window_start is None:
        window_start = vs["date"].iloc[0]

    vs = vs[(vs["date"] >= window_start) & (vs["date"] <= as_of)]
    if len(vs) < 2:
        return empty

    # Net external contribution (investor puts in) per date within the window.
    cf = cashflows.copy()
    cf["contribution"] = -cf["amount_aud"]  # buy positive, sell negative
    contrib_by_date = cf.groupby("date")["contribution"].sum().to_dict()

    dates = vs["date"].tolist()
    values = vs["value"].tolist()

    growth = 1.0
    for i in range(1, len(dates)):
        start_val = values[i - 1]
        end_val = values[i]
        # Contributions strictly AFTER the start snapshot up to and including
        # this one are external flows within the sub-period. Attribute them to
        # the end value so the return reflects market movement only.
        contrib = sum(
            amt for d, amt in contrib_by_date.items()
            if dates[i - 1] < d <= dates[i]
        )
        if start_val <= 0:
            continue
        sub_return = (end_val - contrib - start_val) / start_val
        growth *= (1.0 + sub_return)

    total_return = growth - 1.0
    days = (dates[-1] - dates[0]).days
    return {
        "total_return": total_return,
        "annualised": _annualise(total_return, days),
        "start_date": dates[0],
        "end_date": dates[-1],
        "days": days,
        "start_value": values[0],
        "end_value": values[-1],
        "net_contributions": sum(
            amt for d, amt in contrib_by_date.items()
            if dates[0] < d <= dates[-1]
        ),
    }


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------

# Standard reporting windows: label → lookback in days (None = all-time).
STANDARD_WINDOWS = [
    ("6M", 183),
    ("12M", 365),
    ("3Y", 365 * 3),
    ("All", None),
]


def window_start(as_of: date, lookback_days: int | None) -> date | None:
    return None if lookback_days is None else as_of - timedelta(days=lookback_days)


# --------------------------------------------------------------------------
# Orchestration — assemble everything the dashboard / brief consume
# --------------------------------------------------------------------------

_TWR_DETAIL_KEYS = ("start_date", "end_date", "days", "total_return",
                    "start_value", "end_value", "net_contributions")


def _twr_detail(twr: dict) -> dict:
    """The subset of a time_weighted_return result the dashboard's
    "show the working" expansion needs."""
    return {k: twr.get(k) for k in _TWR_DETAIL_KEYS}

def _value_series_from_snapshots(snapshot_totals_df: pd.DataFrame) -> pd.DataFrame:
    """Normalise snapshot_totals into the [date(date), value] shape TWR wants."""
    df = snapshot_totals_df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return (
        df[["date", "total_value_aud"]]
        .rename(columns={"total_value_aud": "value"})
        .sort_values("date")
        .reset_index(drop=True)
    )


def build_performance(
    trades_df: pd.DataFrame,
    snapshot_totals_df: pd.DataFrame,
    enriched_df: pd.DataFrame,
    totals: dict,
    bench_price_series: dict[str, dict[str, float]],
    benchmarks: list,
    fx_provider: Callable[[str, str], float],
    as_of: date | None = None,
) -> dict:
    """Assemble the full performance picture.

    ``bench_price_series`` maps benchmark ticker → {date_str: close_aud}.
    ``benchmarks`` is a list of config.Benchmark (ticker, label).

    Returns a dict with:
      value_series          — portfolio [date, value]
      contributions         — [date, net_contributed_aud]
      shadows               — {ticker: DataFrame[date, value_aud]}
      bench_labels          — {ticker: label}
      portfolio_xirr        — float | None
      per_holding_xirr      — {code: float | None}
      windows               — list of per-window rows (dicts) for the table
    Empty/degenerate inputs produce a dict with the same keys and empty values
    so callers can treat "no history yet" uniformly.
    """
    as_of = as_of or date.today()
    current_value = totals.get("total_value_aud", 0.0)

    cashflows = trade_cashflows(trades_df, fx_provider)

    result = {
        "value_series": pd.DataFrame(columns=["date", "value"]),
        "contributions": pd.DataFrame(columns=["date", "net_contributed_aud"]),
        "shadows": {},
        "bench_labels": {b.ticker: b.label for b in benchmarks},
        "portfolio_xirr": portfolio_xirr(cashflows, current_value, as_of),
        "per_holding_xirr": {},
        "windows": [],
    }

    # Per-holding XIRR (terminal value = current market value per code).
    values_by_code = (
        enriched_df.groupby("code")["market_value_aud"].sum().to_dict()
        if enriched_df is not None and not enriched_df.empty else {}
    )
    result["per_holding_xirr"] = per_holding_xirr(cashflows, values_by_code, as_of)

    if snapshot_totals_df is None or snapshot_totals_df.empty:
        return result

    value_series = _value_series_from_snapshots(snapshot_totals_df)
    snapshot_dates = value_series["date"].tolist()
    result["value_series"] = value_series
    result["contributions"] = net_contributions_series(cashflows, snapshot_dates)

    # Shadow portfolios per benchmark.
    for b in benchmarks:
        prices = bench_price_series.get(b.ticker, {})
        if not prices:
            continue
        result["shadows"][b.ticker] = shadow_portfolio(
            trades_df, prices, fx_provider, snapshot_dates
        )

    # Windowed TWR: portfolio vs each benchmark, with excess return.
    port_vs = value_series  # already [date, value]
    for label, lookback in STANDARD_WINDOWS:
        wstart = window_start(as_of, lookback)
        port_twr = time_weighted_return(port_vs, cashflows, wstart, as_of)
        # A window with < 2 snapshots yields None — skip it entirely.
        if port_twr["annualised"] is None and port_twr["total_return"] is None:
            continue

        bench_entries = []
        for b in benchmarks:
            shadow = result["shadows"].get(b.ticker)
            if shadow is None or shadow.empty:
                continue
            bench_vs = shadow.rename(columns={"value_aud": "value"})
            bench_twr = time_weighted_return(bench_vs, cashflows, wstart, as_of)
            excess = None
            if (port_twr["annualised"] is not None
                    and bench_twr["annualised"] is not None):
                excess = port_twr["annualised"] - bench_twr["annualised"]
            bench_entries.append({
                "ticker": b.ticker,
                "label": b.label,
                "annualised": bench_twr["annualised"],
                "total_return": bench_twr["total_return"],
                "excess": excess,
                "detail": _twr_detail(bench_twr),
            })

        result["windows"].append({
            "window": label,
            "days": port_twr["days"],
            "portfolio_twr": port_twr["annualised"],
            "portfolio_total": port_twr["total_return"],
            "portfolio_detail": _twr_detail(port_twr),
            "benchmarks": bench_entries,
        })

    return result
