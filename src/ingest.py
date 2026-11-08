import pandas as pd
from pathlib import Path

from src.dates import parse_date_series
from src.market_data import EXCHANGE_MAP

REQUIRED_COLUMNS = {"code", "exchange", "date", "type", "quantity", "unit_price", "platform"}
KNOWN_EXCHANGES = set(EXCHANGE_MAP)
# "drp" (dividend reinvestment) is a valid input type. For CGT it is an
# acquisition identical to a buy — its own cost base and its own 12-month
# discount clock — so load_trades normalises it to "buy" while preserving the
# distinction in the boolean ``is_drp`` column for the audit trail.
VALID_TYPES = {"buy", "sell", "drp"}

# Accepted CSV names for the acquisition/disposal cost (brokerage, GST on
# brokerage, etc.). Any present columns are summed into the internal ``fees``.
FEE_COLUMN_ALIASES = ("fees", "brokerage")

CORPORATE_ACTIONS_COLUMNS = {"code", "exchange", "date", "ratio"}

# Optional per-dividend franking overrides for ASX holdings (Phase 5).
# franking_pct is 0–100 (percent of the dividend that is franked).
FRANKING_COLUMNS = {"code", "exchange", "date", "franking_pct"}


def load_franking(path: str | Path) -> pd.DataFrame:
    """Load optional data/franking.csv (code,exchange,date,franking_pct).

    Returns an empty frame with the right columns if absent, so callers treat
    "no file" and "no overrides" identically. ``date`` is the dividend ex-date.
    """
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=sorted(FRANKING_COLUMNS))

    df = pd.read_csv(path)
    df.columns = df.columns.str.strip().str.lower()
    missing = FRANKING_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"franking.csv is missing columns: {', '.join(sorted(missing))}"
        )
    df["date"] = parse_date_series(df["date"], source="franking.csv")
    df["franking_pct"] = pd.to_numeric(df["franking_pct"], errors="raise")
    df["code"] = df["code"].str.strip().str.upper()
    df["exchange"] = df["exchange"].str.strip().str.upper()
    if ((df["franking_pct"] < 0) | (df["franking_pct"] > 100)).any():
        raise ValueError("franking.csv: franking_pct must be between 0 and 100")
    return df.sort_values("date").reset_index(drop=True)


def load_corporate_actions(path: str | Path) -> pd.DataFrame:
    """Load optional data/corporate_actions.csv (code,exchange,date,ratio).

    Returns an empty frame with the right columns if the file is absent, so
    callers can treat "no file" and "no actions" identically (H4).
    """
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=sorted(CORPORATE_ACTIONS_COLUMNS))

    df = pd.read_csv(path)
    df.columns = df.columns.str.strip().str.lower()
    missing = CORPORATE_ACTIONS_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"corporate_actions.csv is missing columns: {', '.join(sorted(missing))}"
        )
    df["date"] = parse_date_series(df["date"], source="corporate_actions.csv")
    df["ratio"] = pd.to_numeric(df["ratio"], errors="raise")
    df["code"] = df["code"].str.strip().str.upper()
    df["exchange"] = df["exchange"].str.strip().str.upper()
    if (df["ratio"] <= 0).any():
        raise ValueError("corporate_actions.csv: ratio must be positive")
    return df.sort_values("date").reset_index(drop=True)


def load_trades(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = df.columns.str.strip().str.lower()

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"trades.csv is missing columns: {', '.join(sorted(missing))}")

    df["date"] = parse_date_series(df["date"], source="trades.csv")
    df["quantity"] = pd.to_numeric(df["quantity"], errors="raise")
    df["unit_price"] = pd.to_numeric(df["unit_price"], errors="raise")

    # Optional transaction-cost column(s) (local currency, default 0). Accept
    # either "fees" or "brokerage" (or both, summed) so the CSV can use whichever
    # name the user thinks in. Absent for older CSVs → 0.
    present_fee_cols = [c for c in FEE_COLUMN_ALIASES if c in df.columns]
    if present_fee_cols:
        fees = sum(
            pd.to_numeric(df[c], errors="raise").fillna(0.0)
            for c in present_fee_cols
        )
        df["fees"] = fees
    else:
        df["fees"] = 0.0

    df["code"] = df["code"].str.strip().str.upper()
    df["exchange"] = df["exchange"].str.strip().str.upper()
    df["type"] = df["type"].str.strip().str.lower()
    df["platform"] = df["platform"].str.strip()

    # A DRP reinvestment is a CGT acquisition identical to a buy. Record the
    # distinction, then normalise so every downstream consumer (cost base,
    # holdings, performance) treats it as a buy without needing a new branch.
    df["is_drp"] = df["type"] == "drp"
    df.loc[df["type"] == "drp", "type"] = "buy"

    return df.sort_values("date").reset_index(drop=True)


def validate_trades(df: pd.DataFrame) -> None:
    errors = []

    bad_qty = df[df["quantity"] <= 0]
    if not bad_qty.empty:
        for _, row in bad_qty.iterrows():
            errors.append(f"Row {row.name}: quantity must be positive (got {row['quantity']} for {row['code']})")

    bad_price = df[df["unit_price"] < 0]
    if not bad_price.empty:
        for _, row in bad_price.iterrows():
            errors.append(f"Row {row.name}: unit_price must be non-negative (got {row['unit_price']} for {row['code']})")

    bad_fees = df[df["fees"] < 0]
    if not bad_fees.empty:
        for _, row in bad_fees.iterrows():
            errors.append(f"Row {row.name}: fees/brokerage must be non-negative (got {row['fees']} for {row['code']})")

    bad_exchange = df[~df["exchange"].isin(KNOWN_EXCHANGES)]
    if not bad_exchange.empty:
        for _, row in bad_exchange.iterrows():
            errors.append(
                f"Row {row.name}: unknown exchange '{row['exchange']}' for {row['code']}. "
                f"Valid exchanges: {', '.join(sorted(KNOWN_EXCHANGES))}"
            )

    bad_type = df[~df["type"].isin(VALID_TYPES)]
    if not bad_type.empty:
        for _, row in bad_type.iterrows():
            errors.append(f"Row {row.name}: type must be 'buy', 'sell' or 'drp' (got '{row['type']}' for {row['code']})")

    # Check sells don't exceed holdings per (code, exchange, platform)
    for (code, exchange, platform), group in df.groupby(["code", "exchange", "platform"]):
        running = 0.0
        for _, row in group.iterrows():
            if row["type"] == "buy":
                running += row["quantity"]
            else:
                running -= row["quantity"]
                if running < -1e-9:
                    errors.append(
                        f"Row {row.name}: sell of {row['quantity']} {code} on {row['date'].date()} "
                        f"exceeds holdings ({running + row['quantity']:.4f} units held) "
                        f"on {platform}/{exchange}"
                    )

    if errors:
        raise ValueError("Trade validation failed:\n" + "\n".join(f"  - {e}" for e in errors))
