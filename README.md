# Shares Tracker

A personal Australian shares/crypto portfolio dashboard. Give it your trade
history as a CSV; it fetches current and historical prices/FX from Yahoo
Finance and produces an HTML dashboard on your own machine.

Your trades never leave your computer. The only network requests are to Yahoo
Finance, for prices and FX rates.

**Not financial advice.** Every figure is an estimate.

## In your browser

**Try it: https://jackhill24.github.io/shares-tracker/**

The same tracker also runs as a web page, with nothing to install: the Python
in `src/` runs in the browser via [Pyodide](https://pyodide.org), and your
trades, targets and settings stay in that browser. The page sends only ticker
symbols and date ranges to a small price relay (`worker/`), which fetches them
from Yahoo Finance.

The web edition doesn't handle share splits or show sectors yet.

`web/` holds the page. To try it locally:

```bash
python web/build.py                      # assembles web/dist, incl. py/app.zip of src/
python -m http.server 8000 -d web/dist   # then open http://localhost:8000
node --test web/test/*.test.mjs          # price-fetching tests
```

`web/config.js` names the price relay. Its `ALLOWED_ORIGINS` must include your
site's address (localhost is always allowed).

## What's on the dashboard

- **Summary**: total value, unrealised P/L, 12-month change, all-time XIRR and
  return against a benchmark.
- **Goal Planner**: set a goal amount, date, monthly contribution and expected
  return, and see whether you're on track.
- **Holdings Map**: a treemap of your holdings, organised by total portfolio,
  by your allocation buckets or by sector.
- **Holdings detail** and **allocation** by currency, sector and platform.
- **Targets**: group your holdings into strategy buckets (e.g. Core,
  Growth, Speculative), set a target % for each, and see which buckets are
  over or under target.
- **Net Worth History**: portfolio value against money invested over time.
- **Performance**: money-weighted (XIRR) and time-weighted returns over 6M, 12M,
  3Y and all time, compared with putting the same cashflows into an index ETF.
- **Risk**: volatility, maximum drawdown, Sharpe ratio and beta over the
  trailing ~3 years.

## Running it

Needs Python 3.12.

```bash
pip install -r requirements.txt
python main.py --demo      # try it with the bundled fictional portfolio first
python main.py             # then with your own data/trades.csv
```

It writes `output/portfolio.html` (or `output/demo_portfolio.html`) and opens it
in your browser. Prices and history are cached in a local SQLite database
(`portfolio.db`) so later runs are quick. The first run on a long trade
history backfills past snapshots and can take a few minutes.

`data/`, the databases and `output/` are git-ignored. Keep it that way, so
your own holdings never end up in a commit.

## Your trades: `data/trades.csv`

One row per buy or sell.

| Column        | Notes                                                          |
|---------------|----------------------------------------------------------------|
| `code`        | Ticker as your broker shows it, e.g. `VAS`, `MSFT`, `BTC`        |
| `exchange`    | One of `ASX`, `NASDAQ`, `NYSE`, `LSE`, `HKEX`, `TSX`, `XMIL`, `CRYPTO`, `CRYPTO-AUD` |
| `date`        | Trade date (see below)                                         |
| `type`        | `buy` or `sell`                                                |
| `quantity`    | Units traded, positive                                         |
| `unit_price`  | Price per unit in the exchange's currency                      |
| `platform`    | Broker name. Holdings of the same code on different platforms are kept apart |
| `fees`        | *Optional.* Brokerage in the exchange's currency (blank = 0)    |

`data/demo_trades.csv` is a working example.

**Dates.** Any common layout works: `2020-07-23`, `23/07/2020`, `23-Jul-2020`,
`23 July 2020`, `Jul 23, 2020`. Numeric dates are always read **day first**, so
`03/04/2020` is 3 April. Convert US-style exports first. A year is always
required.

**Share splits.** Optional `data/corporate_actions.csv` with columns `code`,
`exchange`, `date`, `ratio` (new units per old unit: `2` for a 2-for-1 split,
`0.1` for a 1-for-10 consolidation). Yahoo prices are unadjusted, so record
splits here to keep pre-split units in line with today's prices.

## Targets

Open **Assign holdings** in the Targets section and pick a bucket for each
holding. You can rename, add or remove buckets and set their targets, which
must add up to 100. A holding you haven't assigned is left out of the
percentages and flagged. A bucket counts as on target within a tolerance band
of ± the smaller of 5 points and 25% of its target (both adjustable).

Edits are kept in your browser. **Save to file** downloads
`shares-tracker-allocation.yaml`. The next `python main.py` picks it up from
`~/Downloads` and moves it to `data/allocation.yaml`. You can also edit that
file by hand.

## Goal Planner

Set everything directly on the page. Settings are saved in your browser.
Contribution changes (e.g. a pay rise) and one-off lump sums can be added. Tick
"Show goal & projection" on the net-worth chart to overlay the projection.

## Optional `config.yaml`

```yaml
benchmarks:            # must be AUD-quoted, e.g. .AX ETFs
  - ticker: VAS.AX
    label: ASX 300
  - ticker: IVV.AX
    label: S&P 500 (AUD)
risk:
  rba_cash_rate: 0.0435   # risk-free rate for the Sharpe ratio
```

Performance compares your returns with a *shadow portfolio*: what the same
dollar amounts, on the same dates, would be worth in each benchmark.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```
