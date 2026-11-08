"""Target allocation: strategy buckets, targets and holding assignments.

Loaded from an optional ``data/allocation.yaml``. The dashboard's Targets
section edits the same config in the browser and downloads it back as YAML
(see ``import_downloaded``), so the file stays the saved source of truth.

Holdings are assigned to buckets manually, one bucket per ticker ``code``
(the same grouping as ``aggregate_by_code``). Nothing is ever guessed: an
unassigned holding is left out of the target maths and flagged instead.

The interactive rebalancing strategies live in the dashboard's JavaScript;
this module supplies the config, the actual-vs-target summary used by the
brief and CLI, and the per-parcel CGT inputs the page needs to price sells.
"""
import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

from src.cgt import _discount_eligible, _parcel_cgt

DEFAULT_BUCKETS = [
    ("core", "Core", 50.0),
    ("quality_growth", "Active / Quality Growth", 25.0),
    ("high_conviction", "High Conviction", 15.0),
    ("speculative", "Speculative", 10.0),
]
DEFAULT_TOLERANCE_ABS_PP = 5.0
DEFAULT_TOLERANCE_REL_PCT = 25.0

_EPS = 1e-9


@dataclass
class Bucket:
    id: str
    label: str
    target: float  # percent of the assigned portfolio, 0–100


@dataclass
class AllocationConfig:
    buckets: list[Bucket] = field(default_factory=list)
    # Tolerance band either side of each target: the tighter of an absolute
    # number of percentage points and a share of the target itself (the
    # "5/25 rule"), so a 10% bucket isn't allowed to drift to 5–15%.
    tolerance_abs_pp: float = DEFAULT_TOLERANCE_ABS_PP
    tolerance_rel_pct: float = DEFAULT_TOLERANCE_REL_PCT
    holdings: dict[str, str] = field(default_factory=dict)  # code → bucket id

    def to_dict(self) -> dict:
        return {
            "buckets": [
                {"id": b.id, "label": b.label, "target": float(b.target)}
                for b in self.buckets
            ],
            "tolerance": {
                "absolute_pp": float(self.tolerance_abs_pp),
                "relative_pct": float(self.tolerance_rel_pct),
            },
            "holdings": dict(sorted(self.holdings.items())),
        }


def default_config() -> AllocationConfig:
    return AllocationConfig(
        buckets=[Bucket(i, l, t) for i, l, t in DEFAULT_BUCKETS]
    )


def _raw_holdings(text: str) -> list[tuple[str, str]] | None:
    """``holdings:`` entries exactly as written, read from the YAML node tree.

    ``yaml.safe_load`` would turn unquoted tickers like ``NO`` or ``ON`` into
    booleans and ``0700`` (an HKEX code) into an octal int. Reading the raw
    scalar text sidesteps every implicit type, quoted or not.
    """
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    if not isinstance(root, yaml.MappingNode):
        return None
    for key_node, value_node in root.value:
        if key_node.value != "holdings":
            continue
        if not isinstance(value_node, yaml.MappingNode):
            return []
        return [
            (str(k.value).strip(), str(v.value).strip())
            for k, v in value_node.value
            if isinstance(k, yaml.ScalarNode) and isinstance(v, yaml.ScalarNode)
        ]
    return None


def load_allocation(path: str | Path) -> tuple[AllocationConfig, list[str]]:
    """Load allocation.yaml → (config, warnings). Absent file → defaults.

    Structural problems (not a mapping, a bucket without an id) raise
    ValueError. Softer problems — targets not summing to 100, a holding
    assigned to a bucket that doesn't exist — are returned as warnings; the
    offending holding is then treated as unassigned.
    """
    path = Path(path)
    if not path.exists():
        return default_config(), []
    return parse_allocation(path.read_text(), path)


def parse_allocation(
    text: str, source: str | Path = "allocation.yaml"
) -> tuple[AllocationConfig, list[str]]:
    """load_allocation for YAML text (an uploaded file in the browser edition).
    ``source`` only names the file in messages."""
    raw = yaml.safe_load(text) or {}
    return _from_raw(raw, _raw_holdings(text) or [], Path(source))


def allocation_from_dict(d: dict) -> tuple[AllocationConfig, list[str]]:
    """load_allocation for a ``to_dict()``-shaped mapping (the browser edition
    stores the config as JSON, where tickers are already strings)."""
    holdings = d.get("holdings") if isinstance(d, dict) else None
    pairs = [
        (str(k).strip(), str(v).strip())
        for k, v in (holdings.items() if isinstance(holdings, dict) else [])
    ]
    return _from_raw(d, pairs, Path("allocation.yaml"))


def _from_raw(
    raw, holdings_raw: list[tuple[str, str]], path: Path
) -> tuple[AllocationConfig, list[str]]:
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a YAML mapping at the top level.")
    warnings: list[str] = []

    buckets: list[Bucket] = []
    raw_buckets = raw.get("buckets")
    if not raw_buckets:
        buckets = default_config().buckets
    else:
        if not isinstance(raw_buckets, list):
            raise ValueError(f"{path}: 'buckets' must be a list.")
        seen: set[str] = set()
        for b in raw_buckets:
            if not isinstance(b, dict) or not str(b.get("id") or "").strip():
                raise ValueError(f"{path}: every bucket needs an 'id'.")
            bid = str(b["id"]).strip()
            if bid in seen:
                raise ValueError(f"{path}: duplicate bucket id '{bid}'.")
            seen.add(bid)
            target = float(b.get("target", 0) or 0)
            if target < 0:
                raise ValueError(f"{path}: bucket '{bid}' has a negative target.")
            label = str(b.get("label") or bid).strip()
            buckets.append(Bucket(bid, label, target))

    total = sum(b.target for b in buckets)
    if abs(total - 100.0) > 0.01:
        warnings.append(
            f"Allocation targets sum to {total:g}%, not 100% — fix them in "
            f"{path.name} or the dashboard's Targets section."
        )

    tol_raw = raw.get("tolerance")
    abs_pp, rel_pct = DEFAULT_TOLERANCE_ABS_PP, DEFAULT_TOLERANCE_REL_PCT
    if isinstance(tol_raw, (int, float)):
        abs_pp = float(tol_raw)
    elif isinstance(tol_raw, dict):
        abs_pp = float(tol_raw.get("absolute_pp", abs_pp))
        rel_pct = float(tol_raw.get("relative_pct", rel_pct))

    bucket_ids = {b.id for b in buckets}
    holdings: dict[str, str] = {}
    for code, bid in holdings_raw:
        if not code or not bid:
            continue
        if bid not in bucket_ids:
            warnings.append(
                f"{code} is assigned to unknown bucket '{bid}' in {path.name} "
                f"— treating it as unassigned."
            )
            continue
        holdings[code] = bid

    return AllocationConfig(buckets, abs_pp, rel_pct, holdings), warnings


def fingerprint(cfg: AllocationConfig) -> str:
    """Stable hash of a config, so the dashboard can tell whether a browser
    draft was based on the file as it is now or on an older version."""
    canon = json.dumps(cfg.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canon.encode()).hexdigest()[:16]


def band(bucket: Bucket, cfg: AllocationConfig) -> float:
    """Tolerance either side of a bucket's target, in percentage points."""
    return min(cfg.tolerance_abs_pp, cfg.tolerance_rel_pct / 100.0 * bucket.target)


def bucket_summary(by_code: pd.DataFrame, cfg: AllocationConfig) -> dict:
    """Actual vs target per bucket.

    Percentages are of the *assigned* total — unassigned holdings are
    reported separately rather than silently counted against any target.
    """
    df = by_code[["code", "market_value_aud"]].copy()
    df["market_value_aud"] = df["market_value_aud"].fillna(0.0)
    df["bucket"] = df["code"].map(cfg.holdings)

    assigned = df[df["bucket"].notna()]
    unassigned = df[df["bucket"].isna()].sort_values("market_value_aud", ascending=False)
    assigned_total = float(assigned["market_value_aud"].sum())
    portfolio_total = float(df["market_value_aud"].sum())

    rows = []
    for b in cfg.buckets:
        members = assigned[assigned["bucket"] == b.id]
        value = float(members["market_value_aud"].sum())
        actual = value / assigned_total * 100.0 if assigned_total > 0 else 0.0
        bp = band(b, cfg)
        lower, upper = max(0.0, b.target - bp), b.target + bp
        if assigned_total <= 0:
            status = "on"
        elif actual > upper + _EPS:
            status = "over"
        elif actual < lower - _EPS:
            status = "under"
        else:
            status = "on"
        rows.append({
            "id": b.id,
            "label": b.label,
            "target_pct": b.target,
            "value": value,
            "actual_pct": actual,
            "drift_pp": actual - b.target,
            "drift_aud": value - b.target / 100.0 * assigned_total,
            "band_pp": bp,
            "lower_pct": lower,
            "upper_pct": upper,
            "status": status,
            "codes": members.sort_values("market_value_aud", ascending=False)["code"].tolist(),
        })

    return {
        "buckets": pd.DataFrame(rows),
        "assigned_total": assigned_total,
        "portfolio_total": portfolio_total,
        "unassigned": unassigned[["code", "market_value_aud"]].reset_index(drop=True),
        "unassigned_value": float(unassigned["market_value_aud"].sum()),
    }


def waterfill(values: list[float], targets: list[float], money: float) -> list[float]:
    """Split ``money`` across buckets so the most underweight fill first.

    Buckets are ranked by value ÷ target (how far below its share each one
    is); new money lifts the lowest to the next, then both together, and so
    on — i.e. find the level L where Σ max(0, L·t − V) = money. Every bucket
    that receives money ends at L·t: on target for a portfolio worth L.
    Routing is path-independent (filling month by month lands in the same
    place as one lump), which is what makes "months to target" well defined.
    The dashboard's JS implements the same routine; keep them in step.
    """
    buys = [0.0] * len(values)
    if money <= 0:
        return buys
    idx = sorted((i for i, t in enumerate(targets) if t > 0),
                 key=lambda i: values[i] / targets[i])
    if not idx:
        return buys
    sum_v = sum_t = level = 0.0
    k = 0
    for k, i in enumerate(idx):
        sum_v += values[i]
        sum_t += targets[i]
        level = (money + sum_v) / sum_t
        nxt = values[idx[k + 1]] / targets[idx[k + 1]] if k + 1 < len(idx) else float("inf")
        if level <= nxt:
            break
    for i in idx[:k + 1]:
        buys[i] = max(0.0, level * targets[i] - values[i])
    return buys


def _in_band(values: list[float], lowers: list[float], uppers: list[float]) -> bool:
    total = sum(values)
    if total <= 0:
        return True
    return all(
        l / 100.0 - 1e-9 <= v / total <= u / 100.0 + 1e-9
        for v, l, u in zip(values, lowers, uppers)
    )


def contribution_to_target(summary: dict) -> float | None:
    """New money needed to reach every target exactly without selling, or
    None if impossible (a bucket with a 0% target still holds shares).

    The binding bucket is the most overweight one: it's on target once the
    portfolio is worth value ÷ target, and water-filling puts every other
    bucket on target at exactly that total.
    """
    b = summary["buckets"]
    total = summary["assigned_total"]
    if b.empty or total <= 0:
        return 0.0
    needed = total
    for v, t in zip(b["value"], b["target_pct"]):
        if v > _EPS:
            if t <= 0:
                return None
            needed = max(needed, v / (t / 100.0))
    return max(0.0, needed - total)


def contribution_to_band(summary: dict) -> float | None:
    """New money (water-filled, no selling) that brings every bucket inside
    its tolerance band. Found by bisection between 0 and the full
    contribution-to-target, which is always in band."""
    b = summary["buckets"]
    hi = contribution_to_target(summary)
    if hi is None or b.empty:
        return hi
    values = b["value"].tolist()
    targets = b["target_pct"].tolist()
    lowers, uppers = b["lower_pct"].tolist(), b["upper_pct"].tolist()

    def ok(money: float) -> bool:
        buys = waterfill(values, targets, money)
        return _in_band([v + x for v, x in zip(values, buys)], lowers, uppers)

    if ok(0.0):
        return 0.0
    lo = 0.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if ok(mid):
            hi = mid
        else:
            lo = mid
    return hi


def parcels_payload(
    open_parcels_df: pd.DataFrame, enriched: pd.DataFrame, today: date | None = None
) -> dict[str, list[dict]]:
    """Per-unit CGT inputs for every open parcel, grouped by ticker code.

    The dashboard scales these to price a sell of any size: disposing ``n``
    units of a parcel realises ``n × gain_per_unit`` (CGT is linear in units),
    with the 50% discount and loss netting applied page-side. Parcels of the
    same code on different exchanges are merged; each is priced at its own
    exchange's current price and today's FX. Oldest first (FIFO order).
    """
    today = today or date.today()
    if open_parcels_df is None or open_parcels_df.empty:
        return {}

    quotes: dict[tuple[str, str], tuple[float, float]] = {}
    for _, r in enriched.iterrows():
        price, fx_now = r.get("current_price_local"), r.get("fx_to_aud")
        if pd.notna(price) and pd.notna(fx_now):
            quotes.setdefault((r["code"], r["exchange"]), (float(price), float(fx_now)))

    out: dict[str, list[dict]] = {}
    for _, p in open_parcels_df.sort_values("acquired").iterrows():
        quote = quotes.get((p["code"], p["exchange"]))
        if quote is None or float(p["units"]) <= _EPS:
            continue
        one = _parcel_cgt(
            {
                "acquired": p["acquired"],
                "unit_cost_local": float(p["unit_cost_local"]),
                "fx_at_buy": float(p["fx_at_buy"]),
            },
            1.0, quote[0], quote[1], today,
        )
        out.setdefault(str(p["code"]), []).append({
            "exchange": p["exchange"],
            "acquired": pd.Timestamp(p["acquired"]).date().isoformat(),
            "units": float(p["units"]),
            "value_per_unit": one["proceeds_aud"],
            "gain_per_unit": one["gain_aud"],
            "discount_eligible": bool(_discount_eligible(p["acquired"], today)),
        })
    return out


def import_downloaded(
    data_path: str | Path, downloads_dir: str | Path, pattern: str
) -> Path | None:
    """Adopt the newest dashboard download matching ``pattern`` if it is newer
    than ``data_path``. Returns the imported file, else None.

    The download is validated first (an unreadable file is skipped with the
    error raised to the caller), and the file it replaces is kept as
    ``<name>.bak``. ``copy2`` preserves the download's mtime, so the same
    download is never imported twice, and a later hand edit of the data file
    makes it newer than the download again.
    """
    data_path, downloads_dir = Path(data_path), Path(downloads_dir)
    if not downloads_dir.is_dir():
        return None
    candidates = sorted(downloads_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
    if not candidates:
        return None
    newest = candidates[-1]
    if data_path.exists() and newest.stat().st_mtime <= data_path.stat().st_mtime:
        return None

    load_allocation(newest)  # raises ValueError on a malformed file
    data_path.parent.mkdir(parents=True, exist_ok=True)
    if data_path.exists():
        shutil.copy2(data_path, data_path.with_name(data_path.name + ".bak"))
    shutil.copy2(newest, data_path)
    return newest
