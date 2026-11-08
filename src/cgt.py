"""FIFO realised capital gains for Australian CGT estimation.

Tracks buy parcels per (code, exchange) in a FIFO queue and matches them
against sells. Cost base and proceeds are converted to AUD using the FX rate
at the respective transaction date (ATO treatment of foreign-currency CGT).

This is intentionally separate from compute.py's average-cost method.
"""
from collections import deque
from datetime import date
from typing import Callable

import pandas as pd
from dateutil.relativedelta import relativedelta

from src.market_data import EXCHANGE_MAP

_EPS = 1e-9


def _discount_eligible(acquired: date, disposed: date) -> bool:
    """ATO 12-month rule: eligible only if held for MORE than 12 months.

    Exactly 12 months (e.g. acquired 10 Jul 2024, disposed 10 Jul 2025) is
    NOT eligible; 12 months + 1 day is. Uses a calendar-month test rather than
    a 365-day count so leap years are handled correctly.
    """
    return disposed > acquired + relativedelta(months=12)

_REALISED_COLUMNS = [
    "code", "exchange", "currency", "acquired", "disposed", "units",
    "unit_cost_local", "unit_proceeds_local", "cost_base_aud",
    "proceeds_aud", "gain_aud", "held_days", "discount_eligible",
]

_OPEN_COLUMNS = [
    "code", "exchange", "currency", "acquired", "units",
    "unit_cost_local", "fx_at_buy",
]

_FY_COLUMNS = [
    "fy", "gross_gains", "gross_losses", "net_gain_pre_discount",
    "discount_eligible_gains", "carried_loss_applied", "loss_carried_forward",
    "est_taxable_gain",
]


def _splits_for_key(corporate_actions, code, exchange) -> list[tuple[date, float]]:
    if corporate_actions is None or corporate_actions.empty:
        return []
    ca = corporate_actions
    sub = ca[(ca["code"] == code) & (ca["exchange"] == exchange)]
    return sorted(
        (pd.to_datetime(r["date"]).date(), float(r["ratio"]))
        for _, r in sub.iterrows()
    )


def _apply_pending_splits(queue: deque, pending: list, up_to: date) -> None:
    """Apply splits with date <= up_to to every parcel acquired before the split.

    ``pending`` is a mutable list of (split_date, ratio); applied entries are
    removed. Units scale by ratio, unit cost divides by ratio (total cost and
    acquisition date preserved).
    """
    while pending and pending[0][0] <= up_to:
        split_date, ratio = pending.pop(0)
        for parcel in queue:
            if parcel["acquired"] < split_date:
                parcel["units"] *= ratio
                parcel["unit_cost_local"] /= ratio
                parcel["fee_per_unit_local"] /= ratio


def compute_realised_gains(
    trades_df: pd.DataFrame,
    fx_provider: Callable[[str, str], float],
    corporate_actions: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Walk trades chronologically, matching sells to buys FIFO.

    fx_provider(currency, 'YYYY-MM-DD') -> AUD value of 1 unit of currency.
    corporate_actions: optional splits (code, exchange, date, ratio) applied to
    open parcels before any disposal that follows the split date (H4).

    Returns (realised_df, open_parcels_df).
    """
    realised_records: list[dict] = []
    open_parcels: dict[tuple, deque] = {}
    pending_splits: dict[tuple, list] = {}

    for _, row in trades_df.iterrows():
        key = (row["code"], row["exchange"])
        currency = EXCHANGE_MAP[row["exchange"]]["currency"]
        date_str = row["date"].strftime("%Y-%m-%d")

        if key not in pending_splits:
            pending_splits[key] = _splits_for_key(
                corporate_actions, row["code"], row["exchange"]
            )
        # Apply any splits that have taken effect on/before this trade date.
        _apply_pending_splits(
            open_parcels.setdefault(key, deque()),
            pending_splits[key], row["date"].date(),
        )

        fees_local = float(row.get("fees", 0.0) or 0.0)

        if row["type"] == "buy":
            fx_at_buy = fx_provider(currency, date_str)
            # Buy fees form part of the cost base, apportioned per-unit so
            # partial FIFO matches carry their fair share.
            fee_per_unit = fees_local / row["quantity"] if row["quantity"] else 0.0
            open_parcels.setdefault(key, deque()).append({
                "acquired": row["date"].date(),
                "units": row["quantity"],
                "unit_cost_local": row["unit_price"],
                "fee_per_unit_local": fee_per_unit,
                "fx_at_buy": fx_at_buy,
            })

        elif row["type"] == "sell":
            fx_at_sell = fx_provider(currency, date_str)
            remaining = row["quantity"]
            # Sell fees reduce proceeds, apportioned per-unit sold.
            sell_fee_per_unit = fees_local / row["quantity"] if row["quantity"] else 0.0
            queue = open_parcels.get(key, deque())

            while remaining > _EPS and queue:
                parcel = queue[0]
                matched = min(parcel["units"], remaining)

                # Cost base = purchase cost + apportioned buy fee (in AUD at buy FX).
                cost_base_aud = (
                    matched * (parcel["unit_cost_local"] + parcel["fee_per_unit_local"])
                    * parcel["fx_at_buy"]
                )
                # Proceeds = sale value less apportioned sell fee (in AUD at sell FX).
                proceeds_aud = (
                    matched * (row["unit_price"] - sell_fee_per_unit) * fx_at_sell
                )
                held_days = (row["date"].date() - parcel["acquired"]).days

                realised_records.append({
                    "code": row["code"],
                    "exchange": row["exchange"],
                    "currency": currency,
                    "acquired": parcel["acquired"],
                    "disposed": row["date"].date(),
                    "units": matched,
                    "unit_cost_local": parcel["unit_cost_local"],
                    "unit_proceeds_local": row["unit_price"],
                    "cost_base_aud": cost_base_aud,
                    "proceeds_aud": proceeds_aud,
                    "gain_aud": proceeds_aud - cost_base_aud,
                    "held_days": held_days,
                    "discount_eligible": _discount_eligible(
                        parcel["acquired"], row["date"].date()
                    ),
                })

                if matched >= parcel["units"] - _EPS:
                    queue.popleft()
                else:
                    parcel["units"] -= matched
                remaining -= matched

            if remaining > _EPS:
                print(
                    f"  WARNING: {row['code']} ({row['exchange']}) sell of "
                    f"{row['quantity']} on {date_str} exceeded available parcels "
                    f"by {remaining:.4f} units"
                )

    # Apply any splits that took effect after the last trade for a key, so the
    # open-parcels output reflects current (post-split) units.
    today = date.today()
    for key, queue in open_parcels.items():
        _apply_pending_splits(queue, pending_splits.get(key, []), today)

    open_records = []
    for (code, exchange), queue in open_parcels.items():
        currency = EXCHANGE_MAP[exchange]["currency"]
        for parcel in queue:
            if parcel["units"] > _EPS:
                open_records.append({
                    "code": code,
                    "exchange": exchange,
                    "currency": currency,
                    "acquired": parcel["acquired"],
                    "units": parcel["units"],
                    "unit_cost_local": parcel["unit_cost_local"],
                    "fx_at_buy": parcel["fx_at_buy"],
                })

    realised_df = (
        pd.DataFrame(realised_records)
        if realised_records else pd.DataFrame(columns=_REALISED_COLUMNS)
    )
    open_parcels_df = (
        pd.DataFrame(open_records)
        if open_records else pd.DataFrame(columns=_OPEN_COLUMNS)
    )
    return realised_df, open_parcels_df


def open_cost_base_aud_as_of(
    trades_df: pd.DataFrame,
    as_of: date,
    fx_provider: Callable[[str, str], float],
    corporate_actions: pd.DataFrame | None = None,
) -> dict[tuple, float]:
    """AUD cost base of open FIFO parcels per (code, exchange, platform) as of a date.

    Each surviving parcel is valued at the FX rate of its OWN buy date (M4),
    not the snapshot-date FX, so historical cost is measured in the AUD
    actually committed. Includes buy fees in the cost base. Splits from
    corporate_actions adjust units/unit-cost while preserving total cost.
    """
    prior = trades_df[trades_df["date"].dt.date <= as_of]
    open_parcels: dict[tuple, deque] = {}

    for _, row in prior.sort_values("date").iterrows():
        key = (row["code"], row["exchange"], row["platform"])
        currency = EXCHANGE_MAP[row["exchange"]]["currency"]
        date_str = row["date"].strftime("%Y-%m-%d")
        fees_local = float(row.get("fees", 0.0) or 0.0)

        if row["type"] == "buy":
            fee_per_unit = fees_local / row["quantity"] if row["quantity"] else 0.0
            open_parcels.setdefault(key, deque()).append({
                "acquired": row["date"].date(),
                "units": row["quantity"],
                "unit_cost_local": row["unit_price"] + fee_per_unit,
                "fx_at_buy": fx_provider(currency, date_str),
            })
        elif row["type"] == "sell":
            remaining = row["quantity"]
            queue = open_parcels.get(key, deque())
            while remaining > _EPS and queue:
                parcel = queue[0]
                matched = min(parcel["units"], remaining)
                if matched >= parcel["units"] - _EPS:
                    queue.popleft()
                else:
                    parcel["units"] -= matched
                remaining -= matched

    if corporate_actions is not None and not corporate_actions.empty:
        _apply_splits_to_parcels(open_parcels, corporate_actions, as_of)

    cost_base: dict[tuple, float] = {}
    for key, queue in open_parcels.items():
        total = sum(
            p["units"] * p["unit_cost_local"] * p["fx_at_buy"]
            for p in queue if p["units"] > _EPS
        )
        if total:
            cost_base[key] = total
    return cost_base


def _apply_splits_to_parcels(open_parcels, corporate_actions, as_of: date) -> None:
    """Adjust open parcels for share splits on or before ``as_of``.

    A split of ratio R multiplies units by R and divides unit cost by R,
    preserving total cost and the original acquisition date. Only splits after
    a parcel's acquisition and on/before as_of apply.
    """
    ca = corporate_actions.copy()
    ca["date"] = pd.to_datetime(ca["date"])
    for (code, exchange), grp in ca.groupby(["code", "exchange"]):
        for _, action in grp.sort_values("date").iterrows():
            split_date = action["date"].date()
            if split_date > as_of:
                continue
            ratio = float(action["ratio"])
            for key, queue in open_parcels.items():
                if key[0] != code or key[1] != exchange:
                    continue
                for parcel in queue:
                    if parcel["acquired"] < split_date:
                        parcel["units"] *= ratio
                        parcel["unit_cost_local"] /= ratio


def _parcel_cgt(
    parcel: dict, units: float, price_local: float, fx_now: float, disposed: date
) -> dict:
    """CGT of disposing ``units`` from one open parcel at ``price_local`` today.

    ``parcel`` carries acquired/unit_cost_local/fx_at_buy (from open_parcels_df).
    Cost base uses the parcel's own buy FX; proceeds use today's FX. Returns the
    per-parcel breakdown including the discount-adjusted taxable gain (50% on
    eligible gains only; losses are shown in full, discount N/A).
    """
    cost_base = units * parcel["unit_cost_local"] * parcel["fx_at_buy"]
    proceeds = units * price_local * fx_now
    gain = proceeds - cost_base
    eligible = _discount_eligible(parcel["acquired"], disposed)
    taxable = gain * 0.5 if (eligible and gain > 0) else gain
    return {
        "acquired": parcel["acquired"],
        "units": units,
        "cost_base_aud": cost_base,
        "proceeds_aud": proceeds,
        "gain_aud": gain,
        "discount_eligible": eligible,
        "taxable_gain_aud": taxable,
    }


def simulate_disposal(
    open_parcels_df: pd.DataFrame,
    code: str,
    exchange: str,
    units: float,
    price_local: float,
    fx_now: float,
    disposed: date | None = None,
) -> dict:
    """"If sold today" CGT for disposing ``units`` of (code, exchange).

    Compares two parcel-selection strategies over the open parcels:
      * ``fifo``     — oldest parcels first (what the tracker actually books).
      * ``min_tax``  — parcels ordered to minimise the estimated taxable gain
                       (largest per-unit taxable gain last), which tends to
                       realise losses and discount-eligible parcels first.

    Returns {'fifo': {...}, 'min_tax': {...}} where each is
    {parcels: [per-parcel dicts], total_gain_aud, total_taxable_gain_aud,
    units_disposed}. If fewer units are available than requested, only what's on
    hand is disposed (and ``units_disposed`` reflects that).
    """
    disposed = disposed or date.today()
    sub = open_parcels_df[
        (open_parcels_df["code"] == code) & (open_parcels_df["exchange"] == exchange)
    ]
    parcels = [
        {
            "acquired": r["acquired"],
            "units": float(r["units"]),
            "unit_cost_local": float(r["unit_cost_local"]),
            "fx_at_buy": float(r["fx_at_buy"]),
        }
        for _, r in sub.iterrows()
    ]

    def _run(ordered: list[dict]) -> dict:
        remaining = units
        rows = []
        for p in ordered:
            if remaining <= _EPS:
                break
            take = min(p["units"], remaining)
            rows.append(_parcel_cgt(p, take, price_local, fx_now, disposed))
            remaining -= take
        return {
            "parcels": rows,
            "total_gain_aud": sum(r["gain_aud"] for r in rows),
            "total_taxable_gain_aud": sum(r["taxable_gain_aud"] for r in rows),
            "units_disposed": units - remaining,
        }

    fifo_order = sorted(parcels, key=lambda p: p["acquired"])

    # Min-tax: dispose the parcels with the LOWEST per-unit taxable gain first
    # (losses and discount-eligible winners), leaving high-tax parcels on hand.
    def _per_unit_taxable(p: dict) -> float:
        one = _parcel_cgt(p, 1.0, price_local, fx_now, disposed)
        return one["taxable_gain_aud"]

    min_tax_order = sorted(parcels, key=_per_unit_taxable)

    return {"fifo": _run(fifo_order), "min_tax": _run(min_tax_order)}


def _fy_label(d: date) -> str:
    """Australian FY: 1 July - 30 June, labelled FYxx/yy."""
    if d.month >= 7:
        start, end = d.year, d.year + 1
    else:
        start, end = d.year - 1, d.year
    return f"FY{str(start)[-2:]}/{str(end)[-2:]}"


def _fy_sort_key(fy_label: str) -> int:
    """Sort FYxx/yy chronologically. FY99/00 → FY00/01 must order correctly,
    so key off the (2-digit) start year mapped into a 100-year window."""
    start = int(fy_label[2:4])
    # Portfolio-era assumption: 2-digit years 70-99 → 1970-1999, 00-69 → 2000-2069.
    return start + (1900 if start >= 70 else 2000)


def summarise_by_fy(realised_df: pd.DataFrame) -> pd.DataFrame:
    """Group realised parcels by Australian financial year with ATO-correct
    discount ordering and capital-loss carry-forward.

    Method (per ATO): current-year and carried-forward capital losses reduce
    *gross* capital gains before the 50% discount is applied, and are applied
    against non-discountable gains first (the taxpayer-optimal ordering, since
    a dollar of loss saves a full dollar against a non-discount gain but only
    50c against a discount gain). Any loss left over after all gains are
    exhausted carries forward to the next FY. est_taxable_gain is never
    negative. Rough estimate only — not a tax return.
    """
    if realised_df.empty:
        return pd.DataFrame(columns=_FY_COLUMNS)

    # H7: CGT with unknown FX is meaningless — fail loudly rather than
    # silently summing NaN into a wrong tax figure.
    if realised_df["gain_aud"].isna().any():
        raise ValueError(
            "summarise_by_fy: NaN gain_aud present — missing FX for one or more "
            "disposals. Cannot compute CGT with unknown exchange rates."
        )

    df = realised_df.copy()
    df["fy"] = df["disposed"].map(_fy_label)

    rows = []
    carried_loss = 0.0  # ≥ 0, magnitude of losses carried into the current FY
    for fy in sorted(df["fy"].unique(), key=_fy_sort_key):
        group = df[df["fy"] == fy]
        gains = group.loc[group["gain_aud"] > 0, "gain_aud"].sum()
        losses = group.loc[group["gain_aud"] < 0, "gain_aud"].sum()  # ≤ 0
        net_pre_discount = group["gain_aud"].sum()
        disc_eligible_gains = group.loc[
            group["discount_eligible"] & (group["gain_aud"] > 0), "gain_aud"
        ].sum()
        non_disc_gains = gains - disc_eligible_gains

        # Total loss pool available this FY: this year's losses + carry-forward.
        current_year_loss = -losses  # ≥ 0
        loss_pool = current_year_loss + carried_loss

        # H1: apply losses to non-discount gains first, then discount gains.
        remaining_loss = loss_pool
        non_disc_after = max(0.0, non_disc_gains - remaining_loss)
        remaining_loss = max(0.0, remaining_loss - non_disc_gains)
        disc_after = max(0.0, disc_eligible_gains - remaining_loss)
        remaining_loss = max(0.0, remaining_loss - disc_eligible_gains)

        est_taxable = non_disc_after + 0.5 * disc_after
        loss_carried_forward = remaining_loss  # unused loss → next FY

        # Of the loss absorbed this FY, current-year losses are used first;
        # the remainder is drawn from the carried-in balance.
        total_absorbed = loss_pool - loss_carried_forward
        carried_applied = max(0.0, total_absorbed - current_year_loss)

        carried_loss = loss_carried_forward

        rows.append({
            "fy": fy,
            "gross_gains": gains,
            "gross_losses": losses,
            "net_gain_pre_discount": net_pre_discount,
            "discount_eligible_gains": disc_eligible_gains,
            "carried_loss_applied": carried_applied,
            "loss_carried_forward": loss_carried_forward,
            "est_taxable_gain": est_taxable,
        })

    return pd.DataFrame(rows).reset_index(drop=True)


def fy_working_steps(fy_summary_df: pd.DataFrame) -> list[dict]:
    """Reconstruct the per-FY arithmetic behind summarise_by_fy, one dict per
    row (same order), so the dashboard can show the working.

    The carried-in loss for FY n is the previous row's loss_carried_forward,
    so rows must be walked in FY order — which is how summarise_by_fy emits
    them. Each step is reconciled against the row's own est_taxable_gain and
    loss_carried_forward; any drift means the two code paths have diverged and
    raises rather than display working that disagrees with the summary.
    """
    steps: list[dict] = []
    carried_in = 0.0
    for _, row in fy_summary_df.iterrows():
        gains = float(row["gross_gains"])
        disc_gains = float(row["discount_eligible_gains"])
        non_disc_gains = gains - disc_gains
        current_year_loss = -float(row["gross_losses"])  # ≥ 0
        loss_pool = current_year_loss + carried_in

        loss_to_non_disc = min(non_disc_gains, loss_pool)
        non_disc_after = non_disc_gains - loss_to_non_disc
        remaining = loss_pool - loss_to_non_disc
        loss_to_disc = min(disc_gains, remaining)
        disc_after = disc_gains - loss_to_disc
        remaining -= loss_to_disc

        discount_amount = 0.5 * disc_after
        est_taxable = non_disc_after + discount_amount

        for col, ours in (("est_taxable_gain", est_taxable),
                          ("loss_carried_forward", remaining)):
            if abs(float(row[col]) - ours) > 0.01:
                raise ValueError(
                    f"fy_working_steps: reconstructed {col} ({ours:,.2f}) does "
                    f"not match summarise_by_fy ({float(row[col]):,.2f}) for "
                    f"{row['fy']} — display logic has diverged from the "
                    f"calculation."
                )

        steps.append({
            "fy": row["fy"],
            "gross_gains": gains,
            "disc_gains": disc_gains,
            "non_disc_gains": non_disc_gains,
            "current_year_loss": current_year_loss,
            "carried_in": carried_in,
            "loss_pool": loss_pool,
            "loss_to_non_disc": loss_to_non_disc,
            "non_disc_after": non_disc_after,
            "loss_to_disc": loss_to_disc,
            "disc_after": disc_after,
            "discount_amount": discount_amount,
            "est_taxable": est_taxable,
            "loss_carried_forward": remaining,
        })
        carried_in = remaining

    return steps
