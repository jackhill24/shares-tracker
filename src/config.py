"""Application configuration loaded from an optional config.yaml.

If the file is absent (or a key is missing) sensible defaults are used, so the
tracker runs unchanged out of the box. Benchmarks must be AUD-quoted so the
performance layer needs no FX conversion (Phase 3).
"""
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_DEFAULT_BENCHMARKS = [
    {"ticker": "VAS.AX", "label": "ASX 300"},
    {"ticker": "IVV.AX", "label": "S&P 500 (AUD)"},
]


@dataclass
class Benchmark:
    ticker: str
    label: str


@dataclass
class Config:
    base_currency: str = "AUD"
    benchmarks: list[Benchmark] = field(default_factory=list)
    # Risk-free rate for Sharpe (Phase 5), as a fraction p.a. Default ≈ RBA cash.
    rba_cash_rate: float = 0.0435


def load_config(path: str | Path) -> Config:
    """Load config.yaml, falling back to defaults for anything missing."""
    path = Path(path)
    raw: dict = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} must be a YAML mapping at the top level.")
        raw = loaded
    return config_from_dict(raw)


def config_from_dict(raw: dict) -> Config:
    """Build a Config from config.yaml's structure (the browser edition keeps
    its settings as JSON in this shape), defaulting anything missing."""
    if not isinstance(raw, dict):
        raise ValueError("Config must be a mapping.")
    base_currency = str(raw.get("base_currency", "AUD")).strip().upper()

    bench_raw = raw.get("benchmarks")
    if bench_raw is None:
        bench_raw = _DEFAULT_BENCHMARKS
    benchmarks = []
    for b in bench_raw:
        ticker = str(b["ticker"]).strip()
        label = str(b.get("label", ticker)).strip()
        benchmarks.append(Benchmark(ticker=ticker, label=label))

    # Risk block (Phase 5). ``risk.rba_cash_rate`` is a fraction (0.0435 = 4.35%).
    risk_raw = raw.get("risk") or {}
    rba_cash_rate = float(risk_raw.get("rba_cash_rate", 0.0435))

    return Config(
        base_currency=base_currency,
        benchmarks=benchmarks,
        rba_cash_rate=rba_cash_rate,
    )
