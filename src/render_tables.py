"""Pure HTML string builders for the dashboard tables and "show the working"
expansions.

No plotly here — every function returns an escaped HTML fragment that
render.py drops into the Jinja template. Kept separate from render.py so the
fragments stay unit-testable without a browser, and so the chart code doesn't
grow. All dynamic text must pass through html.escape (this is manual string
building, not Jinja autoescape).

Expandable rows follow the pattern established by the holdings table:
a ``tr.parent`` with ``data-target="ID"`` toggles every ``tr.child ID``
sibling via the single delegated click listener in the template.
"""

from __future__ import annotations

from html import escape

import pandas as pd

from src.cgt import _fy_label, _fy_sort_key


# --- Shared formatters ------------------------------------------------------

def _pl_class(v: float) -> str:
    return "pos" if v >= 0 else "neg"


def _fmt_money(v: float) -> str:
    return f"${v:,.0f}"


def _fmt_money2(v: float) -> str:
    """Exact-to-the-cent money for tax-prep / working detail."""
    sign = "−" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _fmt_pl(v: float) -> str:
    sign = "+" if v >= 0 else "−"
    return f"{sign}${abs(v):,.0f}"


def _fmt_pl2(v: float) -> str:
    sign = "+" if v >= 0 else "−"
    return f"{sign}${abs(v):,.2f}"


def _fmt_pct(v: float) -> str:
    if pd.isna(v):
        return "—"
    sign = "+" if v >= 0 else "−"
    return f"{sign}{abs(v):.1f}%"


def _fmt_ret(v) -> str:
    """Format an annualised return (fraction) as a signed percentage."""
    if v is None or pd.isna(v):
        return "—"
    sign = "+" if v >= 0 else "−"
    return f"{sign}{abs(v) * 100:.1f}%"


def _fmt_date(d) -> str:
    if d is None or pd.isna(d):
        return "—"
    return pd.Timestamp(d).strftime("%d %b %Y")


# --- Small structural helpers -----------------------------------------------

def _thead(cols: list[tuple[str, str]]) -> str:
    """cols: (label, css_class) — class 'hcol' for left-aligned, 'num' for right."""
    ths = "".join(f'<th class="{c}">{escape(label)}</th>' for label, c in cols)
    return f"<thead><tr>{ths}</tr></thead>"


def _working_html(lines: list[tuple[str, str]]) -> str:
    """A .working block: label on the left, substituted arithmetic on the right.

    Values are built exclusively from the formatters above (no raw user text);
    labels are escaped here.
    """
    rows = "".join(
        f'<div class="wline"><span class="wlabel">{escape(label)}</span>'
        f'<span class="wval">{value}</span></div>'
        for label, value in lines
    )
    return f'<div class="working">{rows}</div>'


def _caret() -> str:
    return '<span class="caret">▸</span>'


# --- Realised CGT by FY (summary + working) ---------------------------------

def cgt_fy_table_html(
    fy_summary_df: pd.DataFrame,
    working_steps: list[dict],
    current_fy: str,
) -> str:
    """FY summary table (same columns as the old plotly table) where each FY
    row expands to a .working block showing the loss-offset and discount
    arithmetic. ``working_steps`` comes from cgt.fy_working_steps and is
    row-aligned with ``fy_summary_df``.
    """
    if fy_summary_df is None or fy_summary_df.empty:
        return ""

    cols = [
        ("Financial Year", "hcol"),
        ("Gross Gains", "num"),
        ("Gross Losses", "num"),
        ("Net Pre-Discount", "num"),
        ("Discount-Eligible Gains", "num"),
        ("Loss Carried Fwd", "num"),
        ("Est. Taxable Gain*", "num"),
    ]

    rows: list[str] = []
    for i, (_, row) in enumerate(fy_summary_df.iterrows()):
        step = working_steps[i]
        rid = f"cgtfy-{i}"
        fy = escape(str(row["fy"]))
        cur_cls = " cur-fy" if row["fy"] == current_fy else ""
        tax_cls = _pl_class(row["est_taxable_gain"])
        rows.append(f"""
        <tr class="parent{cur_cls}" data-target="{rid}">
          <td class="hcol">{_caret()}<b>{fy}</b></td>
          <td class="num">{_fmt_pl(row['gross_gains'])}</td>
          <td class="num">{_fmt_pl(row['gross_losses']) if row['gross_losses'] != 0 else '$0'}</td>
          <td class="num">{_fmt_pl(row['net_gain_pre_discount'])}</td>
          <td class="num">{_fmt_pl(row['discount_eligible_gains'])}</td>
          <td class="num">{_fmt_money(row['loss_carried_forward'])}</td>
          <td class="num {tax_cls}"><b>{_fmt_pl(row['est_taxable_gain'])}</b></td>
        </tr>
        <tr class="child {rid}" style="display:none">
          <td colspan="7" class="wrap">{_fy_working_block(step)}</td>
        </tr>""")

    return (
        f'<table class="holdings">{_thead(cols)}'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _fy_working_block(s: dict) -> str:
    """Substituted arithmetic for one FY, mirroring summarise_by_fy exactly."""
    lines: list[tuple[str, str]] = [(
        "Gross capital gains",
        f"{_fmt_money2(s['gross_gains'])} = {_fmt_money2(s['disc_gains'])} discount-eligible"
        f" + {_fmt_money2(s['non_disc_gains'])} other",
    )]

    if s["loss_pool"] > 0:
        lines.append((
            "Capital losses available",
            f"{_fmt_money2(s['loss_pool'])} = {_fmt_money2(s['current_year_loss'])} this FY"
            f" + {_fmt_money2(s['carried_in'])} carried in",
        ))
        if s["loss_to_non_disc"] > 0:
            lines.append((
                "Losses offset non-discount gains first",
                f"{_fmt_money2(s['non_disc_gains'])} − {_fmt_money2(s['loss_to_non_disc'])}"
                f" → {_fmt_money2(s['non_disc_after'])}",
            ))
        if s["loss_to_disc"] > 0:
            lines.append((
                "Remaining losses offset discount-eligible gains",
                f"{_fmt_money2(s['disc_gains'])} − {_fmt_money2(s['loss_to_disc'])}"
                f" → {_fmt_money2(s['disc_after'])}",
            ))

    if s["disc_after"] > 0:
        lines.append((
            "50% CGT discount (held > 12 months)",
            f"{_fmt_money2(s['disc_after'])} × 50% = {_fmt_money2(s['discount_amount'])}",
        ))

    lines.append((
        "Est. taxable gain",
        f"{_fmt_money2(s['non_disc_after'])} + {_fmt_money2(s['discount_amount'])}"
        f" = {_fmt_money2(s['est_taxable'])}",
    ))

    if s["loss_carried_forward"] > 0:
        lines.append((
            "Unused losses carried forward to next FY",
            _fmt_money2(s["loss_carried_forward"]),
        ))
    return _working_html(lines)


# --- Disposals by stock (tax-return prep detail) ----------------------------

_PARCEL_SUBTABLE_HEAD = (
    '<tr><th class="l">Acquired</th><th class="l">Disposed</th><th>Units</th>'
    "<th>Unit Cost</th><th>Unit Proceeds</th><th>Cost Base A$</th>"
    "<th>Proceeds A$</th><th>Gain/Loss A$</th><th>Days Held</th>"
    "<th>Discount</th></tr>"
)


def disposals_by_stock_html(realised_df: pd.DataFrame) -> str:
    """Tax-return prep view: disposals grouped by FY (most recent first), one
    expandable row per stock with per-stock subtotals, opening to the full
    per-parcel detail (dates, units, cost base, proceeds, gain, discount).

    Gross gains and gross losses are shown as separate columns (not just the
    net), because the ATO figures are per-parcel: a single stock — even a
    single disposal — can contribute to *both* when FIFO matches winning and
    losing parcels together. The per-FY header sums those gross columns so they
    reconcile against the FY summary table's Gross Gains / Gross Losses.
    """
    if realised_df is None or realised_df.empty:
        return ""

    df = realised_df.copy()
    df["fy"] = df["disposed"].map(_fy_label)

    cols = [
        ("Stock", "hcol"),
        ("Parcels", "num"),
        ("Units Sold", "num"),
        ("Cost Base (A$)", "num"),
        ("Proceeds (A$)", "num"),
        ("Gross Gains (A$)", "num"),
        ("Gross Losses (A$)", "num"),
        ("Net (A$)", "num"),
        ("Discount-Eligible Gains", "num"),
    ]
    n_cols = len(cols)

    rows: list[str] = []
    fy_order = sorted(df["fy"].unique(), key=_fy_sort_key, reverse=True)
    for fi, fy in enumerate(fy_order):
        g_fy = df[df["fy"] == fy]
        gross_gains = g_fy.loc[g_fy["gain_aud"] > 0, "gain_aud"].sum()
        gross_losses = g_fy.loc[g_fy["gain_aud"] < 0, "gain_aud"].sum()  # ≤ 0
        net = g_fy["gain_aud"].sum()
        rows.append(f"""
        <tr class="fyhead"><td colspan="{n_cols}"><b>{escape(fy)}</b>
          <span class="fyhead-sub">{len(g_fy)} disposal event{'s' if len(g_fy) != 1 else ''}
          · gross gains {_fmt_money2(gross_gains)} · gross losses {_fmt_money2(gross_losses)}
          · net {_fmt_pl2(net)}</span></td></tr>""")

        for si, (code, g) in enumerate(sorted(g_fy.groupby("code"), key=lambda kv: kv[0])):
            rid = f"stk-{fi}-{si}"
            g_gains = g.loc[g["gain_aud"] > 0, "gain_aud"].sum()
            g_losses = g.loc[g["gain_aud"] < 0, "gain_aud"].sum()  # ≤ 0
            disc_gains = g.loc[g["discount_eligible"] & (g["gain_aud"] > 0), "gain_aud"].sum()
            gain = g["gain_aud"].sum()
            rows.append(f"""
        <tr class="parent" data-target="{rid}">
          <td class="hcol">{_caret()}<b>{escape(str(code))}</b></td>
          <td class="num">{len(g)}</td>
          <td class="num">{g['units'].sum():,.4f}</td>
          <td class="num">{_fmt_money2(g['cost_base_aud'].sum())}</td>
          <td class="num">{_fmt_money2(g['proceeds_aud'].sum())}</td>
          <td class="num pos">{_fmt_money2(g_gains) if g_gains else '—'}</td>
          <td class="num neg">{_fmt_money2(g_losses) if g_losses else '—'}</td>
          <td class="num {_pl_class(gain)}"><b>{_fmt_pl2(gain)}</b></td>
          <td class="num">{_fmt_money2(disc_gains)}</td>
        </tr>
        <tr class="child {rid}" style="display:none">
          <td colspan="{n_cols}" class="wrap">{_parcel_subtable(g)}</td>
        </tr>""")

    return (
        f'<table class="holdings">{_thead(cols)}'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _parcel_subtable(g: pd.DataFrame) -> str:
    body = []
    for _, p in g.sort_values(["disposed", "acquired"]).iterrows():
        ccy = escape(str(p["currency"]))
        disc = '<span class="badge">50%</span>' if p["discount_eligible"] else "—"
        body.append(
            f'<tr><td class="l">{_fmt_date(p["acquired"])}</td>'
            f'<td class="l">{_fmt_date(p["disposed"])}</td>'
            f"<td>{p['units']:,.4f}</td>"
            f"<td>{p['unit_cost_local']:,.4f} {ccy}</td>"
            f"<td>{p['unit_proceeds_local']:,.4f} {ccy}</td>"
            f"<td>{_fmt_money2(p['cost_base_aud'])}</td>"
            f"<td>{_fmt_money2(p['proceeds_aud'])}</td>"
            f'<td class="{_pl_class(p["gain_aud"])}">{_fmt_pl2(p["gain_aud"])}</td>'
            f"<td>{int(p['held_days'])}</td>"
            f"<td>{disc}</td></tr>"
        )
    return (
        f'<table class="subtable"><thead>{_PARCEL_SUBTABLE_HEAD}</thead>'
        f"<tbody>{''.join(body)}</tbody></table>"
    )


# --- ATO tax-return figures (current FY headline numbers) -------------------

def cgt_tax_return_summary_html(
    fy_summary_df: pd.DataFrame,
    current_fy: str,
) -> str:
    """The headline figures for the current FY, labelled to match the ATO CGT
    schedule, so they can be read straight onto a return.

    Every value comes from the one ``fy_summary_df`` row for ``current_fy`` — the
    same frame the FY table and the parcel detail are built from — so the three
    views always agree. ``est_taxable_gain`` is the *net capital gain* (losses
    applied, then the 50% discount). Losses are shown as positive magnitudes,
    the way ATO fields are entered.

    Returns "" if the current FY has no realised disposals.
    """
    if fy_summary_df is None or fy_summary_df.empty:
        return ""
    row = fy_summary_df[fy_summary_df["fy"] == current_fy]
    if row.empty:
        return ""
    r = row.iloc[0]

    gross_gains = float(r["gross_gains"])
    gross_losses = -float(r["gross_losses"])  # magnitude, ≥ 0
    carried_applied = float(r["carried_loss_applied"])  # prior-year losses used
    carried_forward = float(r["loss_carried_forward"])
    net_gain = float(r["est_taxable_gain"])

    # Current-year losses applied = this year's own losses that were used up,
    # i.e. own losses minus whatever of them is carried forward. Prior-year
    # losses are only ever carried forward once this year's own losses are
    # exhausted (summarise_by_fy applies current-year first), so the carry-
    # forward balance is drawn from own losses before carried-in — hence the
    # unused own-loss is exactly min(own losses, carry-forward).
    current_unused = min(gross_losses, carried_forward)
    current_applied = gross_losses - current_unused

    def _row(label: str, value: str, cls: str = "", strong: bool = False) -> str:
        v = f"<b>{value}</b>" if strong else value
        return (
            f'<div class="taxrow{" " + cls if cls else ""}">'
            f'<span class="taxlabel">{escape(label)}</span>'
            f'<span class="taxval">{v}</span></div>'
        )

    parts = [
        _row("Total current year capital gains", _fmt_money2(gross_gains)),
        _row("Total current year capital losses", _fmt_money2(gross_losses)),
        _row("Total current year capital losses applied", _fmt_money2(current_applied)),
    ]
    if carried_applied > 0:
        parts.append(_row(
            "Net capital losses from earlier years applied",
            _fmt_money2(carried_applied),
        ))
    parts.append(_row(
        "Net capital gain",
        _fmt_money2(net_gain), cls="taxtotal", strong=True,
    ))
    if float(r["loss_carried_forward"]) > 0:
        parts.append(_row(
            "Net capital losses carried forward to later years",
            _fmt_money2(float(r["loss_carried_forward"])),
        ))

    return (
        f'<div class="taxsummary"><div class="taxsummary-fy">{escape(current_fy)}</div>'
        f'{"".join(parts)}</div>'
    )


# --- Performance (TWR windows + working, per-holding XIRR) ------------------

def performance_table_frag_html(performance: dict) -> str:
    """One expandable row per standard window: portfolio TWR, each benchmark's
    TWR, excess vs the primary benchmark; the expansion shows the inputs and
    the annualisation with real numbers substituted.
    """
    windows = performance.get("windows", [])
    labels = performance.get("bench_labels", {})
    if not windows:
        return ""
    bench_tickers = list(labels.keys())

    cols = [("Window", "hcol"), ("Portfolio TWR (p.a.)", "num")]
    cols += [(f"{labels[t]} (p.a.)", "num") for t in bench_tickers]
    primary_label = labels.get(bench_tickers[0], "benchmark") if bench_tickers else "benchmark"
    cols.append((f"Excess vs {primary_label}", "num"))
    n_cols = len(cols)

    rows: list[str] = []
    for i, w in enumerate(windows):
        rid = f"perfw-{i}"
        bench_by_ticker = {b["ticker"]: b for b in w["benchmarks"]}
        cells = [
            f'<td class="hcol">{_caret()}<b>{escape(w["window"])}</b></td>',
            f'<td class="num {_ret_class(w["portfolio_twr"])}">{_fmt_ret(w["portfolio_twr"])}</td>',
        ]
        for t in bench_tickers:
            entry = bench_by_ticker.get(t)
            v = entry["annualised"] if entry else None
            cells.append(f'<td class="num {_ret_class(v)}">{_fmt_ret(v)}</td>')
        primary = bench_by_ticker.get(bench_tickers[0]) if bench_tickers else None
        excess = primary["excess"] if primary else None
        cells.append(f'<td class="num {_ret_class(excess)}"><b>{_fmt_ret(excess)}</b></td>')

        rows.append(f"""
        <tr class="parent" data-target="{rid}">{''.join(cells)}</tr>
        <tr class="child {rid}" style="display:none">
          <td colspan="{n_cols}" class="wrap">{_window_working_block(w, labels)}</td>
        </tr>""")

    return (
        f'<table class="holdings">{_thead(cols)}'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _ret_class(v) -> str:
    if v is None or pd.isna(v):
        return ""
    return _pl_class(v)


def _twr_lines(prefix: str, d: dict) -> list[tuple[str, str]]:
    """Working lines for one TWR result detail dict (portfolio or benchmark)."""
    lines = [(
        f"{prefix} value",
        f"{_fmt_date(d['start_date'])}: {_fmt_money(d['start_value'])}"
        f" → {_fmt_date(d['end_date'])}: {_fmt_money(d['end_value'])}",
    )]
    if d.get("net_contributions") is not None:
        lines.append((
            f"{prefix} net contributions in window (excluded from return)",
            _fmt_pl(d["net_contributions"]),
        ))
    total, days = d.get("total_return"), d.get("days")
    if total is not None:
        lines.append((
            f"{prefix} total return (chain-linked across snapshot sub-periods)",
            _fmt_ret(total),
        ))
    if total is not None and days and 1.0 + total > 0:  # see performance._annualise
        ann = (1.0 + total) ** (365.0 / days) - 1.0
        lines.append((
            f"{prefix} annualised",
            f"(1 {'+' if total >= 0 else '−'} {abs(total):.4f})^(365 / {days}) − 1"
            f" = {_fmt_ret(ann)} p.a.",
        ))
    return lines


def _window_working_block(w: dict, labels: dict) -> str:
    detail = w.get("portfolio_detail")
    if not detail or detail.get("start_value") is None:
        return _working_html([(
            "Detail unavailable for this window",
            "re-run after the next snapshot",
        )])

    lines = _twr_lines("Portfolio", detail)
    for b in w["benchmarks"]:
        bd = b.get("detail")
        label = labels.get(b["ticker"], b["ticker"])
        if bd and bd.get("start_value") is not None:
            lines += _twr_lines(f"{label} shadow", bd)
        if b.get("excess") is not None and b.get("annualised") is not None \
                and w.get("portfolio_twr") is not None:
            lines.append((
                f"Excess vs {label}",
                f"{_fmt_ret(w['portfolio_twr'])} − {_fmt_ret(b['annualised'])}"
                f" = {_fmt_ret(b['excess'])} p.a.",
            ))
    return _working_html(lines)


def per_holding_xirr_table_html(per_holding_xirr: dict, by_code: pd.DataFrame) -> str:
    """Collapsible per-holding XIRR table (money-weighted, all-time), with
    current market value for context. Sold-out holdings show a value of —.
    """
    if not per_holding_xirr:
        return ""

    values = (
        by_code.set_index("code")["market_value_aud"].to_dict()
        if by_code is not None and not by_code.empty else {}
    )
    order = sorted(
        per_holding_xirr.keys(),
        key=lambda c: (-(values.get(c) or 0.0), c),
    )
    body = []
    for code in order:
        x = per_holding_xirr[code]
        mv = values.get(code)
        body.append(
            f'<tr><td class="l"><b>{escape(str(code))}</b></td>'
            f"<td>{_fmt_money(mv) if mv else '—'}</td>"
            f'<td class="{_ret_class(x)}">{_fmt_ret(x)}</td></tr>'
        )
    table = (
        '<table class="subtable"><thead><tr><th class="l">Holding</th>'
        "<th>Market Value</th><th>XIRR (p.a.)</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )
    return (
        '<details class="inline-details"><summary>Per-holding XIRR '
        "(money-weighted, all-time)</summary>"
        f"{table}</details>"
    )


# --- "If sold today" (surfaced disposal simulation) -------------------------

def if_sold_today_table_html(disposals: dict) -> str:
    """One expandable row per holding: estimated gain and taxable gain if the
    whole position were sold at today's price/FX, FIFO vs a min-tax parcel
    ordering. Expands to the FIFO per-parcel breakdown.
    """
    if not disposals:
        return ""

    cols = [
        ("Holding", "hcol"),
        ("Units", "num"),
        ("Est. Gain (FIFO)", "num"),
        ("Est. Taxable (FIFO)", "num"),
        ("Est. Taxable (min-tax order)", "num"),
        ("Potential Saving", "num"),
    ]

    rows: list[str] = []
    for i, key in enumerate(sorted(disposals.keys())):
        d = disposals[key]
        fifo, min_tax = d["fifo"], d["min_tax"]
        if not fifo["parcels"]:
            continue
        code, _, exchange = key.partition(":")
        rid = f"sold-{i}"
        saving = fifo["total_taxable_gain_aud"] - min_tax["total_taxable_gain_aud"]
        saving_str = _fmt_money2(saving) if saving > 0.005 else "—"
        rows.append(f"""
        <tr class="parent" data-target="{rid}">
          <td class="hcol">{_caret()}<b>{escape(code)}</b>
            <span class="badge">{escape(exchange)}</span></td>
          <td class="num">{fifo['units_disposed']:,.4f}</td>
          <td class="num {_pl_class(fifo['total_gain_aud'])}">{_fmt_pl2(fifo['total_gain_aud'])}</td>
          <td class="num">{_fmt_pl2(fifo['total_taxable_gain_aud'])}</td>
          <td class="num">{_fmt_pl2(min_tax['total_taxable_gain_aud'])}</td>
          <td class="num">{saving_str}</td>
        </tr>
        <tr class="child {rid}" style="display:none">
          <td colspan="6" class="wrap">{_sold_parcel_subtable(fifo, min_tax)}</td>
        </tr>""")

    if not rows:
        return ""
    return (
        f'<table class="holdings">{_thead(cols)}'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _sold_parcel_subtable(fifo: dict, min_tax: dict) -> str:
    body = []
    for p in fifo["parcels"]:
        disc = '<span class="badge">50%</span>' if p["discount_eligible"] else "—"
        body.append(
            f'<tr><td class="l">{_fmt_date(p["acquired"])}</td>'
            f"<td>{p['units']:,.4f}</td>"
            f"<td>{_fmt_money2(p['cost_base_aud'])}</td>"
            f"<td>{_fmt_money2(p['proceeds_aud'])}</td>"
            f'<td class="{_pl_class(p["gain_aud"])}">{_fmt_pl2(p["gain_aud"])}</td>'
            f"<td>{disc}</td>"
            f"<td>{_fmt_pl2(p['taxable_gain_aud'])}</td></tr>"
        )
    head = (
        '<tr><th class="l">Acquired (FIFO order)</th><th>Units</th>'
        "<th>Cost Base A$</th><th>Proceeds A$</th><th>Gain/Loss A$</th>"
        "<th>Discount</th><th>Taxable A$</th></tr>"
    )
    saving = fifo["total_taxable_gain_aud"] - min_tax["total_taxable_gain_aud"]
    note = ""
    if saving > 0.005:
        note = (
            '<p class="subnote">Choosing parcels to minimise tax (realising losses '
            "and discount-eligible parcels first) would reduce the estimated "
            f"taxable gain by <b>{_fmt_money2(saving)}</b> "
            f"({_fmt_pl2(min_tax['total_taxable_gain_aud'])} vs "
            f"{_fmt_pl2(fifo['total_taxable_gain_aud'])} FIFO).</p>"
        )
    return (
        f'<table class="subtable"><thead>{head}</thead>'
        f"<tbody>{''.join(body)}</tbody></table>{note}"
    )


# --- Dividend income by FY ---------------------------------------------------

def income_fy_table_html(income_fy_df: pd.DataFrame) -> str:
    """Dividends per FY as a flat table; franking columns only when present."""
    if income_fy_df is None or income_fy_df.empty:
        return ""
    df = income_fy_df
    has_franking = df["franking_credits_aud"].sum() > 0

    if has_franking:
        cols = [("Financial Year", "hcol"), ("Dividends", "num"),
                ("Franking Credits", "num"), ("Grossed-Up", "num")]
        body = "".join(
            f'<tr><td class="hcol"><b>{escape(str(r["fy"]))}</b></td>'
            f'<td class="num">{_fmt_money(r["dividend_income_aud"])}</td>'
            f'<td class="num">{_fmt_money(r["franking_credits_aud"])}</td>'
            f'<td class="num">{_fmt_money(r["grossed_up_aud"])}</td></tr>'
            for _, r in df.iterrows()
        )
    else:
        cols = [("Financial Year", "hcol"), ("Dividends (AUD)", "num")]
        body = "".join(
            f'<tr><td class="hcol"><b>{escape(str(r["fy"]))}</b></td>'
            f'<td class="num">{_fmt_money(r["dividend_income_aud"])}</td></tr>'
            for _, r in df.iterrows()
        )

    return f'<table class="holdings">{_thead(cols)}<tbody>{body}</tbody></table>'
