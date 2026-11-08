// The price relay (worker/ in this repo). If you run your own copy, put its
// *.workers.dev address here and add your site to its ALLOWED_ORIGINS.
export const RELAY_URL = "https://shares-tracker-prices.portfolio-summary.workers.dev";

// Shown in Settings until the visitor changes them. Mirrors the defaults in
// src/config.py, which apply whenever no settings are saved.
export const DEFAULT_SETTINGS = {
  benchmarks: [
    { ticker: "VAS.AX", label: "ASX 300" },
    { ticker: "IVV.AX", label: "S&P 500 (AUD)" },
  ],
  risk: { rba_cash_rate: 0.0435 },
};
