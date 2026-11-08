import argparse
import sys
import webbrowser
from pathlib import Path

import pandas as pd

from src.ingest import (
    load_trades,
    validate_trades,
    load_corporate_actions,
    load_franking,
)
from src.market_data import get_current_prices, get_fx_rates_to_aud, get_sectors
from src.db import init_db
from src.config import load_config
from src.allocation import default_config, import_downloaded, load_allocation
from src.pipeline import NoOpenPositions, build_dashboard, market_requirements

TRADES_PATH = Path("data/trades.csv")
CORPORATE_ACTIONS_PATH = Path("data/corporate_actions.csv")
FRANKING_PATH = Path("data/franking.csv")
CONFIG_PATH = Path("config.yaml")
OUTPUT_PATH = Path("output/portfolio.html")
DB_PATH = Path("portfolio.db")
ALLOCATION_PATH = Path("data/allocation.yaml")

# The dashboard's Targets section can't write files, so "Save to file"
# downloads the allocation YAML under this name; the next run adopts the newest
# such download from ~/Downloads if it's newer than the data file.
DOWNLOADS_DIR = Path.home() / "Downloads"
ALLOCATION_DOWNLOAD = "shares-tracker-allocation.yaml"

# Demo mode (--demo): a fictional portfolio for showing the tool around without
# revealing anything personal. Fully separate trades CSV, database and output
# file, so it never touches (or leaks) your real data.
DEMO_TRADES_PATH = Path("data/demo_trades.csv")
DEMO_OUTPUT_PATH = Path("output/demo_portfolio.html")
DEMO_DB_PATH = Path("demo_portfolio.db")
DEMO_ALLOCATION_PATH = Path("data/demo_allocation.yaml")
DEMO_ALLOCATION_DOWNLOAD = "shares-tracker-demo-allocation.yaml"

# The public edition — the copy shared with friends — shows only the core
# dashboard: summary, holdings map, targets (assignment only, no rebalancing
# scenarios), goal planner, net-worth history, performance and risk metrics.
# No CGT, dividend income, if-sold-today or brief. scripts/export_public.py
# sets this to True in the exported copy; --public previews it from here.
PUBLIC_EDITION = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Shares tracker dashboard")
    parser.add_argument(
        "--demo", action="store_true",
        help="run against the bundled fictional demo portfolio "
             "(separate CSV, database and output — your data is untouched)",
    )
    if not PUBLIC_EDITION:
        parser.add_argument(
            "--public", action="store_true",
            help="preview the public edition (core dashboard only), written "
                 "alongside the full dashboard as *_public.html",
        )
    args = parser.parse_args()
    public = PUBLIC_EDITION or args.public

    if args.demo:
        trades_path, db_path, output_path = DEMO_TRADES_PATH, DEMO_DB_PATH, DEMO_OUTPUT_PATH
        allocation_path, allocation_download = DEMO_ALLOCATION_PATH, DEMO_ALLOCATION_DOWNLOAD
        title = "Demo Portfolio"
        print("=== Shares Tracker (DEMO portfolio) ===\n")
    else:
        trades_path, db_path, output_path = TRADES_PATH, DB_PATH, OUTPUT_PATH
        allocation_path, allocation_download = ALLOCATION_PATH, ALLOCATION_DOWNLOAD
        title = "Portfolio Dashboard"
        print("=== Shares Tracker ===\n")
    if public and not PUBLIC_EDITION:
        # A preview from the private repo mustn't overwrite the full dashboard.
        output_path = output_path.with_name(output_path.stem + "_public.html")
        print("(previewing the public edition)\n")

    # 0. Load configuration (benchmarks etc.). Absent file → sensible defaults.
    config = load_config(CONFIG_PATH)

    # 1. Load and validate trades
    print(f"Loading trades from {trades_path}...")
    try:
        trades = load_trades(trades_path)
    except FileNotFoundError:
        print(f"ERROR: {trades_path} not found. Add your trades CSV and re-run.")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR loading trades: {e}")
        sys.exit(1)

    print(f"  {len(trades)} trade rows loaded.")

    try:
        validate_trades(trades)
    except ValueError as e:
        print(f"\n{e}")
        sys.exit(1)

    print("  Validation passed.")

    # Optional corporate actions (share splits). Absent → no adjustment.
    # Demo mode skips the personal CSVs entirely — they belong to *your* trades.
    corporate_actions = load_corporate_actions(
        Path("data/demo_corporate_actions.csv") if args.demo else CORPORATE_ACTIONS_PATH
    )
    if not corporate_actions.empty:
        print(f"  {len(corporate_actions)} corporate action(s) loaded.")

    # Optional franking overrides for ASX dividends (Phase 5). Absent → none.
    # Only dividend income uses it, which the public edition doesn't show.
    franking = pd.DataFrame()
    if not public:
        franking = load_franking(
            Path("data/demo_franking.csv") if args.demo else FRANKING_PATH
        )
        if not franking.empty:
            print(f"  {len(franking)} franking override(s) loaded.")

    # 2. Target allocation: adopt a newer "Save to file" download, then load.
    try:
        imported = import_downloaded(
            allocation_path, DOWNLOADS_DIR,
            Path(allocation_download).stem + "*.yaml",
        )
        if imported is not None:
            print(f"  Imported {imported} → {allocation_path} (previous copy kept as .bak).")
    except (ValueError, OSError) as e:
        print(f"  WARNING: couldn't import the downloaded allocation file: {e}")
    try:
        alloc_cfg, alloc_warnings = load_allocation(allocation_path)
    except ValueError as e:
        alloc_cfg, alloc_warnings = default_config(), [f"{e} Using default buckets."]
    for w in alloc_warnings:
        print(f"  WARNING: {w}")

    # 3. Live prices and FX for what's held (M6: one batch per run). Price
    # history is fetched by the pipeline itself, as it reads the cache.
    needs = market_requirements(trades, corporate_actions, config)
    if not needs["holdings"]:
        print("No open positions found. Check your trades CSV.")
        sys.exit(0)
    tickers = list(needs["holdings"])
    print(f"\nFetching prices for {len(tickers)} tickers...")
    prices = get_current_prices(tickers)
    currencies = {"AUD", *needs["currencies"]}
    print(f"Fetching FX rates for currencies: {', '.join(sorted(currencies))}...")
    fx = get_fx_rates_to_aud(currencies)

    # 4. Database (price cache + snapshots), and sectors for "By Sector".
    print("\nInitialising database...")
    conn = init_db(db_path)
    print("Fetching sector classifications...")
    sectors = get_sectors(conn, needs["holdings"])

    # 5. Build the dashboard (src/pipeline.py, shared with the web edition).
    try:
        html, summary = build_dashboard(
            trades, corporate_actions, alloc_cfg, config, conn, prices, fx,
            public=public, title=title, franking=franking, sectors=sectors,
            allocation_warnings=alloc_warnings,
            allocation_file=allocation_path.name,
            allocation_download=allocation_download,
        )
    except NoOpenPositions as e:
        print(e)
        sys.exit(0)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")

    # 6. Print summary
    pl = summary["total_pl_aud"]
    pl_pct = summary["total_pl_pct"]
    pl_sign = "+" if pl >= 0 else ""
    print("\n=== Summary ===")
    print(f"  Total Value (AUD):  ${summary['total_value_aud']:>12,.2f}")
    print(f"  Unrealised P/L:     {pl_sign}${pl:>11,.2f}  ({pl_sign}{pl_pct:.2f}%)")
    print(f"  Open Positions:     {summary['num_positions']}")
    n_unassigned = summary["unassigned"]
    print(
        f"  Allocation:         {summary['buckets_off']} bucket(s) outside band"
        + (f"; {n_unassigned} holding(s) unassigned "
           f"(${summary['unassigned_value']:,.0f})" if n_unassigned else "")
    )
    print(f"\n  Dashboard: {output_path.resolve()}")

    # 7. Open in browser
    webbrowser.open(output_path.resolve().as_uri())
    print("  Opened in browser.")


if __name__ == "__main__":
    main()
