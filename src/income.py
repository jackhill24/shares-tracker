"""Dividend income by financial year (Phase 5).

For each holding we pull its cached per-share dividend history, work out how many
units were held on each ex-date (replayed from the trade ledger, split-adjusted),
convert the cash to AUD at the ex-date FX, and group by Australian financial year.

Franking credits for ASX holdings are optional and come from a manual override
CSV (yfinance doesn't expose franking); when present they're grossed up at the
30% company tax rate to estimate the attached credit.

Everything reads from the DB-backed caches the caller has already populated —
nothing here hits the network.
"""
from collections import deque
from datetime import date
from typing import Callable

import pandas as pd

from src.market_data import EXCHANGE_MAP

_EPS = 1e-9

# Australian company tax rate used to gross up franked dividends into credits:
# credit = dividend * franked_fraction * rate / (1 - rate).
_COMPANY_TAX_RATE = 0.30

_INCOME_COLUMNS = [
    "fy", "code", "exchange", "currency", "ex_date", "units_held",
    "amount_local", "amount_aud", "franking_pct", "franking_credit_aud",
]

_FY_INCOME_COLUMNS = [
    "fy", "dividend_income_aud", "franking_credits_aud", "grossed_up_aud",
]


def _fy_label(d: date) -> str:
    """Australian FY: 1 July – 30 June, labelled FYxx/yy."""
    if d.month >= 7:
        start, end = d.year, d.year + 1
    else:
        start, end = d.year - 1, d.year
    return f"FY{str(start)[-2:]}/{str(end)[-2:]}"


def _fy_sort_key(fy_label: str) -> int:
    start = int(fy_label[2:4])
    return start + (1900 if start >= 70 else 2000)


def _splits_for_key(corporate_actions, code, exchange) -> list[tuple[date, float]]:
    if corporate_actions is None or corporate_actions.empty:
        return []
    ca = corporate_actions
    sub = ca[(ca["code"] == code) & (ca["exchange"] == exchange)]
    return sorted(
        (pd.to_datetime(r["date"]).date(), float(r["ratio"]))
        for _, r in sub.iterrows()
    )


def units_held_series(
    trades_df: pd.DataFrame,
    code: str,
    exchange: str,
    days: list[date],
    corporate_actions: pd.DataFrame | None = None,
) -> list[float]:
    """Split-adjusted units of (code, exchange) held at end of each of ``days``.

    ``days`` must be ascending. Trades (across all platforms) and splits are
    replayed in date order in one pass, as compute_holdings does: a split
    scales the units held at that point, same-day trades included, so units
    bought after a split aren't scaled by it.
    """
    sub = trades_df[(trades_df["code"] == code) & (trades_df["exchange"] == exchange)]
    events = [
        (ts.date(), 0, qty if kind == "buy" else -qty)
        for ts, kind, qty in zip(sub["date"], sub["type"], sub["quantity"])
    ]
    events += [(d, 1, ratio) for d, ratio in _splits_for_key(corporate_actions, code, exchange)]
    events.sort(key=lambda e: (e[0], e[1]))  # same-day trades before the split

    out, units, i = [], 0.0, 0
    for day in days:
        while i < len(events) and events[i][0] <= day:
            _, is_split, v = events[i]
            units = units * v if is_split else units + v
            i += 1
        out.append(max(0.0, units))
    return out


def units_held_on(
    trades_df: pd.DataFrame,
    code: str,
    exchange: str,
    on: date,
    corporate_actions: pd.DataFrame | None = None,
) -> float:
    """Split-adjusted units of (code, exchange) held at end of day ``on``.

    A dividend's ex-date is the entitlement cutoff, so "held on the ex-date"
    correctly captures entitlement. See ``units_held_series``.
    """
    return units_held_series(trades_df, code, exchange, [on], corporate_actions)[0]


def compute_dividend_income(
    trades_df: pd.DataFrame,
    dividends_by_ticker: dict[str, dict[str, float]],
    ticker_meta: dict[str, tuple[str, str]],
    fx_provider: Callable[[str, str], float],
    corporate_actions: pd.DataFrame | None = None,
    franking_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Per-dividend income across all holdings.

    ``dividends_by_ticker`` maps yf_ticker → {ex_date_str: per_share_local}.
    ``ticker_meta`` maps yf_ticker → (code, exchange). ``fx_provider`` gives the
    AUD value of one local-currency unit at a date. Franking overrides (ASX only)
    are matched on (code, exchange, ex_date).

    Returns a per-dividend frame (one row per ex-date with non-zero units held),
    with columns in ``_INCOME_COLUMNS``. Ex-dates where nothing was held are
    dropped (no entitlement).
    """
    frank_lookup: dict[tuple, float] = {}
    if franking_df is not None and not franking_df.empty:
        for _, r in franking_df.iterrows():
            frank_lookup[(r["code"], r["exchange"], r["date"].date())] = float(r["franking_pct"])

    records = []
    for ticker, divs in dividends_by_ticker.items():
        code, exchange = ticker_meta[ticker]
        currency = EXCHANGE_MAP[exchange]["currency"]
        for ex_date_str, per_share in sorted(divs.items()):
            ex_date = date.fromisoformat(ex_date_str)
            units = units_held_on(trades_df, code, exchange, ex_date, corporate_actions)
            if units <= _EPS or not per_share:
                continue

            amount_local = units * per_share
            fx = fx_provider(currency, ex_date_str)
            amount_aud = amount_local * fx

            franking_pct = frank_lookup.get((code, exchange, ex_date), 0.0)
            # Franking credit only meaningful for AUD (ASX) dividends.
            if franking_pct and currency == "AUD":
                franked_fraction = franking_pct / 100.0
                credit_aud = (
                    amount_aud * franked_fraction
                    * _COMPANY_TAX_RATE / (1.0 - _COMPANY_TAX_RATE)
                )
            else:
                credit_aud = 0.0

            records.append({
                "fy": _fy_label(ex_date),
                "code": code,
                "exchange": exchange,
                "currency": currency,
                "ex_date": ex_date,
                "units_held": units,
                "amount_local": amount_local,
                "amount_aud": amount_aud,
                "franking_pct": franking_pct,
                "franking_credit_aud": credit_aud,
            })

    if not records:
        return pd.DataFrame(columns=_INCOME_COLUMNS)
    return pd.DataFrame(records)[_INCOME_COLUMNS]


def summarise_income_by_fy(income_df: pd.DataFrame) -> pd.DataFrame:
    """Group per-dividend income into FY totals (income, credits, grossed-up)."""
    if income_df is None or income_df.empty:
        return pd.DataFrame(columns=_FY_INCOME_COLUMNS)

    grp = income_df.groupby("fy", as_index=False).agg(
        dividend_income_aud=("amount_aud", "sum"),
        franking_credits_aud=("franking_credit_aud", "sum"),
    )
    grp["grossed_up_aud"] = grp["dividend_income_aud"] + grp["franking_credits_aud"]
    grp["_k"] = grp["fy"].map(_fy_sort_key)
    return grp.sort_values("_k").drop(columns="_k").reset_index(drop=True)


def trailing_yield(
    income_df: pd.DataFrame,
    current_value_aud: float,
    cost_base_aud: float,
    as_of: date | None = None,
) -> dict[str, float | None]:
    """Trailing-12-month dividend yield on current value and on cost.

    Sums AUD dividends with ex-date in the year ending ``as_of`` and divides by
    the respective base. Returns {ttm_income_aud, yield_on_value, yield_on_cost};
    a base of 0 yields None for that ratio.
    """
    as_of = as_of or date.today()
    if income_df is None or income_df.empty:
        return {"ttm_income_aud": 0.0, "yield_on_value": None, "yield_on_cost": None}

    one_year_ago = date(as_of.year - 1, as_of.month, as_of.day) \
        if not (as_of.month == 2 and as_of.day == 29) else date(as_of.year - 1, 2, 28)
    ttm = income_df[
        (income_df["ex_date"] > one_year_ago) & (income_df["ex_date"] <= as_of)
    ]["amount_aud"].sum()

    return {
        "ttm_income_aud": float(ttm),
        "yield_on_value": (ttm / current_value_aud) if current_value_aud else None,
        "yield_on_cost": (ttm / cost_base_aud) if cost_base_aud else None,
    }
