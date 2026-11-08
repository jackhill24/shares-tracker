"""Config loader (src/config.py): defaults when absent, override from YAML."""
import pytest

from src.config import config_from_dict, load_config


def test_missing_file_uses_defaults(tmp_path):
    cfg = load_config(tmp_path / "nope.yaml")
    assert cfg.base_currency == "AUD"
    assert [b.ticker for b in cfg.benchmarks] == ["VAS.AX", "IVV.AX"]


def test_reads_benchmarks_and_base_currency(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "base_currency: aud\n"
        "benchmarks:\n"
        "  - ticker: STW.AX\n"
        "    label: ASX 200\n"
    )
    cfg = load_config(p)
    assert cfg.base_currency == "AUD"
    assert len(cfg.benchmarks) == 1
    assert cfg.benchmarks[0].ticker == "STW.AX"
    assert cfg.benchmarks[0].label == "ASX 200"


def test_label_defaults_to_ticker(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("benchmarks:\n  - ticker: VAS.AX\n")
    cfg = load_config(p)
    assert cfg.benchmarks[0].label == "VAS.AX"


def test_config_from_dict_matches_the_file_loader(tmp_path):
    # The browser edition keeps its settings as JSON in config.yaml's shape.
    p = tmp_path / "config.yaml"
    p.write_text("benchmarks:\n  - ticker: STW.AX\n    label: ASX 200\nrisk:\n  rba_cash_rate: 0.03\n")
    d = {"benchmarks": [{"ticker": "STW.AX", "label": "ASX 200"}], "risk": {"rba_cash_rate": 0.03}}
    assert config_from_dict(d) == load_config(p)
    assert config_from_dict({}) == load_config(tmp_path / "nope.yaml")
    with pytest.raises(ValueError):
        config_from_dict(["not", "a", "mapping"])
