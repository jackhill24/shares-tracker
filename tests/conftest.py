from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def trades_csv_path() -> Path:
    return FIXTURES_DIR / "trades.csv"


@pytest.fixture
def flat_fx_provider():
    """FX provider stub: AUD is 1:1, everything else a fixed 1.5 AUD per unit."""
    def _fx(currency: str, date_str: str) -> float:
        return 1.0 if currency == "AUD" else 1.5
    return _fx
