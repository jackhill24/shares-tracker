# Price relay (Cloudflare Worker)

The web edition runs the tracker in your browser, but browsers can't read
Yahoo Finance's price data from another website. This small Worker fetches it
on the page's behalf:

```
GET /chart?symbol=VAS.AX&start=2025-01-01&end=2025-12-31
→ {"symbol": "VAS.AX", "currency": "AUD", "timezone": "Australia/Sydney",
   "last": 98.12, "closes": {"2025-01-02": 97.5, ...}}
```

Share prices, price history and exchange rates (`AUDUSD=X`) all use this one
endpoint. Closes are dated in each exchange's own time zone, the same series
the desktop tracker gets from yfinance.

**What it sees and keeps.** Only ticker symbols and date ranges, never your
trades. It keeps no request logs and no cache of its own: each browser caches
its own answers. Cloudflare still handles the requests, as it would for any
website.

**Who can use it.** Pages on the site named in `ALLOWED_ORIGINS`
(`wrangler.toml`) and `http://localhost` for testing, with a limit of 120
requests a minute per visitor.

## Deploy

Needs a free Cloudflare account and Node.js.

```
cd worker
npm install
npx wrangler login     # once; opens a browser
npx wrangler deploy    # prints https://shares-tracker-prices.<you>.workers.dev
```

Set `ALLOWED_ORIGINS` in `wrangler.toml` to your GitHub Pages address (for
example `https://yourname.github.io`) and deploy again whenever it changes.

## Check

```
npm test                                          # rules + local end-to-end run
python3.12 parity_check.py https://shares-tracker-prices.<you>.workers.dev
```

`npm test` runs against a mock price source (no network needed).
`parity_check.py` compares the deployed relay with yfinance over the last 12
months, through the same cache code the dashboard uses.
