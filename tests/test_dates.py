"""Tests for src/dates.py — flexible parsing of user-supplied dates.

The load-bearing behaviours are: day-first for numeric dates, one format per
column (never a per-cell guess unless the column really is mixed), and a loud
failure rather than a silent NaT when a value can't be read.
"""
import pandas as pd
import pytest

from src.dates import parse_date, parse_date_series


def _parse(values, **kw):
    return parse_date_series(pd.Series(values), source="trades.csv", **kw)


# --------------------------------------------------------------------------
# Formats accepted
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "2020-07-23",
        "2020/07/23",
        "20200723",
        "23/07/2020",
        "23/7/2020",
        "23-07-2020",
        "23.07.2020",
        "23/07/20",
        "23-Jul-2020",
        "23-Jul-20",
        "23 Jul 2020",
        "23 July 2020",
        "23Jul2020",
        "Jul 23 2020",
        "Jul 23, 2020",
        "July 23, 2020",
    ],
)
def test_formats_all_resolve_to_same_day(text):
    assert _parse([text]).iloc[0] == pd.Timestamp("2020-07-23")


def test_trailing_clock_time_is_dropped(text="23/07/2020 14:32:00"):
    assert _parse([text]).iloc[0] == pd.Timestamp("2020-07-23")


@pytest.mark.parametrize(
    "text", ["2020-07-23T00:00:00", "2020-07-23 09:15", "23/07/2020 2:05 PM"]
)
def test_datetime_variants(text):
    assert _parse([text]).iloc[0] == pd.Timestamp("2020-07-23")


def test_surrounding_whitespace_tolerated():
    assert _parse(["  23/07/2020 "]).iloc[0] == pd.Timestamp("2020-07-23")


def test_already_datetime_passes_through():
    s = pd.Series(pd.to_datetime(["2020-07-23"]))
    assert parse_date_series(s, source="trades.csv").iloc[0] == pd.Timestamp("2020-07-23")


# --------------------------------------------------------------------------
# Day-first, and one format per column
# --------------------------------------------------------------------------

def test_numeric_dates_are_day_first():
    # 03/04 is 3 April, not 4 March.
    assert _parse(["03/04/2020", "23/07/2020"]).iloc[0] == pd.Timestamp("2020-04-03")


def test_column_format_is_chosen_once_not_per_cell():
    # 05/06/2020 alone would parse under several formats; the column's own
    # 23/07/2020 pins it to day-first, so 05/06 must be 5 June.
    out = _parse(["23/07/2020", "05/06/2020"])
    assert list(out) == [pd.Timestamp("2020-07-23"), pd.Timestamp("2020-06-05")]


def test_fully_ambiguous_column_warns_and_reads_day_first(capsys):
    out = _parse(["03/04/2020", "05/06/2020"])
    assert out.iloc[0] == pd.Timestamp("2020-04-03")
    assert "ambiguous" in capsys.readouterr().out


def test_unambiguous_column_does_not_warn(capsys):
    _parse(["23/07/2020", "05/06/2020"])
    assert "ambiguous" not in capsys.readouterr().out


def test_iso_column_does_not_warn(capsys):
    _parse(["2020-07-23", "2020-04-03"])
    assert capsys.readouterr().out == ""


def test_mixed_formats_parse_per_value_with_warning(capsys):
    out = _parse(["2020-07-23", "24-Jul-2020"])
    assert list(out) == [pd.Timestamp("2020-07-23"), pd.Timestamp("2020-07-24")]
    assert "mixes more than one date format" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Failures are loud
# --------------------------------------------------------------------------

def test_unparseable_value_raises_with_row_number():
    with pytest.raises(ValueError, match=r"row 1: 'not a date'"):
        _parse(["2020-07-23", "not a date"])


def test_impossible_date_raises():
    with pytest.raises(ValueError, match="could not parse"):
        _parse(["32/01/2020"])


def test_blank_date_raises():
    with pytest.raises(ValueError, match="is empty on row"):
        _parse(["2020-07-23", ""])


def test_missing_date_raises():
    with pytest.raises(ValueError, match="is empty on row"):
        _parse(["2020-07-23", None])


# --------------------------------------------------------------------------
# Scalar parsing (config.yaml)
# --------------------------------------------------------------------------

def test_parse_date_scalar_formats():
    from datetime import date

    for text in ["2030-01-01", "01/01/2030", "1-Jan-2030", "1 January 2030"]:
        assert parse_date(text, source="config.yaml") == date(2030, 1, 1)


def test_parse_date_passes_through_real_date():
    from datetime import date

    assert parse_date(date(2030, 1, 1), source="config.yaml") == date(2030, 1, 1)


def test_parse_date_rejects_junk():
    with pytest.raises(ValueError, match="could not parse target_date"):
        parse_date("soon", source="config.yaml", field="target_date")
