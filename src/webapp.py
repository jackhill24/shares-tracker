"""The browser edition's entry points, called from web/py-worker.js in Pyodide.

Each takes and returns JSON text, so the JavaScript side never handles Python
objects. A result is ``{"ok": true, ...}``, or ``{"ok": false, "errors":
[...]}`` for a problem the user can fix (a bad CSV, a bad allocation file),
worded for someone who has never seen a traceback. Anything else is a bug and
raises.

The page drives a build in two calls, because prices are fetched by the page
(through the Worker) in between:

  requirements(csv, settings)  → the trades check out; here's what to fetch
  build(csv, settings, targets, fetched, ...)  → the dashboard's HTML

Lines the pipeline prints with "WARNING" (an ambiguous date column, a price
that couldn't be found) come back in ``warnings``, since there's no terminal.
"""
import contextlib
import io
import json
import re
from pathlib import Path

import pandas as pd

from src import market_data
from src.allocation import allocation_from_dict, default_config, parse_allocation
from src.config import config_from_dict
from src.db import init_db
from src.ingest import CORPORATE_ACTIONS_COLUMNS, load_trades, validate_trades
from src.pipeline import NoOpenPositions, build_dashboard, market_requirements

DEMO_TRADES = Path("data/demo_trades.csv")
DEMO_ALLOCATION = Path("data/demo_allocation.yaml")

NOTHING_HELD = (
    "These trades leave nothing held today, so there's no dashboard to show. "
    "Check that every holding you still own has its buys in the file."
)


class UserError(ValueError):
    """Something the user can fix, as messages to show them."""

    def __init__(self, messages: list[str]):
        super().__init__("\n".join(messages))
        self.messages = messages


def _reply(fn):
    """Run ``fn`` → JSON reply, turning UserError into ``ok: false``."""
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            result = fn()
    except UserError as e:
        return json.dumps({"ok": False, "errors": e.messages, "warnings": _warnings(out)})
    return json.dumps({"ok": True, **result, "warnings": _warnings(out)})


def _warnings(out: io.StringIO) -> list[str]:
    seen: list[str] = []
    for line in out.getvalue().splitlines():
        text = line.strip()
        if "WARNING" in text:
            text = text.split("WARNING:", 1)[-1].strip()
            if text not in seen:
                seen.append(text)
    return seen


# --- Trades -------------------------------------------------------------------

def _sheet_row(index: int) -> int:
    """A 0-based data row → its row number in a spreadsheet (header is row 1)."""
    return index + 2


def _load_messages(message: str) -> list[str]:
    """load_trades' messages, with pandas row positions as spreadsheet rows."""
    message = message.replace("trades.csv: ", "").replace("trades.csv ", "The file ")
    message = re.sub(r"\brow (\d+)", lambda m: f"row {_sheet_row(int(m.group(1)))}", message)
    message = re.sub(r"\brow\(s\) ([\d, ]+)", lambda m: "row(s) " + ", ".join(
        str(_sheet_row(int(n))) for n in re.findall(r"\d+", m.group(1))), message)
    message = re.sub(r"at position (\d+)",
                     lambda m: f"on row {_sheet_row(int(m.group(1)))}", message)
    return [message[:1].upper() + message[1:]]


def _sorted_to_sheet_rows(csv_text: str) -> dict[int, int]:
    """load_trades sorts by date, so validate_trades' "Row N" is a position in
    date order. Map it back to the file by loading a copy that carries each
    row's spreadsheet row number as an extra column (the sort ignores it)."""
    raw = pd.read_csv(io.StringIO(csv_text), dtype=str)
    raw["_sheet_row"] = [_sheet_row(i) for i in range(len(raw))]
    again = load_trades(io.StringIO(raw.to_csv(index=False)))
    return {int(i): int(r) for i, r in again["_sheet_row"].items()}


def _validation_messages(message: str, csv_text: str) -> list[str]:
    """validate_trades' messages with file row numbers, in file order."""
    rows = _sorted_to_sheet_rows(csv_text)
    found = []
    for ln in message.splitlines():
        m = re.match(r"\s*- Row (\d+): (.*)", ln)
        if m:
            row = rows.get(int(m.group(1)))
            found.append((row or 0, f"Row {row or '?'}: {m.group(2)}"))
    return [text for _, text in sorted(found, key=lambda f: f[0])]


def read_trades(csv_text: str) -> pd.DataFrame:
    """Load and validate an uploaded trades CSV, or raise UserError."""
    if not csv_text.strip():
        raise UserError(["The file is empty."])
    try:
        trades = load_trades(io.StringIO(csv_text))
    except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeError) as e:
        raise UserError([f"This doesn't read as a CSV file ({e})."]) from e
    except ValueError as e:
        raise UserError(_load_messages(str(e))) from e
    except (AttributeError, TypeError, KeyError) as e:
        # e.g. a column of numbers where text was expected (.str on floats).
        raise UserError([
            "Some columns don't hold what they should: code, exchange, type "
            f"and platform must be text, quantity and unit_price numbers ({e})."
        ]) from e
    if trades.empty:
        raise UserError(["The file has a header row but no trades."])
    try:
        validate_trades(trades)
    except ValueError as e:
        raise UserError(_validation_messages(str(e), csv_text)) from e
    return trades


def _no_corporate_actions() -> pd.DataFrame:
    # Share splits aren't supported in the web edition yet.
    return pd.DataFrame(columns=sorted(CORPORATE_ACTIONS_COLUMNS))


# --- Settings and targets ---------------------------------------------------------

def _config(settings_json: str | None):
    try:
        return config_from_dict((json.loads(settings_json) if settings_json else None) or {})
    except (ValueError, KeyError, TypeError) as e:
        raise UserError([f"The saved settings aren't valid ({e}). Reset them in Settings."]) from e


def _allocation(targets_json: str | None):
    if not targets_json:
        return default_config(), []
    d = json.loads(targets_json)
    if d is None:
        return default_config(), []
    try:
        return allocation_from_dict(d)
    except ValueError as e:
        return default_config(), [f"{e} Using default buckets."]


# --- Entry points ---------------------------------------------------------------

def demo() -> str:
    """The bundled demo portfolio: its trades CSV and target allocation."""
    def run():
        cfg, _ = parse_allocation(DEMO_ALLOCATION.read_text(encoding="utf-8"), DEMO_ALLOCATION.name)
        return {"csv": DEMO_TRADES.read_text(encoding="utf-8"), "targets": cfg.to_dict()}
    return _reply(run)


def requirements(csv_text: str, settings_json: str | None = None) -> str:
    """Check the trades → ``{"ok": true, "requirements": market_requirements}``."""
    def run():
        trades = read_trades(csv_text)
        req = market_requirements(trades, _no_corporate_actions(), _config(settings_json))
        if not req["holdings"]:
            raise UserError([NOTHING_HELD])
        return {"requirements": req, "trades": len(trades)}
    return _reply(run)


def build(
    csv_text: str,
    settings_json: str | None,
    targets_json: str | None,
    fetched_json: str,
    storage_key: str,
    title: str,
    download_name: str = "shares-tracker-allocation.yaml",
) -> str:
    """Build the dashboard from fetched prices → ``{"ok": true, "html", "summary"}``.

    ``fetched_json`` is the list seed_cache takes: each requirements request
    plus its ``closes`` and ``last``. ``targets_json`` is the allocation config
    as the dashboard posts it (to_dict's shape), or null for default buckets.
    """
    def run():
        trades = read_trades(csv_text)
        alloc_cfg, alloc_warnings = _allocation(targets_json)
        conn = init_db(":memory:")
        quotes, fx = market_data.seed_cache(conn, json.loads(fetched_json))
        market_data.set_provider("none")
        try:
            html, summary = build_dashboard(
                trades, _no_corporate_actions(), alloc_cfg, _config(settings_json),
                conn, quotes, fx,
                public=True, title=title, storage_key=storage_key, web=True,
                allocation_warnings=alloc_warnings, allocation_download=download_name,
            )
        except NoOpenPositions as e:
            raise UserError([NOTHING_HELD]) from e
        finally:
            conn.close()
        return {"html": html, "summary": summary}
    return _reply(run)


def parse_targets(yaml_text: str) -> str:
    """An imported allocation YAML → ``{"ok": true, "targets": to_dict()}``."""
    def run():
        try:
            cfg, warnings = parse_allocation(yaml_text, "the imported file")
        except Exception as e:  # yaml.YAMLError and friends
            raise UserError([f"Couldn't read that YAML file: {e}"]) from e
        for w in warnings:
            print(f"  WARNING: {w}")
        return {"targets": cfg.to_dict()}
    return _reply(run)
