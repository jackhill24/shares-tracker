import math
import pandas as pd
from src.market_data import EXCHANGE_MAP, to_yf_ticker


def compute_holdings(
    trades_df: pd.DataFrame,
    corporate_actions: pd.DataFrame | None = None,
) -> pd.DataFrame:
    records = []
    has_ca = corporate_actions is not None and not corporate_actions.empty

    for (code, exchange, platform), group in trades_df.groupby(["code", "exchange", "platform"]):
        group = group.sort_values("date")

        # Merge trade rows and split events into one chronologically-ordered
        # stream. A split multiplies units and preserves total cost (H4).
        events = [(r["date"], "trade", r) for _, r in group.iterrows()]
        if has_ca:
            splits = corporate_actions[
                (corporate_actions["code"] == code)
                & (corporate_actions["exchange"] == exchange)
            ]
            events += [(s["date"], "split", s) for _, s in splits.iterrows()]
        # Stable sort keeps buys before same-day splits (split acts on holdings).
        events.sort(key=lambda e: e[0])

        net_units = 0.0
        total_cost_local = 0.0

        for _, kind, row in events:
            if kind == "split":
                net_units *= float(row["ratio"])  # total cost unchanged
                continue
            if row["type"] == "buy":
                net_units += row["quantity"]
                # Buy fees form part of the cost base.
                total_cost_local += row["quantity"] * row["unit_price"] + row.get("fees", 0.0)
            else:
                # Reduce average cost proportionally on sell
                if net_units > 0:
                    avg_cost = total_cost_local / net_units
                    total_cost_local -= row["quantity"] * avg_cost
                net_units -= row["quantity"]

        if net_units < 1e-9:
            continue

        avg_cost_local = total_cost_local / net_units if net_units > 0 else 0.0
        currency = EXCHANGE_MAP[exchange]["currency"]
        yf_ticker = to_yf_ticker(code, exchange)

        records.append({
            "code": code,
            "exchange": exchange,
            "platform": platform,
            "currency": currency,
            "yf_ticker": yf_ticker,
            "units": net_units,
            "avg_cost_local": avg_cost_local,
            "total_cost_local": total_cost_local,
        })

    return pd.DataFrame(records)


def enrich_with_market_data(
    holdings_df: pd.DataFrame,
    prices: dict[str, float],
    fx: dict[str, float],
) -> pd.DataFrame:
    df = holdings_df.copy()

    df["current_price_local"] = df["yf_ticker"].map(prices)
    df["fx_to_aud"] = df["currency"].map(fx)

    df["current_price_aud"] = df["current_price_local"] * df["fx_to_aud"]
    df["avg_cost_aud"] = df["avg_cost_local"] * df["fx_to_aud"]
    df["market_value_aud"] = df["units"] * df["current_price_aud"]
    df["cost_base_aud"] = df["units"] * df["avg_cost_aud"]
    df["unrealised_pl_aud"] = df["market_value_aud"] - df["cost_base_aud"]

    df["unrealised_pl_pct"] = (
        (df["unrealised_pl_aud"] / df["cost_base_aud"].replace(0, float("nan"))) * 100
    )

    total_value = df["market_value_aud"].sum()
    df["weight_pct"] = (df["market_value_aud"] / total_value * 100) if total_value > 0 else 0.0

    return df


def aggregate(holdings_df: pd.DataFrame) -> dict:
    df = holdings_df

    def _rollup(by: str) -> pd.DataFrame:
        return (
            df.groupby(by)
            .agg(market_value_aud=("market_value_aud", "sum"))
            .assign(weight_pct=lambda x: x["market_value_aud"] / x["market_value_aud"].sum() * 100)
            .reset_index()
        )

    total_value = df["market_value_aud"].sum()
    total_cost = df["cost_base_aud"].sum()
    total_pl = df["unrealised_pl_aud"].sum()
    total_pl_pct = (total_pl / total_cost * 100) if total_cost > 0 else 0.0

    return {
        "totals": {
            "total_value_aud": total_value,
            "total_cost_aud": total_cost,
            "total_pl_aud": total_pl,
            "total_pl_pct": total_pl_pct,
            "num_positions": len(df),
        },
        "by_exchange": _rollup("exchange"),
        "by_currency": _rollup("currency"),
        "by_platform": _rollup("platform"),
        "by_code": aggregate_by_code(df),
    }


# --- Asset classification (derived from exchange) ---------------------------

_CRYPTO_EXCHANGES = {"CRYPTO", "CRYPTO-AUD"}

_REGION_MAP = {
    "ASX": "Australia",
    "NASDAQ": "United States",
    "NYSE": "United States",
    "LSE": "United Kingdom",
    "HKEX": "Hong Kong",
    "TSX": "Canada",
    "XMIL": "Europe",
    "CRYPTO": "Global (Crypto)",
    "CRYPTO-AUD": "Global (Crypto)",
}


def asset_class(exchange: str) -> str:
    return "Crypto" if exchange in _CRYPTO_EXCHANGES else "Equity"


def region(exchange: str) -> str:
    return _REGION_MAP.get(exchange, "Other")


def aggregate_by_code(holdings_df: pd.DataFrame) -> pd.DataFrame:
    """Collapse positions to one row per ticker ``code``, summing across the
    exchanges/platforms an asset is held on.

    The same asset can span currencies (e.g. ETH held as both CRYPTO-USD and
    CRYPTO-AUD), so only AUD-normalised money figures are summed. Local-currency
    price / avg-cost are value-weighted and only meaningful when the group is a
    single currency — callers should show "—" otherwise (see ``single_currency``).
    """
    df = holdings_df
    total_value = df["market_value_aud"].sum()

    records = []
    for code, g in df.groupby("code"):
        mv = g["market_value_aud"].sum()
        cb = g["cost_base_aud"].sum()
        pl = g["unrealised_pl_aud"].sum()
        currencies = sorted(g["currency"].unique())
        single_ccy = len(currencies) == 1

        # Value-weighted local price / avg cost (only meaningful single-currency).
        w = g["market_value_aud"]
        wsum = w.sum()
        if single_ccy and wsum > 0:
            avg_price_local = (g["current_price_local"] * w).sum() / wsum
            avg_cost_local = (g["avg_cost_local"] * w).sum() / wsum
        else:
            avg_price_local = float("nan")
            avg_cost_local = float("nan")

        records.append({
            "code": code,
            "units": g["units"].sum(),
            "asset_class": asset_class(g["exchange"].iloc[0]),
            "currency": currencies[0] if single_ccy else "Mixed",
            "single_currency": single_ccy,
            "avg_cost_local": avg_cost_local,
            "current_price_local": avg_price_local,
            "avg_cost_aud": (cb / g["units"].sum()) if g["units"].sum() > 0 else float("nan"),
            "current_price_aud": (mv / g["units"].sum()) if g["units"].sum() > 0 else float("nan"),
            "market_value_aud": mv,
            "cost_base_aud": cb,
            "unrealised_pl_aud": pl,
            "unrealised_pl_pct": (pl / cb * 100) if cb > 0 else float("nan"),
            "weight_pct": (mv / total_value * 100) if total_value > 0 else 0.0,
            "platforms": sorted(g["platform"].unique()),
            "exchanges": sorted(g["exchange"].unique()),
            "currencies": currencies,
            "n_parcels": len(g),
        })

    out = pd.DataFrame(records)
    return out.sort_values("market_value_aud", ascending=False).reset_index(drop=True)
