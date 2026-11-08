"""Flexible parsing of user-supplied dates (CSV columns and config.yaml).

Everything downstream of ingest works in pandas Timestamps / ``datetime.date``,
so this module is the single place where free-text dates are pinned down.

Two rules shape the design:

1. **Day-first.** Numeric ``03/04/2020`` is read as 3 April, not 4 March. This
   tracker is AU-facing (ASX codes, AUD base currency, AU broker exports), and
   guessing per-value would silently mix the two conventions inside one file.
2. **One format per column.** A whole column is parsed with a single format —
   the first candidate that explains *every* value. Choosing per cell is what
   turns "one row was written oddly" into "half your cost base moved by a
   month". Genuinely mixed files still work, but only via an explicit fallback
   that says so on stdout.
"""
from __future__ import annotations

import re
from datetime import date, datetime

import pandas as pd

# Candidate formats, in priority order. The first one that parses every value
# in a column wins, so unambiguous/common layouts must come before exotic ones.
DATE_FORMATS: tuple[str, ...] = (
    # ISO and ISO-like
    "%Y-%m-%d",      # 2020-07-23
    "%Y/%m/%d",      # 2020/07/23
    "%Y%m%d",        # 20200723
    # Numeric, day first
    "%d/%m/%Y",      # 23/07/2020, 23/7/2020
    "%d-%m-%Y",      # 23-07-2020
    "%d.%m.%Y",      # 23.07.2020
    "%d/%m/%y",      # 23/07/20
    "%d-%m-%y",      # 23-07-20
    "%d.%m.%y",      # 23.07.20
    # Month name, day first
    "%d-%b-%Y",      # 23-Jul-2020
    "%d-%b-%y",      # 23-Jul-20
    "%d %b %Y",      # 23 Jul 2020
    "%d %b %y",      # 23 Jul 20
    "%d %B %Y",      # 23 July 2020
    "%d%b%Y",        # 23Jul2020
    "%d%b%y",        # 23Jul20
    # Month name, month first (unambiguous — the month is spelled out)
    "%b %d %Y",      # Jul 23 2020
    "%b %d, %Y",     # Jul 23, 2020
    "%B %d %Y",      # July 23 2020
    "%B %d, %Y",     # July 23, 2020
)

# Day-first numeric formats and the month-first reading of the same layout.
# If both explain a whole column, the column is genuinely ambiguous and we say
# so rather than quietly taking the day-first answer.
_MONTH_FIRST_TWIN: dict[str, str] = {
    "%d/%m/%Y": "%m/%d/%Y",
    "%d-%m-%Y": "%m-%d-%Y",
    "%d.%m.%Y": "%m.%d.%Y",
    "%d/%m/%y": "%m/%d/%y",
    "%d-%m-%y": "%m-%d-%y",
    "%d.%m.%y": "%m.%d.%y",
}

# Trailing clock time on broker exports ("23/07/2020 14:32:00", "2020-07-23T00:00").
# Holdings and CGT are day-resolution, so the time is dropped.
_TIME_SUFFIX = re.compile(
    r"[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\s*(?:[AaPp]\.?[Mm]\.?)?"
    r"(?:\s*(?:Z|[+-]\d{2}:?\d{2}))?$"
)


def _normalise(raw: object) -> str:
    """Strip whitespace, any trailing clock time, and a trailing full stop."""
    text = str(raw).strip()
    text = _TIME_SUFFIX.sub("", text).strip()
    return text


def _parse_one(text: str, fmt: str) -> date | None:
    try:
        return datetime.strptime(text, fmt).date()
    except ValueError:
        return None


def _describe_supported() -> str:
    return (
        "Supported date formats include YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, "
        "DD-Mon-YY, DD Mon YYYY and YYYYMMDD. Numeric day/month dates are read "
        "day-first (03/04/2020 is 3 April)."
    )


def parse_date(value: object, *, source: str, field: str = "date") -> date:
    """Parse a single user-supplied date (e.g. a config.yaml ``target_date``).

    ``source`` and ``field`` only appear in the error message.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = _normalise(value)
    if not text or text.lower() in {"nan", "nat", "none"}:
        raise ValueError(f"{source}: {field} is required but was empty")

    for fmt in DATE_FORMATS:
        parsed = _parse_one(text, fmt)
        if parsed is not None:
            return parsed

    raise ValueError(
        f"{source}: could not parse {field} '{text}'. {_describe_supported()}"
    )


def parse_date_series(
    values: pd.Series, *, source: str, column: str = "date"
) -> pd.Series:
    """Parse a column of user-supplied dates into datetime64.

    Picks the single format that explains every value in the column. Falls back
    to per-value parsing (with a warning) only if no single format fits, and
    raises with the offending rows if some value fits no format at all.
    """
    if pd.api.types.is_datetime64_any_dtype(values):
        return values

    text = values.map(_normalise)

    blank = text.isin(["", "nan", "NaN", "NaT", "None"])
    if blank.any():
        rows = ", ".join(str(i) for i in text.index[blank][:5])
        raise ValueError(
            f"{source}: {column} is empty on row(s) {rows}. Every row needs a date."
        )

    # Pass 1 — one format for the whole column.
    for fmt in DATE_FORMATS:
        parsed = [_parse_one(t, fmt) for t in text]
        if any(p is None for p in parsed):
            continue
        _warn_if_ambiguous(text, fmt, source=source, column=column)
        return pd.Series(pd.to_datetime(parsed), index=values.index, name=values.name)

    # Pass 2 — mixed formats within the one column. Legitimate when a file is
    # stitched together from two brokers' exports, but worth flagging.
    per_value: list[date | None] = []
    for t in text:
        match = next(
            (p for p in (_parse_one(t, fmt) for fmt in DATE_FORMATS) if p is not None),
            None,
        )
        per_value.append(match)

    unparsed = [
        (idx, t) for idx, t, p in zip(text.index, text, per_value) if p is None
    ]
    if unparsed:
        shown = "; ".join(f"row {idx}: '{t}'" for idx, t in unparsed[:5])
        more = f" (and {len(unparsed) - 5} more)" if len(unparsed) > 5 else ""
        raise ValueError(
            f"{source}: could not parse {column} on {len(unparsed)} row(s) — "
            f"{shown}{more}. {_describe_supported()}"
        )

    print(
        f"  WARNING: {source} mixes more than one date format in '{column}'. "
        f"Each value was parsed on its own, day-first. Check the rows that "
        f"aren't YYYY-MM-DD."
    )
    return pd.Series(pd.to_datetime(per_value), index=values.index, name=values.name)


def _warn_if_ambiguous(
    text: pd.Series, fmt: str, *, source: str, column: str
) -> None:
    """Flag a numeric column where every value also reads as month-first.

    Only fires when nothing in the column disambiguates — a single value with a
    first component above 12 (e.g. ``23/07/2020``) proves the day-first reading.
    """
    twin = _MONTH_FIRST_TWIN.get(fmt)
    if twin is None:
        return
    if any(_parse_one(t, twin) is None for t in text):
        return
    print(
        f"  WARNING: every {column} in {source} is ambiguous (e.g. "
        f"'{text.iloc[0]}' could be day-first or month-first). Read as "
        f"day-first (DD/MM). Use YYYY-MM-DD if that's wrong."
    )
