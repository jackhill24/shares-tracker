from datetime import date, datetime
from html import escape
import json

import pandas as pd
import plotly.graph_objects as go
from plotly.io import to_html
from jinja2 import Template

from src.cgt import fy_working_steps
from src.render_tables import (
    _fmt_money,
    _fmt_pct,
    _fmt_pl,
    _pl_class,
    cgt_fy_table_html,
    cgt_tax_return_summary_html,
    disposals_by_stock_html,
    if_sold_today_table_html,
    income_fy_table_html,
    per_holding_xirr_table_html,
    performance_table_frag_html,
)
from src.render_targets import TARGETS_CSS, TARGETS_JS, targets_html, targets_payload

# --- Simply Wall St-inspired light palette ---------------------------------
PAPER = "#ffffff"
PLOT_BG = "#ffffff"
GRID = "#e7ebf0"
TEXT = "#1a2b3c"
MUTED = "#64748b"
TEAL = "#14b8a6"
GREEN = "#22c55e"
RED = "#ef4444"
AXIS = "#94a3b8"

_PLOTLY_FONT = dict(family="-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif", color=TEXT)

# --- Categorical palette (identity encoding: allocation donuts) -------------
# Fixed slot order, assigned by rank and never cycled. Validated on a light
# surface for colour-vision-deficient separation (worst adjacent pair ΔE 9.1)
# and normal-vision separation (worst adjacent pair ΔE 19.6). Do NOT reorder or
# substitute individual hues without re-validating the set — the ordering *is*
# the safety mechanism. Past MAX_SLICES the tail folds into a single grey
# "Other" slice rather than inventing a ninth hue nobody can tell apart.
#
# (The previous sequential-Teal ramp was the bug behind the invisible currency
# donut: its first step, rgb(209,238,234), is near-white on a white panel, so
# the largest slice painted itself into the background.)
CATEGORICAL = [
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
    "#4a3aa7",  # violet
    "#e34948",  # red
]
MAX_SLICES = 7          # slices past this fold into "Other"
OTHER_COLOR = "#94a3b8"  # grey — "Other" is a residual, not an identity

_ALLOC_TITLES = {
    "currency": "By Currency",
    "sector": "By Sector",
    "platform": "By Platform",
}


def _rel_luminance(hex_color: str) -> float:
    """WCAG relative luminance of a #rrggbb colour."""
    r, g, b = (int(hex_color[i:i + 2], 16) / 255.0 for i in (1, 3, 5))

    def _lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _label_ink(hex_color: str) -> str:
    """White or dark label ink — whichever contrasts better on ``hex_color``.

    Slice labels sit *on* the slice, so a single ink colour can't serve the
    whole palette: white is right on blue and violet, unreadable on yellow.
    """
    lum = _rel_luminance(hex_color)
    on_white = 1.05 / (lum + 0.05)
    on_dark = (lum + 0.05) / (_rel_luminance(TEXT) + 0.05)
    return "#ffffff" if on_white >= on_dark else TEXT


def _platforms_str(platforms: list[str]) -> str:
    return ", ".join(platforms)


# Holdings-map views, in toggle order. "total" is always present; the others
# appear only when there's a grouping to show (see ``treemap_groupings``).
TREEMAP_VIEWS = {
    "total": "Total portfolio",
    "allocation": "Allocation",
    "sector": "Sector",
}


def treemap_groupings(
    holdings_df: pd.DataFrame, by_code_df: pd.DataFrame, allocation: dict | None,
) -> dict[str, dict[str, str] | None]:
    """{view: {code: group label}} for each holdings-map view that can render.

    "total" maps to None (no grouping level). Allocation uses the bucket
    assignments saved in the allocation config; sector uses the per-position
    sector column (one sector per ticker, so the first non-blank wins).
    Holdings with no group land in "Unassigned" / "Unclassified" rather than
    vanishing from the map.
    """
    codes = [str(c) for c in by_code_df["code"]]
    groupings: dict[str, dict[str, str] | None] = {"total": None}

    if allocation is not None:
        cfg = allocation["config"]
        labels = {b.id: b.label for b in cfg.buckets}
        groupings["allocation"] = {
            c: labels.get(cfg.holdings.get(c), "Unassigned") for c in codes
        }

    if "sector" in holdings_df.columns:
        sectors = (
            holdings_df.dropna(subset=["sector"])
            .assign(code=lambda d: d["code"].astype(str))
            .groupby("code")["sector"].first()
        )
        groupings["sector"] = {c: str(sectors.get(c, "Unclassified")) for c in codes}

    return groupings


def _treemap_hover(
    name: str, value: float, pl: float, pl_pct: float, weight: float, extra: list[str],
) -> str:
    lines = [
        f"<b>{escape(name)}</b>",
        f"Value: {_fmt_money(value)} AUD",
        f"Unrealised P/L: {_fmt_pl(pl)} ({_fmt_pct(pl_pct)})",
        f"Weight: {weight:.1f}% of portfolio",
    ]
    return "<br>".join(lines + extra)


def _treemap_trace(
    df: pd.DataFrame, view: str, groups: dict[str, str] | None, total_value: float,
) -> go.Treemap:
    """One treemap trace: Portfolio → [group →] ticker.

    Group and root tiles are computed here rather than left to Plotly, so their
    colour and hover P/L % is the true group figure (P/L ÷ cost base), not an
    average of the members' percentages. Parents carry zero value under
    ``branchvalues="remainder"`` so Plotly sizes them from their children and
    float rounding can never make a parent "smaller" than its contents.
    """
    def pct(pl: float, cost: float) -> float:
        return pl / cost * 100 if cost > 0 else float("nan")

    def weight(v: float) -> float:
        return v / total_value * 100 if total_value > 0 else 0.0

    ids, labels, parents, values, colors, texts, hovers = [], [], [], [], [], [], []

    def node(id_, label, parent, value, color, text, hover):
        ids.append(id_)
        labels.append(label)
        parents.append(parent)
        values.append(value)
        colors.append(None if pd.isna(color) else round(float(color), 2))
        texts.append(text)
        hovers.append(hover)

    def plural(n: int) -> str:
        return f"{n} holding{'s' if n != 1 else ''}"

    def add_parent(id_, name, parent, members, text):
        mv = float(members["market_value_aud"].sum())
        pl = float(members["unrealised_pl_aud"].sum())
        p = pct(pl, float(members["cost_base_aud"].sum()))
        node(id_, escape(name), parent, 0, p, text(mv),
             _treemap_hover(name, mv, pl, p, weight(mv), [plural(len(members))]))

    add_parent(view, "Portfolio", "", df, lambda mv: f"<b>Portfolio</b> · {_fmt_money(mv)}")

    group = None if groups is None else df["code"].astype(str).map(groups)
    group_value: dict[str, float] = {}
    if group is not None:
        for g, members in df.groupby(group, sort=False):
            add_parent(
                f"{view}/{g}", g, view, members,
                lambda mv, g=g: f"<b>{escape(g)}</b> · {_fmt_money(mv)} · {weight(mv):.0f}%",
            )
        group_value = df.groupby(group)["market_value_aud"].sum().to_dict()

    for i, r in df.iterrows():
        code = str(r["code"])
        mv = float(r["market_value_aud"]) if pd.notna(r["market_value_aud"]) else 0.0
        parent, extra = view, []
        if group is not None:
            g = group[i]
            parent = f"{view}/{g}"
            gv = group_value.get(g, 0.0)
            extra.append(f"{(mv / gv * 100 if gv > 0 else 0.0):.1f}% of {escape(g)}")
        extra += [
            f"Exchange(s): {escape(', '.join(r['exchanges']))}",
            f"Platform(s): {escape(_platforms_str(r['platforms']))}",
        ]
        node(
            f"{parent}/{code}", escape(code), parent, mv, r["unrealised_pl_pct"],
            f"<b>{escape(code)}</b><br>{_fmt_money(mv)}",
            _treemap_hover(code, mv, float(r["unrealised_pl_aud"]),
                           r["unrealised_pl_pct"], r["weight_pct"], extra),
        )

    return go.Treemap(
        name=view,
        ids=ids, labels=labels, parents=parents, values=values,
        branchvalues="remainder",
        text=texts, texttemplate="%{text}",
        hovertext=hovers, hovertemplate="%{hovertext}<extra></extra>",
        marker=dict(colors=colors, coloraxis="coloraxis", line=dict(color="#ffffff", width=2)),
        visible=view == "total",
    )


def treemap(
    by_code_df: pd.DataFrame, groupings: dict[str, dict[str, str] | None] | None = None,
) -> go.Figure:
    """Holdings treemap, one trace per view in ``groupings`` (from
    ``treemap_groupings``); only "total" starts visible and the page's
    "Organise by" control switches between them. Tiles are coloured by
    unrealised P/L % on one shared colour axis, so a colour means the same
    thing in every view.
    """
    groupings = groupings or {"total": None}
    total_value = float(by_code_df["market_value_aud"].sum())
    fig = go.Figure([
        _treemap_trace(by_code_df, view, groupings[view], total_value)
        for view in TREEMAP_VIEWS if view in groupings
    ])
    fig.update_layout(
        coloraxis=dict(
            colorscale=[[0, RED], [0.5, "#ffffff"], [1, GREEN]],
            cmid=0,
            colorbar=dict(title="P/L %"),
        ),
        margin=dict(t=10, l=0, r=0, b=0),
        paper_bgcolor=PAPER,
        font=_PLOTLY_FONT,
        height=460,
    )
    return fig


def _allocation_data(holdings_df: pd.DataFrame, by: str) -> pd.DataFrame:
    """Slice table for one allocation donut: label, value, pct, colour.

    Groups with no value are dropped (an invisible slice is noise), slices are
    ordered largest-first so colour follows rank consistently across a run, and
    anything past ``MAX_SLICES`` folds into one "Other" slice.
    """
    if by not in holdings_df.columns or holdings_df.empty:
        return pd.DataFrame(columns=["label", "value", "pct", "color"])

    agg = (
        holdings_df.groupby(by)["market_value_aud"]
        .sum()
        .reset_index()
        .rename(columns={by: "label", "market_value_aud": "value"})
    )
    agg = agg[agg["value"].notna() & (agg["value"] > 0)]
    agg = agg.sort_values("value", ascending=False).reset_index(drop=True)
    if agg.empty:
        return pd.DataFrame(columns=["label", "value", "pct", "color"])

    # Fold the tail only when it saves more than one slice — a lone "Other"
    # standing in for a single category would just hide its name.
    if len(agg) > MAX_SLICES + 1:
        tail_value = float(agg.iloc[MAX_SLICES:]["value"].sum())
        agg = pd.concat(
            [agg.iloc[:MAX_SLICES], pd.DataFrame([{"label": "Other", "value": tail_value}])],
            ignore_index=True,
        )

    agg["label"] = agg["label"].astype(str)
    total = float(agg["value"].sum())
    agg["pct"] = agg["value"] / total * 100.0 if total else 0.0
    agg["color"] = [
        OTHER_COLOR if (lbl == "Other" and i == MAX_SLICES) else CATEGORICAL[i]
        for i, lbl in enumerate(agg["label"])
    ]
    return agg


def _allocation_donut(agg: pd.DataFrame) -> go.Figure:
    """Donut for one allocation dimension, from a ``_allocation_data`` table.

    The chart carries percentages only; the HTML legend beneath it carries the
    names and dollar values, which keeps the plot area square and identical
    across the three panels regardless of how long the category names are.
    """
    total = float(agg["value"].sum())
    fig = go.Figure(go.Pie(
        labels=agg["label"].tolist(),
        values=agg["value"].tolist(),
        hole=0.62,
        sort=False,           # already ranked; let colour follow our order
        direction="clockwise",
        marker=dict(
            colors=agg["color"].tolist(),
            line=dict(color=PAPER, width=2),  # 2px surface gap between slices
        ),
        texttemplate="%{percent:.0%}",
        textposition="inside",
        insidetextorientation="horizontal",
        textfont=dict(size=12, color=[_label_ink(c) for c in agg["color"]]),
        hovertemplate=(
            "<b>%{label}</b><br>$%{value:,.0f} AUD<br>"
            "%{percent:.1%} of portfolio<extra></extra>"
        ),
        automargin=False,
    ))
    fig.update_layout(
        showlegend=False,
        margin=dict(t=6, l=6, r=6, b=6),
        paper_bgcolor=PAPER,
        plot_bgcolor=PAPER,
        font=_PLOTLY_FONT,
        height=230,
        # Drop a slice's % label rather than shrink every label to fit it.
        uniformtext=dict(minsize=11, mode="hide"),
        annotations=[dict(
            text=(
                f"<b>${total:,.0f}</b><br>"
                f"<span style='font-size:11px;color:{MUTED}'>total</span>"
            ),
            x=0.5, y=0.5, showarrow=False, align="center",
            font=dict(size=17, color=TEXT),
        )],
    )
    return fig


def allocation_pie(holdings_df: pd.DataFrame, by: str) -> go.Figure:
    """Allocation donut for one dimension ("currency" / "exchange" / "platform")."""
    return _allocation_donut(_allocation_data(holdings_df, by))


def allocation_legend_html(agg: pd.DataFrame) -> str:
    """Legend rows beneath a donut: colour key, name, value and share."""
    rows = [
        '<div class="legend-row">'
        f'<span class="legend-key" style="background:{s["color"]};"></span>'
        f'<span class="legend-label" title="{escape(s["label"])}">{escape(s["label"])}</span>'
        f'<span class="legend-val">${s["value"]:,.0f}</span>'
        f'<span class="legend-pct">{s["pct"]:.1f}%</span>'
        "</div>"
        for _, s in agg.iterrows()
    ]
    return '<div class="legend">' + "".join(rows) + "</div>"


# --- Custom HTML holdings table (grouped, expandable per-platform rows) ------

def holdings_table_html(by_code_df: pd.DataFrame, holdings_df: pd.DataFrame) -> str:
    """Render the SWS-style expandable holdings table as raw HTML.

    Each ticker is a clickable parent row; clicking reveals its per-platform
    sub-rows (one per exchange/platform parcel from ``holdings_df``).
    """
    rows: list[str] = []
    for i, g in by_code_df.iterrows():
        code = escape(str(g["code"]))
        n_plat = len(g["platforms"])
        plat_badge = (
            f'<span class="badge">{n_plat} platforms</span>' if n_plat > 1 else ""
        )

        if g["single_currency"]:
            avg_cost = f"{g['avg_cost_local']:,.2f} {escape(g['currency'])}"
            price = f"{g['current_price_local']:,.2f} {escape(g['currency'])}"
        else:
            avg_cost = f"${g['avg_cost_aud']:,.2f} <span class='ccy'>AUD</span>"
            price = f"${g['current_price_aud']:,.2f} <span class='ccy'>AUD</span>"

        pl_cls = _pl_class(g["unrealised_pl_aud"])
        rid = f"grp-{i}"
        rows.append(f"""
        <tr class="parent" data-target="{rid}">
          <td class="hcol"><span class="caret">▸</span><b>{code}</b> {plat_badge}</td>
          <td class="num">{g['units']:,.4f}</td>
          <td class="num">{avg_cost}</td>
          <td class="num">{price}</td>
          <td class="num"><b>{_fmt_money(g['market_value_aud'])}</b></td>
          <td class="num {pl_cls}">{_fmt_pl(g['unrealised_pl_aud'])}</td>
          <td class="num {pl_cls}">{_fmt_pct(g['unrealised_pl_pct'])}</td>
          <td class="num">{g['weight_pct']:.1f}%</td>
        </tr>""")

        # Per-platform sub-rows for this code.
        subs = holdings_df[holdings_df["code"] == g["code"]].sort_values(
            "market_value_aud", ascending=False
        )
        for _, s in subs.iterrows():
            spl = _pl_class(s["unrealised_pl_aud"])
            label = f"{escape(str(s['platform']))} · {escape(str(s['exchange']))}"
            rows.append(f"""
        <tr class="child {rid}" style="display:none">
          <td class="hcol sub">{label}</td>
          <td class="num">{s['units']:,.4f}</td>
          <td class="num">{s['avg_cost_local']:,.2f} {escape(s['currency'])}</td>
          <td class="num">{s['current_price_local']:,.2f} {escape(s['currency'])}</td>
          <td class="num">{_fmt_money(s['market_value_aud'])}</td>
          <td class="num {spl}">{_fmt_pl(s['unrealised_pl_aud'])}</td>
          <td class="num {spl}">{_fmt_pct(s['unrealised_pl_pct'])}</td>
          <td class="num">{s['weight_pct']:.1f}%</td>
        </tr>""")

    return f"""
    <table class="holdings">
      <thead>
        <tr>
          <th class="hcol">Holding</th>
          <th class="num">Units</th>
          <th class="num">Avg Cost</th>
          <th class="num">Price</th>
          <th class="num">Market Value</th>
          <th class="num">Unrealised P/L</th>
          <th class="num">P/L %</th>
          <th class="num">Weight</th>
        </tr>
      </thead>
      <tbody>{''.join(rows)}
      </tbody>
    </table>"""


def _current_fy_label() -> str:
    today = date.today()
    if today.month >= 7:
        start, end = today.year, today.year + 1
    else:
        start, end = today.year - 1, today.year
    return f"FY{str(start)[-2:]}/{str(end)[-2:]}"


# Distinct, colour-blind-friendly hues for benchmark shadow lines.
_BENCH_COLORS = ["#f59e0b", "#8b5cf6", "#0ea5e9", "#ec4899"]


def net_worth_chart(
    snapshot_totals_df: pd.DataFrame,
    performance: dict | None = None,
) -> go.Figure:
    df = snapshot_totals_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")

    fig = go.Figure()

    # Contributions (net invested capital). Drawn first and left unfilled so the
    # Portfolio trace can fill *to it* rather than to zero — the shaded band is
    # then market growth itself, and it survives a zoom. (Filling to zero, as
    # this used to, floods the whole plot area with colour the moment the
    # y-axis stops touching the origin.)
    has_contributions = False
    if performance is not None:
        contrib = performance.get("contributions")
        if contrib is not None and not contrib.empty:
            cdf = contrib.copy()
            cdf["date"] = pd.to_datetime(cdf["date"])
            cdf = cdf.sort_values("date")
            fig.add_trace(go.Scatter(
                x=cdf["date"],
                y=cdf["net_contributed_aud"],
                mode="lines",
                name="Net invested",
                line=dict(color=MUTED, width=1.5, dash="dot"),
                hovertemplate=(
                    "<b>%{x|%d %b %Y}</b><br>"
                    "Net invested: $%{y:,.0f} AUD"
                    "<extra></extra>"
                ),
            ))
            # Both series are built on the same snapshot dates, so the band
            # between them is well defined point-for-point.
            has_contributions = True

    fig.add_trace(go.Scatter(
        x=df["date"],
        y=df["total_value_aud"],
        mode="lines",
        name="Portfolio",
        line=dict(color=TEAL, width=2.5),
        fill="tonexty" if has_contributions else None,
        fillcolor="rgba(20, 184, 166, 0.14)",
        customdata=df["unrealised_pl_aud"],
        hovertemplate=(
            "<b>%{x|%d %b %Y}</b><br>"
            "Value: $%{y:,.0f} AUD<br>"
            "Unrealised P/L: $%{customdata:,.0f} AUD"
            "<extra></extra>"
        ),
    ))

    # Benchmark shadow portfolios: what the same cashflows would be worth if
    # invested in each benchmark instead. Legend-toggleable overlays.
    if performance is not None:
        shadows = performance.get("shadows", {})
        labels = performance.get("bench_labels", {})
        for i, (ticker, shadow) in enumerate(shadows.items()):
            if shadow is None or shadow.empty:
                continue
            sdf = shadow.copy()
            sdf["date"] = pd.to_datetime(sdf["date"])
            sdf = sdf.sort_values("date")
            color = _BENCH_COLORS[i % len(_BENCH_COLORS)]
            fig.add_trace(go.Scatter(
                x=sdf["date"],
                y=sdf["value_aud"],
                mode="lines",
                name=labels.get(ticker, ticker),
                line=dict(color=color, width=1.8),
                hovertemplate=(
                    f"<b>{labels.get(ticker, ticker)}</b> "
                    "%{x|%d %b %Y}<br>"
                    "Shadow value: $%{y:,.0f} AUD"
                    "<extra></extra>"
                ),
            ))

    # The goal projection fan is added client-side by the Goal Planner script —
    # it reacts live to the planner's inputs, so Python doesn't draw it here.

    fig.update_layout(
        showlegend=True,
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
            font=dict(size=11, color=TEXT),
        ),
        paper_bgcolor=PAPER,
        plot_bgcolor=PLOT_BG,
        font=_PLOTLY_FONT,
        # Top margin holds the range-selector buttons and the legend, which now
        # share one row above the plot.
        margin=dict(t=54, l=8, r=8, b=8),
        height=440,
        hovermode="x unified",
        dragmode="zoom",
        yaxis=dict(
            gridcolor=GRID, showgrid=True, tickprefix="$", tickformat=",.0f",
            zeroline=False, color=AXIS,
            # Left free to float: the companion JS refits it to whatever x
            # window is on screen, which is the whole point of cropping.
            fixedrange=False,
        ),
    )
    fig.update_xaxes(
        color=AXIS,
        gridcolor=GRID,
        showspikes=True,
        spikemode="across",
        spikethickness=1,
        spikedash="dot",
        spikecolor=AXIS,
        rangeselector=dict(
            buttons=[
                dict(count=1, label="1M", step="month", stepmode="backward"),
                dict(count=6, label="6M", step="month", stepmode="backward"),
                dict(count=1, label="YTD", step="year", stepmode="todate"),
                dict(count=1, label="1Y", step="year", stepmode="backward"),
                dict(count=5, label="5Y", step="year", stepmode="backward"),
                dict(step="all", label="All"),
            ],
            bgcolor="#f1f5f9",
            activecolor=TEAL,
            font=dict(color=TEXT, size=11),
            x=0, xanchor="left", y=1.02, yanchor="bottom",
        ),
        rangeslider=dict(
            visible=True, thickness=0.08, bgcolor="#f8fafc",
            bordercolor=GRID, borderwidth=1,
            # 'auto' keeps the thumbnail showing the full series; the default
            # 'match' would crop it in step with the (now floating) main y-axis,
            # leaving nothing to aim at.
            yaxis=dict(rangemode="auto"),
        ),
        type="date",
    )
    return fig


# --- Income & risk (Phase 5) ------------------------------------------------

def correlation_heatmap(corr: pd.DataFrame) -> go.Figure:
    """Holdings daily-return correlation matrix as a diverging heatmap."""
    codes = list(corr.columns)
    fig = go.Figure(data=go.Heatmap(
        z=corr.values,
        x=codes, y=codes,
        zmin=-1, zmax=1,
        colorscale=[[0, "#dc2626"], [0.5, "#f6f8fa"], [1, "#0d9488"]],
        colorbar=dict(title="ρ", thickness=12),
        hovertemplate="%{y} / %{x}: ρ = %{z:.2f}<extra></extra>",
    ))
    fig.update_layout(
        margin=dict(t=8, l=0, r=0, b=0), paper_bgcolor=PAPER, plot_bgcolor=PAPER,
        font=_PLOTLY_FONT, height=max(240, 32 * len(codes) + 80),
        yaxis=dict(autorange="reversed"),
    )
    return fig


def _fmt_signed_pct(v) -> str:
    if v is None or pd.isna(v):
        return "—"
    sign = "+" if v >= 0 else "−"
    return f"{sign}{abs(v) * 100:.1f}%"


def risk_cards_html(risk: dict) -> str:
    """Small card row for the headline risk metrics; "" if no history."""
    if not risk or risk.get("annualised_volatility") is None:
        return ""

    vol = risk["annualised_volatility"]
    mdd = risk.get("max_drawdown")
    sharpe = risk.get("sharpe")
    beta = risk.get("beta")
    blabel = risk.get("benchmark_label") or "benchmark"

    def _card(label, value, sub=""):
        sub_html = f'<div class="card-sub">{escape(sub)}</div>' if sub else ""
        return (
            f'<div class="card"><div class="card-label">{escape(label)}</div>'
            f'<div class="card-value">{value}</div>{sub_html}</div>'
        )

    cards = [
        _card("Volatility (p.a.)", f"{vol * 100:.1f}%", "Annualised daily σ"),
    ]
    if mdd is not None:
        cards.append(_card("Max Drawdown", f"{mdd * 100:.1f}%", "Peak to trough"))
    if sharpe is not None:
        cards.append(_card("Sharpe", f"{sharpe:.2f}", "Excess return / σ"))
    if beta is not None:
        cards.append(_card(f"Beta vs {blabel}", f"{beta:.2f}",
                           "Sensitivity to the index"))
    return '<div class="summary-grid">' + "".join(cards) + "</div>"


_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ title }}</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #f6f8fa; color: #1a2b3c;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      padding: 28px; max-width: 1280px; margin: 0 auto;
      -webkit-text-size-adjust: 100%; text-size-adjust: 100%;
    }
    h1 { font-size: 1.7rem; font-weight: 700; margin-bottom: 4px; letter-spacing: -0.01em; }
    .subtitle { color: #64748b; font-size: 0.875rem; margin-bottom: 24px; }
    .section-title { font-size: 1.05rem; font-weight: 650; margin: 0 0 12px 2px; color: #1a2b3c; }
    .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 32px; }
    .card {
      background: #ffffff; border-radius: 14px; padding: 18px 20px;
      border: 1px solid #e7ebf0; box-shadow: 0 1px 3px rgba(16,24,40,0.04);
    }
    .card-label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.07em; color: #94a3b8; margin-bottom: 6px; font-weight: 600; }
    .card-value { font-size: 1.45rem; font-weight: 700; }
    .card-sub { font-size: 0.75rem; color: #94a3b8; margin-top: 2px; }
    .pos { color: #16a34a; }
    .neg { color: #dc2626; }
    .section { margin-bottom: 32px; }
    .panel {
      background: #ffffff; border-radius: 14px; padding: 18px 20px;
      border: 1px solid #e7ebf0; box-shadow: 0 1px 3px rgba(16,24,40,0.04);
    }
    .caption { color: #64748b; font-size: 0.8rem; margin: 4px 0 14px; }

    /* Allocation donuts: equal-height panels, chart on top, legend beneath */
    .alloc-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(268px, 100%), 1fr)); gap: 20px; }
    .alloc-panel { display: flex; flex-direction: column; min-width: 0; }
    .panel-title {
      font-size: 0.72rem; font-weight: 700; color: #64748b; margin-bottom: 6px;
      text-transform: uppercase; letter-spacing: 0.06em;
    }
    .legend { margin-top: 12px; border-top: 1px solid #f0f3f6; padding-top: 6px; }
    .legend-row {
      display: grid; grid-template-columns: 10px minmax(0, 1fr) auto 46px;
      align-items: center; gap: 9px; padding: 4px 0; font-size: 0.8rem;
    }
    .legend-key { width: 10px; height: 10px; border-radius: 3px; }
    .legend-label { color: #334155; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .legend-val { color: #1a2b3c; font-weight: 600; font-variant-numeric: tabular-nums; }
    .legend-pct { color: #94a3b8; font-variant-numeric: tabular-nums; text-align: right; }

    /* Chart toolbar (net worth): controls sit in one row above the plot */
    .chart-toolbar {
      display: flex; flex-wrap: wrap; align-items: center; gap: 8px 18px;
      margin-bottom: 10px; padding-bottom: 10px; border-bottom: 1px solid #f0f3f6;
    }
    .switch { display: inline-flex; align-items: center; gap: 8px; cursor: pointer; user-select: none; }
    .switch input { position: absolute; opacity: 0; width: 0; height: 0; }
    .switch .track {
      width: 34px; height: 19px; border-radius: 999px; background: #cbd5e1;
      position: relative; transition: background 0.15s; flex: none;
    }
    .switch .track::after {
      content: ''; position: absolute; top: 2px; left: 2px; width: 15px; height: 15px;
      border-radius: 50%; background: #ffffff; transition: transform 0.15s;
      box-shadow: 0 1px 2px rgba(16,24,40,0.25);
    }
    .switch input:checked + .track { background: #14b8a6; }
    .switch input:checked + .track::after { transform: translateX(15px); }
    .switch input:focus-visible + .track { outline: 2px solid #0d9488; outline-offset: 2px; }
    .switch .switch-label { font-size: 0.82rem; font-weight: 600; color: #334155; }
    .toolbar-hint { font-size: 0.74rem; color: #94a3b8; margin-left: auto; }
    /* Segmented control (holdings map "Organise by") */
    .seg-wrap { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; }
    .seg-label { font-size: 0.82rem; font-weight: 600; color: #334155; }
    .seg { display: inline-flex; gap: 2px; padding: 3px; border-radius: 10px; background: #f1f5f9; }
    .seg button {
      border: none; background: none; border-radius: 7px; padding: 5px 12px; cursor: pointer;
      font-family: inherit; font-size: 0.8rem; font-weight: 600; color: #64748b;
      transition: background 0.15s, color 0.15s;
    }
    .seg button:hover { color: #1a2b3c; }
    .seg button[aria-pressed="true"] { background: #ffffff; color: #0f766e; box-shadow: 0 1px 2px rgba(16,24,40,0.12); }
    .seg button:focus-visible { outline: 2px solid #0d9488; outline-offset: 1px; }
    .data-warning { background: #fff4e5; border: 1px solid #ffb84d; color: #7a4a00; padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; font-size: 14px; font-weight: 500; }
    .export-bar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
    .export-btn {
      background: #14b8a6; color: white; border: none; border-radius: 9px;
      padding: 10px 18px; font-size: 0.9rem; font-weight: 600; cursor: pointer;
      transition: background 0.15s; box-shadow: 0 1px 2px rgba(20,184,166,0.3);
    }
    .export-btn:hover { background: #0d9488; }

    /* Holdings table */
    table.holdings { width: 100%; border-collapse: collapse; font-size: 0.875rem; }
    table.holdings thead th {
      text-align: right; padding: 10px 14px; color: #94a3b8; font-weight: 600;
      font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em;
      border-bottom: 2px solid #e7ebf0;
    }
    table.holdings th.hcol, table.holdings td.hcol { text-align: left; }
    table.holdings td { padding: 11px 14px; border-bottom: 1px solid #f0f3f6; }
    table.holdings td.num { text-align: right; font-variant-numeric: tabular-nums; }
    tr.parent { cursor: pointer; transition: background 0.1s; }
    tr.parent:hover { background: #f0fdfa; }
    tr.child { background: #fafbfc; font-size: 0.83rem; color: #475569; }
    tr.child td.sub { padding-left: 34px; color: #64748b; }
    .caret { display: inline-block; width: 14px; color: #14b8a6; transition: transform 0.12s; font-size: 0.7rem; }
    .caret.open { transform: rotate(90deg); }
    .badge {
      display: inline-block; background: #e6fffb; color: #0d9488; border-radius: 6px;
      padding: 1px 7px; font-size: 0.68rem; font-weight: 600; margin-left: 6px;
    }
    .ccy { color: #94a3b8; font-size: 0.78em; font-weight: 400; }
    /* Wide tables scroll inside their panel instead of widening the page.
       A no-op wherever the table already fits (i.e. on desktop). */
    .table-panel { overflow-x: auto; -webkit-overflow-scrolling: touch; }

    /* Sticky section nav */
    nav.toc {
      position: sticky; top: 0; z-index: 50;
      background: rgba(246,248,250,0.95); backdrop-filter: blur(4px);
      display: flex; flex-wrap: wrap; gap: 4px 18px;
      padding: 10px 2px; margin-bottom: 20px;
      border-bottom: 1px solid #e7ebf0; font-size: 0.8rem;
    }
    nav.toc a { color: #64748b; text-decoration: none; font-weight: 600; }
    nav.toc a:hover { color: #0d9488; }
    .section { scroll-margin-top: 84px; }

    /* "Show the working" blocks inside expanded rows */
    tr.child td.wrap { padding: 4px 14px 16px; }
    .working {
      background: #f8fafc; border: 1px solid #e7ebf0; border-radius: 10px;
      padding: 12px 16px; font-size: 0.8rem; color: #334155;
    }
    .working .wline {
      display: flex; justify-content: space-between; gap: 24px; padding: 3px 0;
    }
    .working .wlabel { color: #64748b; }
    .working .wval { font-variant-numeric: tabular-nums; white-space: nowrap; text-align: right; }

    /* ATO tax-return figures block (current FY headline numbers) */
    .taxsummary {
      background: #f8fafc; border: 1px solid #e7ebf0; border-radius: 10px;
      padding: 6px 18px; max-width: 560px;
    }
    .taxsummary-fy {
      font-weight: 700; color: #1a2b3c; font-size: 0.8rem; padding: 10px 0 4px;
      letter-spacing: 0.02em;
    }
    .taxsummary .taxrow {
      display: flex; justify-content: space-between; gap: 24px; padding: 7px 0;
      border-bottom: 1px solid #eef2f6; font-size: 0.86rem; color: #334155;
    }
    .taxsummary .taxrow:last-child { border-bottom: none; }
    .taxsummary .taxlabel { color: #475569; }
    .taxsummary .taxval { font-variant-numeric: tabular-nums; white-space: nowrap; text-align: right; }
    .taxsummary .taxtotal {
      border-top: 2px solid #cbd5e1; border-bottom: none; margin-top: 2px;
      padding-top: 10px; font-size: 0.95rem; color: #0f1f2e;
    }

    /* Nested parcel detail tables */
    table.subtable { width: 100%; border-collapse: collapse; font-size: 0.78rem; margin: 6px 0 2px; }
    table.subtable th {
      text-align: right; color: #94a3b8; font-weight: 600; padding: 6px 10px;
      border-bottom: 1px solid #e7ebf0; font-size: 0.66rem;
      text-transform: uppercase; letter-spacing: 0.04em;
    }
    table.subtable td {
      padding: 6px 10px; border-bottom: 1px solid #f0f3f6; text-align: right;
      font-variant-numeric: tabular-nums; color: #475569;
    }
    table.subtable th.l, table.subtable td.l { text-align: left; }
    table.subtable tr:last-child td { border-bottom: none; }
    .subnote { font-size: 0.78rem; color: #64748b; margin-top: 8px; }

    /* FY group headings + current-FY highlight in CGT tables */
    tr.fyhead td { background: #f1f5f9; font-size: 0.8rem; padding: 8px 14px; }
    .fyhead-sub { color: #64748b; font-weight: 400; margin-left: 8px; font-size: 0.75rem; }
    tr.parent.cur-fy { background: #e6fffb; }

    /* Collapsible methodology / detail notes */
    details.method, details.inline-details { margin: 0 0 12px 2px; font-size: 0.8rem; }
    details.method summary, details.inline-details summary {
      cursor: pointer; color: #0d9488; font-weight: 600; user-select: none;
    }
    details.method .method-body {
      margin: 8px 0 4px; color: #64748b; line-height: 1.55; max-width: 860px;
    }
    details.method .method-body p { margin-bottom: 6px; }
    details.inline-details { margin-top: 14px; }
    .subsection-title { font-size: 0.9rem; font-weight: 650; margin: 20px 0 4px; color: #1a2b3c; }

    /* Goal planner (interactive) */
    .goal-planner { display: grid; grid-template-columns: minmax(250px, 330px) 1fr; gap: 26px; }
    .gp-inputs { display: flex; flex-direction: column; gap: 14px; padding-right: 26px; border-right: 1px solid #f0f3f6; }
    .gp-field label, .gp-list-title, .gp-row-head span { display: block; font-size: 0.7rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.07em; color: #94a3b8; margin-bottom: 4px; }
    .gp-field input, .gp-row input {
      width: 100%; border: 1px solid #e7ebf0; border-radius: 9px; padding: 8px 10px;
      font-size: 0.9rem; color: #1a2b3c; background: #fbfcfd;
      font-variant-numeric: tabular-nums; font-family: inherit;
    }
    .gp-field input:focus, .gp-row input:focus { outline: 2px solid #14b8a6; outline-offset: -1px; background: #ffffff; }
    /* Contribution changes / lump sums: editable lists of month + amount rows */
    .gp-list { border-top: 1px solid #f0f3f6; padding-top: 12px; }
    .gp-list-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
    .gp-list-title { margin-bottom: 2px; }
    .gp-hint { font-size: 0.74rem; color: #94a3b8; line-height: 1.4; margin-bottom: 8px; }
    .gp-rows { display: flex; flex-direction: column; gap: 6px; }
    .gp-row, .gp-row-head { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr) 24px; gap: 6px; align-items: center; }
    .gp-row-head span { margin-bottom: 0; font-size: 0.64rem; }
    .gp-row input { padding: 6px 8px; font-size: 0.82rem; }
    .gp-row-note { grid-column: 1 / -1; font-size: 0.72rem; color: #b45309; margin-top: -2px; }
    .gp-add {
      background: none; border: none; padding: 2px 0; font-size: 0.78rem; font-weight: 600;
      color: #0d9488; cursor: pointer; white-space: nowrap; font-family: inherit;
    }
    .gp-add:hover { color: #0f766e; text-decoration: underline; }
    .gp-del {
      width: 24px; height: 24px; border: none; border-radius: 6px; background: none;
      color: #94a3b8; font-size: 1rem; line-height: 1; cursor: pointer;
    }
    .gp-del:hover { color: #dc2626; background: #fef2f2; }
    .gp-reset {
      align-self: flex-start; background: none; border: 1px solid #e7ebf0; border-radius: 8px;
      padding: 6px 12px; font-size: 0.78rem; font-weight: 600; color: #64748b; cursor: pointer;
    }
    .gp-reset:hover { color: #0d9488; border-color: #99f6e4; }
    .goal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; gap: 12px; }
    .goal-name { font-size: 1.02rem; font-weight: 650; color: #1a2b3c; }
    .goal-badge { font-size: 0.68rem; font-weight: 700; padding: 2px 9px; border-radius: 999px; text-transform: uppercase; letter-spacing: 0.03em; white-space: nowrap; }
    .goal-bar { height: 8px; border-radius: 999px; background: #eef2f6; overflow: hidden; margin-bottom: 8px; }
    .goal-bar-fill { height: 100%; border-radius: 999px; transition: width 0.3s, background 0.3s; }
    .goal-progress { font-size: 0.82rem; font-weight: 600; color: #334155; }
    .gp-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 16px; margin-top: 18px; }
    .gp-stat-val { font-size: 1.25rem; font-weight: 700; }
    .gp-plan { margin-top: 22px; padding-top: 14px; border-top: 1px solid #f0f3f6; }
    .gp-plan .card-label { margin-bottom: 4px; }
    .gp-tl-row {
      display: grid; grid-template-columns: 10px minmax(0, 170px) minmax(0, 1fr) auto;
      gap: 10px; align-items: center; padding: 6px 0; font-size: 0.82rem;
      border-bottom: 1px solid #f6f8fa;
    }
    .gp-tl-row:last-child { border-bottom: none; }
    .gp-tl-key { width: 8px; height: 8px; border-radius: 50%; background: #14b8a6; }
    .gp-tl-key.paused { background: #cbd5e1; }
    .gp-tl-key.lump { background: #6366f1; border-radius: 1px; transform: rotate(45deg); }
    .gp-tl-when { color: #64748b; font-variant-numeric: tabular-nums; }
    .gp-tl-what { color: #1a2b3c; font-weight: 600; font-variant-numeric: tabular-nums; }
    .gp-tl-total { color: #94a3b8; font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
    @media (max-width: 760px) {
      .goal-planner { grid-template-columns: 1fr; }
      .gp-inputs { border-right: none; padding-right: 0; border-bottom: 1px solid #f0f3f6; padding-bottom: 18px; }
    }

    /* Pointer-specific wording: "hover"/"drag" on a mouse, "tap" on a touchscreen */
    .on-touch { display: none; }
    @media (hover: none) and (pointer: coarse) {
      .on-mouse { display: none; }
      .on-touch { display: inline; }
      /* Zoom on touch is via the 1M…All buttons; the mode bar's drag tools
         would only re-arm the page-scroll-blocking drag mode. */
      .plotly-graph-div .modebar-container { display: none; }
    }

    /* Phones. Everything here is scoped to narrow screens — desktop is untouched. */
    @media (max-width: 600px) {
      body { padding: 14px 12px 28px; }
      h1 { font-size: 1.35rem; }
      .export-bar { flex-wrap: wrap; gap: 10px 12px; margin-bottom: 12px; }
      .export-bar .subtitle { margin-bottom: 0; }
      .export-btn { padding: 8px 14px; font-size: 0.82rem; }

      /* Nav: one swipeable row rather than a wrapped block pinned over the page */
      nav.toc {
        flex-wrap: nowrap; overflow-x: auto; white-space: nowrap; gap: 18px;
        margin: 0 -12px 16px; padding: 10px 12px; scrollbar-width: none;
      }
      nav.toc::-webkit-scrollbar { display: none; }
      .section { scroll-margin-top: 52px; margin-bottom: 26px; }

      .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-bottom: 26px; }
      .card { padding: 12px 14px; border-radius: 12px; }
      .card-value { font-size: 1.15rem; }
      .panel { padding: 14px 12px; border-radius: 12px; }

      /* Tables: tighter cells, numbers kept on one line (the panel scrolls) */
      table.holdings { font-size: 0.8rem; }
      table.holdings thead th { padding: 8px 10px; font-size: 0.66rem; }
      table.holdings td { padding: 9px 10px; }
      table.holdings td.num { white-space: nowrap; }
      tr.child td.sub { padding-left: 24px; }
      tr.child td.wrap { padding: 4px 6px 12px; }
      .working .wline, .taxsummary .taxrow { gap: 12px; }
      .taxsummary { padding: 4px 12px; }

      /* Pin the first column (stock / FY) while the rest scrolls sideways.
         Full-width colspan cells (FY headings, working blocks) are left alone. */
      .table-panel table.holdings > thead > tr > th:first-child,
      .table-panel table.holdings > tbody > tr > td:first-child:not([colspan]) {
        position: sticky; left: 0; z-index: 1; background: #ffffff;
        box-shadow: inset -1px 0 0 #e7ebf0;
      }
      /* …on one line, so the ▸ caret doesn't wrap above the label */
      .table-panel table.holdings > tbody > tr > td:first-child:not([colspan]) { white-space: nowrap; }
      .table-panel table.holdings > tbody > tr.child > td:first-child:not([colspan]) { background: #fafbfc; }
      .table-panel table.holdings > tbody > tr.cur-fy > td:first-child:not([colspan]) { background: #e6fffb; }

      /* iOS zooms the page into any focused input under 16px */
      .gp-field input, .gp-row input { font-size: 16px; }
      .toolbar-hint { margin-left: 0; }
      /* The mode bar collides with the 1M…All buttons at this width */
      .plotly-graph-div .modebar-container { display: none; }
    }
{{ targets_css }}
  </style>
</head>
<body>

{% if data_warning %}<div class="data-warning">⚠️ {{ data_warning }}</div>{% endif %}
<noscript><div class="data-warning">This viewer has JavaScript turned off, so the charts,
  expandable rows and Goal Planner won't work. The figures and tables below are
  still complete — open the file in a web browser for the full dashboard.</div></noscript>
<div class="data-warning" id="charts-offline" hidden>Charts couldn't load — they
  need an internet connection the first time the page opens. The figures and tables
  below are still complete.</div>

<div class="export-bar">
  <div>
    <h1>{{ title }}</h1>
    <p class="subtitle">Last updated: {{ updated }}</p>
  </div>
  {% if brief_json %}<button class="export-btn" onclick="exportBrief()">Export for Claude ↓</button>{% endif %}
</div>

<nav class="toc">
  {% for link in nav_links %}<a href="#{{ link.anchor }}">{{ link.label }}</a>{% endfor %}
</nav>

<div class="summary-grid">
  <div class="card">
    <div class="card-label">Total Value</div>
    <div class="card-value">{{ total_value }}</div>
  </div>
  <div class="card">
    <div class="card-label">Unrealised P/L</div>
    <div class="card-value {{ pl_class }}">{{ total_pl }}</div>
    <div class="card-sub {{ pl_class }}">{{ total_pl_pct }}</div>
  </div>
  <div class="card">
    <div class="card-label">Holdings</div>
    <div class="card-value">{{ num_assets }}</div>
    <div class="card-sub">{{ num_positions }} parcels</div>
  </div>
  {% if crypto_pct is not none %}
  <div class="card">
    <div class="card-label">Crypto Exposure</div>
    <div class="card-value">{{ crypto_pct }}</div>
  </div>
  {% endif %}
  {% if realised_fy is not none %}
  <div class="card">
    <div class="card-label">Realised P/L ({{ cur_fy }})</div>
    <div class="card-value {{ realised_fy_class }}">{{ realised_fy }}</div>
  </div>
  {% endif %}
  {% if period_change is not none %}
  <div class="card">
    <div class="card-label">Change (12M)</div>
    <div class="card-value {{ period_change_class }}">{{ period_change }}</div>
    <div class="card-sub {{ period_change_class }}">{{ period_change_pct }}</div>
  </div>
  {% endif %}
  {% if xirr_all is not none %}
  <div class="card">
    <div class="card-label">XIRR (all-time)</div>
    <div class="card-value {{ xirr_all_class }}">{{ xirr_all }}</div>
    <div class="card-sub">Money-weighted return p.a.</div>
  </div>
  {% endif %}
  {% if vs_bench is not none %}
  <div class="card">
    <div class="card-label">vs {{ vs_bench_label }} (12M)</div>
    <div class="card-value {{ vs_bench_class }}">{{ vs_bench }}</div>
    <div class="card-sub">Excess TWR p.a.</div>
  </div>
  {% endif %}
</div>

<div class="section" id="sec-goal">
  <p class="section-title">Goal Planner</p>
  <p class="caption">
    Set a target and see whether you're on track: today's value plus your
    contributions — a monthly amount that can change over time, and any one-off
    lump sums — compounded at the expected return. Settings are saved in this
    browser. Deterministic estimate — not financial advice.
  </p>
  <div class="panel goal-planner">
    <div class="gp-inputs">
      <div class="gp-field">
        <label for="gp-target">Goal amount (AUD)</label>
        <input type="number" id="gp-target" min="0" step="1000">
      </div>
      <div class="gp-field">
        <label for="gp-date">Target date</label>
        <input type="month" id="gp-date">
      </div>
      <div class="gp-field">
        <label for="gp-return">Expected return (% p.a.)</label>
        <input type="number" id="gp-return" min="-20" max="30" step="0.5">
      </div>
      <div class="gp-field">
        <label for="gp-monthly">Monthly contribution (AUD)</label>
        <input type="number" id="gp-monthly" min="0" step="100">
      </div>
      <div class="gp-list">
        <div class="gp-list-head">
          <span class="gp-list-title">Contribution changes</span>
          <button class="gp-add" id="gp-add-change" type="button">+ Add change</button>
        </div>
        <p class="gp-hint">A new monthly amount from a given month onwards — e.g. after a pay rise, or $0 for a break.</p>
        <div class="gp-rows" id="gp-changes">
          <div class="gp-row-head" hidden><span>From</span><span>$ / month</span></div>
        </div>
      </div>
      <div class="gp-list">
        <div class="gp-list-head">
          <span class="gp-list-title">Lump sums</span>
          <button class="gp-add" id="gp-add-lump" type="button">+ Add lump sum</button>
        </div>
        <p class="gp-hint">One-off amounts added in a given month. Use a negative amount for a withdrawal.</p>
        <div class="gp-rows" id="gp-lumps">
          <div class="gp-row-head" hidden><span>Month</span><span>Amount $</span></div>
        </div>
      </div>
      <button class="gp-reset" id="gp-reset" type="button">Reset to defaults</button>
    </div>
    <div class="gp-results">
      <div class="goal-head">
        <span class="goal-name" id="gp-summary"></span>
        <span class="goal-badge" id="gp-status"></span>
      </div>
      <div class="goal-bar"><div class="goal-bar-fill" id="gp-bar"></div></div>
      <div class="goal-progress" id="gp-progress"></div>
      <div class="gp-stats">
        <div class="gp-stat">
          <div class="card-label">Projected at target date</div>
          <div class="gp-stat-val" id="gp-projected">—</div>
          <div class="card-sub" id="gp-range"></div>
        </div>
        <div class="gp-stat">
          <div class="card-label">Required return</div>
          <div class="gp-stat-val" id="gp-required">—</div>
          <div class="card-sub">to exactly hit the target with your contributions</div>
        </div>
        <div class="gp-stat">
          <div class="card-label">Gap at target date</div>
          <div class="gp-stat-val" id="gp-gap">—</div>
          <div class="card-sub" id="gp-lever"></div>
        </div>
        <div class="gp-stat">
          <div class="card-label">You'll contribute</div>
          <div class="gp-stat-val" id="gp-inflows">—</div>
          <div class="card-sub" id="gp-inflows-split"></div>
        </div>
      </div>
      <div class="gp-plan" id="gp-plan" hidden>
        <div class="card-label">Your contribution plan</div>
        <div id="gp-timeline"></div>
      </div>
    </div>
  </div>
</div>

<div class="section" id="sec-map">
  <p class="section-title">Holdings Map</p>
  <div class="panel">
    <div class="chart-toolbar">
      {% if map_views|length > 1 %}
      <div class="seg-wrap">
        <span class="seg-label" id="map-view-label">Organise by</span>
        <div class="seg" role="group" aria-labelledby="map-view-label" id="map-view">
          {% for v in map_views %}<button type="button" data-view="{{ v.key }}" aria-pressed="{{ 'true' if loop.first else 'false' }}">{{ v.label }}</button>{% endfor %}
        </div>
      </div>
      {% endif %}
      <span class="toolbar-hint on-mouse">Colour is unrealised P/L % · click a tile to zoom in, the bar above to zoom out</span>
      <span class="toolbar-hint on-touch">Colour is unrealised P/L % · tap a tile to zoom in</span>
    </div>
    {{ treemap_html }}
  </div>
</div>

<div class="section" id="sec-holdings">
  <p class="section-title">Holdings Detail</p>
  <p class="caption"><span class="on-mouse">Click</span><span class="on-touch">Tap</span> a row to expand its per-platform parcels.</p>
  <div class="panel table-panel">{{ holdings_table_html }}</div>
</div>

<div class="section" id="sec-alloc">
  <p class="section-title">Allocation</p>
  <p class="caption">Share of {{ total_value }} total market value. <span class="on-mouse">Hover</span><span class="on-touch">Tap</span> a slice for its dollar amount.</p>
  <div class="alloc-grid">
    {% for p in allocation_panels %}
    <div class="panel alloc-panel">
      <p class="panel-title">{{ p.title }}</p>
      {{ p.chart_html }}
      {{ p.legend_html }}
    </div>
    {% endfor %}
  </div>
</div>

{% if targets_json %}{{ targets_html }}{% endif %}

{% if net_worth_html %}
<div class="section" id="sec-history">
  <p class="section-title">Net Worth History</p>
  {% if net_worth_caption %}<p class="caption">{{ net_worth_caption }}</p>{% endif %}
  <div class="panel">
    <div class="chart-toolbar">
      <label class="switch">
        <input type="checkbox" id="nw-goals-toggle">
        <span class="track"></span>
        <span class="switch-label">Show goal &amp; projection</span>
      </label>
      <span class="toolbar-hint on-mouse">Drag to zoom · double-click to reset · the $ axis refits to the visible range</span>
      <span class="toolbar-hint on-touch">Tap for values · 1M–All buttons to zoom · the $ axis refits to the visible range</span>
    </div>
    {{ net_worth_html }}
  </div>
</div>
{% endif %}

{% if performance_table_html %}
<div class="section" id="sec-perf">
  <p class="section-title">Performance</p>
  <details class="method"><summary>ⓘ How is this calculated?</summary>
    <div class="method-body">
      <p><b>TWR (time-weighted return)</b> chain-links the return of each period
      between snapshots, with your deposits and withdrawals stripped out of each
      period's return. It measures how well the <i>investments</i> performed,
      independent of when you happened to add money — which makes it the fair
      comparison against a benchmark.</p>
      <p><b>Shadow portfolios</b> replay your exact buy/sell cashflows into each
      benchmark ETF at its closing price on the same dates — "what if every trade
      had bought the index instead". <b>Excess</b> is portfolio TWR minus the
      primary benchmark's shadow TWR, both annualised.</p>
      <p><b>XIRR</b> (money-weighted return) is the single annual rate that makes
      all your dated cashflows plus today's value net to zero — it <i>does</i>
      reward good timing. Click any window row to see the inputs behind it.
      Rough estimates, not audited performance figures.</p>
    </div>
  </details>
  <p class="caption">Click a window row to see the values, contributions and annualisation behind it.</p>
  <div class="panel table-panel">
    {{ performance_table_html }}
    {{ xirr_details_html }}
  </div>
</div>
{% endif %}

{% if gains_table_html %}
<div class="section" id="sec-cgt">
  <p class="section-title">Realised Capital Gains by Financial Year</p>
  {% if cgt_tax_summary_html %}
  <p class="subsection-title">Current-year figures for your tax return</p>
  <p class="caption">
    The headline numbers for this financial year, labelled as they appear on the
    ATO CGT schedule. Losses are shown as positive amounts (the way the fields are
    entered). These are per-parcel figures — gains and losses do not net against
    each other, so a stock can appear in both. Rough estimate — not tax advice.
  </p>
  <div class="panel">{{ cgt_tax_summary_html }}</div>
  {% endif %}
  <details class="method"><summary>ⓘ How is this calculated?</summary>
    <div class="method-body">
      <p><b>Parcel matching:</b> each sale is matched to your oldest open parcels
      first (FIFO). A parcel's cost base uses the exchange rate on its <i>buy</i>
      date; proceeds use the rate on the <i>sale</i> date, so currency moves are
      part of the gain, as the ATO requires.</p>
      <p><b>Discount:</b> parcels held longer than 12 months qualify for the 50%
      CGT discount on gains (losses are never discounted).</p>
      <p><b>Loss ordering:</b> capital losses (this FY's plus any carried
      forward) are applied against non-discountable gains first — that's the
      taxpayer-optimal order the ATO permits — then against discountable gains
      <i>before</i> the 50% discount. Unused losses carry forward. Click a FY row
      to see this arithmetic with your numbers. Rough estimate — not tax advice.</p>
    </div>
  </details>
  <p class="caption">
    * Click a FY row to see the working. Est. Taxable Gain applies losses first, then the 50% discount. Rough estimate — not tax advice.
  </p>
  <div class="panel table-panel">{{ gains_table_html }}</div>
  {% if cgt_disposals_html %}
  <p class="subsection-title">Disposals by Stock (tax return detail)</p>
  <p class="caption">
    Every disposal grouped by financial year and stock. Click a stock to see each
    parcel's acquisition/sale dates, cost base, proceeds and discount eligibility —
    the figures a tax return (or your accountant) needs.
  </p>
  <div class="panel table-panel">{{ cgt_disposals_html }}</div>
  {% endif %}
</div>
{% endif %}

{% if if_sold_today_html %}
<div class="section" id="sec-sold">
  <p class="section-title">If Sold Today</p>
  <details class="method"><summary>ⓘ How is this calculated?</summary>
    <div class="method-body">
      <p>A hypothetical full disposal of each holding at today's price and
      exchange rate, using the same parcel engine as the realised CGT table.
      <b>FIFO</b> books the oldest parcels first (what the tracker assumes).
      <b>Min-tax order</b> instead picks the parcels that minimise the estimated
      taxable gain — realising losses and discount-eligible parcels first —
      and the difference is the potential saving from choosing parcels
      deliberately. Hypothetical only — not advice.</p>
    </div>
  </details>
  <p class="caption">Click a holding to see its per-parcel breakdown at today's price and FX.</p>
  <div class="panel table-panel">{{ if_sold_today_html }}</div>
</div>
{% endif %}

{% if income_table_html %}
<div class="section" id="sec-income">
  <p class="section-title">Dividend Income by Financial Year</p>
  <details class="method"><summary>ⓘ How is this calculated?</summary>
    <div class="method-body">
      <p>Each holding's historical dividends are replayed against the units you
      held on every ex-dividend date, converted to AUD at the ex-date exchange
      rate. Franking credits appear when a franking CSV is provided; grossed-up
      income adds those credits to cash dividends. Rough estimate — not tax
      advice.</p>
    </div>
  </details>
  <p class="caption">
    Cash dividends replayed against units held on each ex-date, converted at ex-date FX.{{ income_yield_caption }} Rough estimate — not tax advice.
  </p>
  <div class="panel table-panel">{{ income_table_html }}</div>
</div>
{% endif %}

{% if risk_cards_html %}
<div class="section" id="sec-risk">
  <p class="section-title">Risk (trailing ~3 years)</p>
  <details class="method"><summary>ⓘ How is this calculated?</summary>
    <div class="method-body">
      <p><b>Volatility</b> is the standard deviation of daily portfolio returns,
      annualised (×√252). <b>Max drawdown</b> is the largest peak-to-trough fall
      in the value series. <b>Sharpe</b> divides the portfolio's return above the
      RBA cash rate by its volatility. <b>Beta</b> regresses daily portfolio
      returns on the primary benchmark's — above 1 means the portfolio amplifies
      index moves. Estimates from snapshot history.</p>
    </div>
  </details>
  <p class="caption">Annualised from the daily portfolio value series. Estimates only.</p>
  {{ risk_cards_html }}
  {% if correlation_html %}
  <p class="caption" style="margin-top:12px;">Holdings return correlation — red pairs move oppositely, teal pairs move together (weaker diversification).</p>
  <div class="panel">{{ correlation_html }}</div>
  {% endif %}
</div>
{% endif %}

<!-- Its own block, so both page scripts below can rely on it. -->
<script>
// Namespace for the settings saved in localStorage (goal planner, targets).
// Local dashboards use their file path, so the demo keeps its own; the web
// edition shows every dashboard at one path, so it names them instead.
var STORAGE_KEY = {{ storage_key_js }};
</script>
<script>
// One delegated toggle for every expandable table row: a tr.parent with
// data-target="ID" shows/hides all tr.child rows carrying class ID.
document.addEventListener('click', function (e) {
  const parent = e.target.closest('tr.parent[data-target]');
  if (!parent) return;
  const id = parent.dataset.target;
  const children = document.querySelectorAll('tr.child.' + CSS.escape(id));
  let opening = false;
  children.forEach(c => {
    opening = c.style.display === 'none';
    c.style.display = opening ? 'table-row' : 'none';
  });
  const caret = parent.querySelector('.caret');
  if (caret) caret.classList.toggle('open', opening);
});

// --- Goal planner -----------------------------------------------------------
// All goal maths runs client-side against the rendered portfolio value, so the
// inputs react instantly and survive re-renders via localStorage (keyed by
// STORAGE_KEY, so the demo dashboard keeps its own goal).
var GOAL_CURRENT_VALUE = {{ current_value_js }};

(function () {
  var els = {
    target: document.getElementById('gp-target'),
    date: document.getElementById('gp-date'),
    monthly: document.getElementById('gp-monthly'),
    ret: document.getElementById('gp-return'),
    reset: document.getElementById('gp-reset'),
  };
  if (!els.target) return;

  var BAND = 0.04;  // ± percentage points either side of the expected return
  var KEY = 'sharesTracker.goal.' + STORAGE_KEY;

  // The two editable month + amount lists. Both store {date: 'YYYY-MM', amount}.
  var LISTS = {
    changes: {
      rows: document.getElementById('gp-changes'),
      add: document.getElementById('gp-add-change'),
      dateLabel: 'Change from month',
      amountLabel: 'New monthly contribution (AUD)',
      min: '0',
    },
    lumps: {
      rows: document.getElementById('gp-lumps'),
      add: document.getElementById('gp-add-lump'),
      dateLabel: 'Lump sum month',
      amountLabel: 'Lump sum amount (AUD, negative to withdraw)',
      min: null,
    },
  };

  // Months as integer indices (year * 12 + month) so month arithmetic is exact.
  function monthIndex(v) {
    var p = (v || '').split('-');
    if (p.length < 2) return null;
    var y = Number(p[0]), m = Number(p[1]);
    return (isFinite(y) && m >= 1 && m <= 12) ? y * 12 + (m - 1) : null;
  }
  function monthString(idx) {
    return Math.floor(idx / 12) + '-' + String(idx % 12 + 1).padStart(2, '0');
  }
  function thisMonth() {
    var d = new Date();
    return d.getFullYear() * 12 + d.getMonth();
  }

  function defaults() {
    return {
      target: 200000,
      date: monthString(thisMonth() + 60),
      monthly: 1000,
      ret: 7,
      changes: [],
      lumps: [],
    };
  }

  function cleanList(v) {
    if (!Array.isArray(v)) return [];
    return v.filter(function (x) {
      return x && typeof x.date === 'string' && isFinite(x.amount);
    });
  }
  function loadSettings() {
    try {
      var s = JSON.parse(localStorage.getItem(KEY));
      if (s && isFinite(s.target) && typeof s.date === 'string') {
        // Settings saved before the lists existed simply have none.
        s.changes = cleanList(s.changes);
        s.lumps = cleanList(s.lumps);
        return s;
      }
    } catch (e) {}
    return defaults();
  }
  function saveSettings(s) {
    try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) {}
  }

  function rowsOf(list) {
    return Array.prototype.slice.call(list.rows.querySelectorAll('.gp-row'));
  }
  function readList(list) {
    return rowsOf(list).map(function (row) {
      return {
        date: row.querySelector('[data-f="date"]').value,
        amount: Number(row.querySelector('[data-f="amount"]').value) || 0,
      };
    });
  }
  function addRow(list, item) {
    var row = document.createElement('div');
    row.className = 'gp-row';
    row.innerHTML =
      '<input type="month" data-f="date">' +
      '<input type="number" data-f="amount" step="100">' +
      '<button class="gp-del" type="button" title="Remove">×</button>' +
      '<div class="gp-row-note" hidden></div>';
    var date = row.querySelector('[data-f="date"]');
    var amount = row.querySelector('[data-f="amount"]');
    date.value = item.date;
    date.setAttribute('aria-label', list.dateLabel);
    amount.value = item.amount;
    amount.setAttribute('aria-label', list.amountLabel);
    if (list.min !== null) amount.min = list.min;
    row.querySelector('.gp-del').setAttribute('aria-label', 'Remove');
    list.rows.appendChild(row);
    syncListHead(list);
    return row;
  }
  function writeList(list, items) {
    rowsOf(list).forEach(function (row) { row.remove(); });
    items.forEach(function (item) { addRow(list, item); });
    syncListHead(list);
  }
  function syncListHead(list) {
    list.rows.querySelector('.gp-row-head').hidden = rowsOf(list).length === 0;
  }

  function readInputs() {
    return {
      target: Number(els.target.value) || 0,
      date: els.date.value,
      monthly: Number(els.monthly.value) || 0,
      ret: Number(els.ret.value) || 0,
      changes: readList(LISTS.changes),
      lumps: readList(LISTS.lumps),
    };
  }
  function writeInputs(s) {
    els.target.value = s.target;
    els.date.value = s.date;
    els.monthly.value = s.monthly;
    els.ret.value = s.ret;
    writeList(LISTS.changes, s.changes || []);
    writeList(LISTS.lumps, s.lumps || []);
  }

  function parseMonth(v) {
    var idx = monthIndex(v);
    return idx === null ? null : new Date(Math.floor(idx / 12), idx % 12, 1);
  }

  // The contribution schedule, in months from now. Contribution k (k = 1..n)
  // lands at the end of month k; a change dated month M sets the amount for
  // every contribution from k = M − this month onwards (so a change dated this
  // month or earlier already applies). Lump sums dated this month count as
  // added today; earlier ones are assumed to be in today's value already.
  function buildPlan(s) {
    var now = thisMonth();
    var steps = [], lumps = [];
    (s.changes || []).forEach(function (c) {
      var m = monthIndex(c.date);
      if (m !== null) steps.push({ k: m - now, amount: c.amount });
    });
    steps.sort(function (a, b) { return a.k - b.k; });
    (s.lumps || []).forEach(function (l) {
      var m = monthIndex(l.date);
      if (m !== null && m >= now && l.amount) lumps.push({ k: m - now, amount: l.amount });
    });
    return { base: s.monthly, steps: steps, lumps: lumps };
  }

  function contributionAt(plan, k) {
    var amount = plan.base;
    for (var i = 0; i < plan.steps.length && plan.steps[i].k <= k; i++) {
      amount = plan.steps[i].amount;
    }
    return amount;
  }

  // Number of monthly contributions made before the target date.
  function horizonMonths(years) {
    return years > 0 ? Math.round(years * 12) : 0;
  }

  // Future value after `years` at `r` p.a.: today's value, plus each monthly
  // contribution compounded for the months left after it lands, plus each
  // lump sum compounded from its month. With no changes or lump sums this is
  // exactly the old closed form (lump + end-of-month annuity).
  function projectValue(cur, plan, r, years) {
    years = Math.max(0, years);
    var n = horizonMonths(years);
    var g = Math.pow(1 + r, 1 / 12);
    var contributed = 0;
    for (var k = 1; k <= n; k++) contributed = contributed * g + contributionAt(plan, k);
    var v = cur * Math.pow(1 + r, years) + contributed;
    plan.lumps.forEach(function (l) {
      if (l.k <= n) v += l.amount * Math.pow(1 + r, Math.max(0, years - l.k / 12));
    });
    return v;
  }

  // Undiscounted totals paid in before the target date (the "You'll contribute" stat).
  function plannedInflows(plan, years) {
    var n = horizonMonths(years);
    var monthly = 0, lumpsIn = 0, lumpsOut = 0;
    for (var k = 1; k <= n; k++) monthly += contributionAt(plan, k);
    plan.lumps.forEach(function (l) {
      if (l.k > n) return;
      if (l.amount > 0) lumpsIn += l.amount; else lumpsOut -= l.amount;
    });
    return { monthly: monthly, lumpsIn: lumpsIn, lumpsOut: lumpsOut, months: n,
             total: monthly + lumpsIn - lumpsOut };
  }

  // The plan as the maths reads it: runs of equal monthly contributions plus
  // the lump sums inside the horizon, in date order (for "Your contribution plan").
  function planTimeline(plan, years) {
    var n = horizonMonths(years), now = thisMonth(), items = [], start = 1;
    for (var k = 1; k <= n; k++) {
      var amount = contributionAt(plan, k);
      if (k === n || contributionAt(plan, k + 1) !== amount) {
        items.push({ k: start, kind: 'period', from: now + start, to: now + k,
                     amount: amount, months: k - start + 1 });
        start = k + 1;
      }
    }
    plan.lumps.forEach(function (l) {
      if (l.k <= n) items.push({ k: l.k, kind: 'lump', from: now + l.k, amount: l.amount });
    });
    return items.sort(function (a, b) { return a.k - b.k; });
  }

  // Annual return that exactly hits the target (bisection); null if unreachable.
  function requiredCagr(cur, plan, target, years) {
    if (years <= 0) return null;
    function f(r) { return projectValue(cur, plan, r, years) - target; }
    var lo = -0.99, hi = 5.0, fLo = f(lo), fHi = f(hi);
    if (fLo === 0) return lo;
    if (fHi === 0) return hi;
    if (fLo * fHi > 0) return null;
    for (var i = 0; i < 200; i++) {
      var mid = (lo + hi) / 2, fMid = f(mid);
      if (Math.abs(fMid) < 1e-6) return mid;
      if (fLo * fMid < 0) { hi = mid; fHi = fMid; } else { lo = mid; fLo = fMid; }
    }
    return (lo + hi) / 2;
  }

  function evaluate(s) {
    var targetD = parseMonth(s.date);
    var years = targetD ? (targetD - new Date()) / (365.25 * 86400000) : null;
    var r = s.ret / 100;
    var plan = buildPlan(s);
    var ev = { years: years, r: r, targetDate: targetD, plan: plan };
    ev.progress = s.target > 0 ? GOAL_CURRENT_VALUE / s.target * 100 : NaN;
    if (years === null || years <= 0) return ev;
    ev.projected = projectValue(GOAL_CURRENT_VALUE, plan, r, years);
    ev.low = projectValue(GOAL_CURRENT_VALUE, plan, Math.max(-0.99, r - BAND), years);
    ev.high = projectValue(GOAL_CURRENT_VALUE, plan, r + BAND, years);
    ev.gap = ev.projected - s.target;
    var tol = Math.max(1, Math.abs(s.target) * 0.02);
    ev.status = ev.gap > tol ? 'ahead' : (ev.gap < -tol ? 'behind' : 'on-track');
    ev.required = requiredCagr(GOAL_CURRENT_VALUE, plan, s.target, years);
    ev.inflows = plannedInflows(plan, years);
    if (ev.status === 'behind') {
      // Future value is linear in the contributions, so the shortfall divided
      // by what $1/month (or $1 today) grows to is the top-up that closes it.
      var shortfall = -ev.gap;
      var perMonthly = projectValue(0, { base: 1, steps: [], lumps: [] }, r, years);
      if (perMonthly > 0) ev.extraMonthly = shortfall / perMonthly;
      ev.extraToday = shortfall / Math.pow(1 + r, years);
    }
    return ev;
  }

  // Flag rows that are ignored or behave differently from what's typed.
  function annotateRows(ev) {
    var now = thisMonth();
    var n = ev.years > 0 ? horizonMonths(ev.years) : null;
    function annotate(list, noteFor) {
      rowsOf(list).forEach(function (row) {
        var m = monthIndex(row.querySelector('[data-f="date"]').value);
        var text = m === null ? 'Pick a month — ignored until set.' : noteFor(m - now);
        var note = row.querySelector('.gp-row-note');
        note.textContent = text || '';
        note.hidden = !text;
      });
    }
    annotate(LISTS.changes, function (k) {
      if (k <= 0) return 'Already in effect — replaces the monthly amount above.';
      if (n !== null && k > n) return 'After the target date — no effect.';
      return '';
    });
    annotate(LISTS.lumps, function (k) {
      if (k < 0) return "In the past — ignored (already in today's value).";
      if (n !== null && k > n) return 'After the target date — not counted.';
      return '';
    });
  }

  var STATUS_STYLE = {
    'ahead':    ['#16a34a', '#dcfce7', 'Ahead'],
    'on-track': ['#0d9488', '#ccfbf1', 'On track'],
    'behind':   ['#dc2626', '#fee2e2', 'Behind'],
    'past':     ['#64748b', '#e2e8f0', 'Date passed'],
  };

  function fmtMoney(v) {
    return (v < 0 ? '−$' : '$') + Math.round(Math.abs(v)).toLocaleString('en-AU');
  }
  function fmtMonth(d) {
    return d ? d.toLocaleDateString('en-AU', { month: 'short', year: 'numeric' }) : '—';
  }
  function monthLabel(idx) {
    return fmtMonth(new Date(Math.floor(idx / 12), idx % 12, 1));
  }

  function render() {
    var s = readInputs();
    var ev = evaluate(s);
    var byId = function (id) { return document.getElementById(id); };

    byId('gp-summary').textContent = fmtMoney(s.target) + ' by ' + fmtMonth(ev.targetDate);

    var past = !(ev.years > 0);
    var st = STATUS_STYLE[past ? 'past' : ev.status];
    var badge = byId('gp-status');
    badge.textContent = st[2];
    badge.style.color = st[0];
    badge.style.background = st[1];

    var pct = isFinite(ev.progress) ? Math.max(0, Math.min(100, ev.progress)) : 0;
    var bar = byId('gp-bar');
    bar.style.width = pct.toFixed(1) + '%';
    bar.style.background = st[0];
    byId('gp-progress').textContent = isFinite(ev.progress)
      ? ev.progress.toFixed(0) + '% there today · ' + fmtMoney(GOAL_CURRENT_VALUE) +
        ' of ' + fmtMoney(s.target)
      : 'Enter a goal amount above zero';

    annotateRows(ev);

    if (past) {
      byId('gp-projected').textContent = '—';
      byId('gp-range').textContent = 'Pick a target date in the future.';
      byId('gp-required').textContent = '—';
      byId('gp-gap').textContent = '—';
      byId('gp-lever').textContent = '';
      byId('gp-inflows').textContent = '—';
      byId('gp-inflows-split').textContent = '';
      byId('gp-plan').hidden = true;
      return;
    }

    byId('gp-projected').textContent = fmtMoney(ev.projected);
    byId('gp-range').textContent =
      fmtMoney(ev.low) + ' – ' + fmtMoney(ev.high) + ' at ' +
      s.ret.toFixed(1) + '% ± ' + (BAND * 100).toFixed(0) + 'pp p.a.';

    byId('gp-required').textContent =
      ev.required === null ? '—' : (ev.required * 100).toFixed(1) + '% p.a.';

    var gapEl = byId('gp-gap');
    gapEl.textContent = (ev.gap >= 0 ? '+' : '') + fmtMoney(ev.gap);
    gapEl.className = 'gp-stat-val ' + (ev.gap >= 0 ? 'pos' : 'neg');

    var lever = byId('gp-lever');
    if (ev.status === 'behind') {
      lever.textContent = ev.extraMonthly != null
        ? 'An extra ' + fmtMoney(ev.extraMonthly) + '/month — or ' +
          fmtMoney(ev.extraToday) + ' as a lump sum today — closes the gap.'
        : 'A lump sum of ' + fmtMoney(ev.extraToday) + ' today closes the gap.';
    } else if (ev.status === 'ahead') {
      lever.textContent = 'Projected to exceed the target.';
    } else {
      lever.textContent = 'On the central projection.';
    }

    var inf = ev.inflows;
    var growth = ev.projected - GOAL_CURRENT_VALUE - inf.total;
    byId('gp-inflows').textContent = fmtMoney(inf.total);
    byId('gp-inflows-split').textContent =
      fmtMoney(inf.monthly) + ' over ' + inf.months + ' months' +
      (inf.lumpsIn ? ' + ' + fmtMoney(inf.lumpsIn) + ' in lump sums' : '') +
      (inf.lumpsOut ? ' − ' + fmtMoney(inf.lumpsOut) + ' withdrawn' : '') +
      ' · ' + fmtMoney(growth) + ' projected growth';

    renderTimeline(s, ev);
  }

  function renderTimeline(s, ev) {
    var box = document.getElementById('gp-plan');
    var show = ev.years > 0 && (s.changes.length > 0 || s.lumps.length > 0);
    box.hidden = !show;
    if (!show) return;
    var list = document.getElementById('gp-timeline');
    list.textContent = '';
    planTimeline(ev.plan, ev.years).forEach(function (it) {
      var when, what, total = '', key = 'gp-tl-key';
      if (it.kind === 'lump') {
        key += ' lump';
        when = monthLabel(it.from) + (it.k === 0 ? ' (today)' : '');
        what = it.amount > 0 ? 'Lump sum +' + fmtMoney(it.amount) : 'Withdrawal ' + fmtMoney(it.amount);
      } else {
        if (it.amount === 0) key += ' paused';
        when = it.months === 1 ? monthLabel(it.from) : monthLabel(it.from) + ' – ' + monthLabel(it.to);
        what = it.amount === 0 ? 'Paused' : fmtMoney(it.amount) + ' / month';
        total = it.months + (it.months === 1 ? ' month' : ' months') +
          (it.amount ? ' · ' + fmtMoney(it.amount * it.months) : '');
      }
      var row = document.createElement('div');
      row.className = 'gp-tl-row';
      [[key, ''], ['gp-tl-when', when], ['gp-tl-what', what], ['gp-tl-total', total]]
        .forEach(function (c) {
          var span = document.createElement('span');
          span.className = c[0];
          span.textContent = c[1];
          row.appendChild(span);
        });
      list.appendChild(row);
    });
  }

  function onChange() {
    saveSettings(readInputs());
    render();
    document.dispatchEvent(new CustomEvent('goal-updated'));
  }

  ['target', 'date', 'monthly', 'ret'].forEach(function (k) {
    els[k].addEventListener('input', onChange);
  });
  els.reset.addEventListener('click', function () {
    writeInputs(defaults());
    onChange();
  });

  // New rows start a year after the list's latest month; a new contribution
  // change carries forward the amount in effect by then, so it's a no-op
  // until edited.
  function newRowFor(key) {
    var s = readInputs();
    var items = s[key];
    var last = items.reduce(function (acc, it) {
      var m = monthIndex(it.date);
      return m !== null && m > acc ? m : acc;
    }, thisMonth());
    var date = last + 12;
    var amount = key === 'lumps'
      ? 10000
      : contributionAt(buildPlan(s), date - thisMonth());
    return { date: monthString(date), amount: amount };
  }

  Object.keys(LISTS).forEach(function (key) {
    var list = LISTS[key];
    list.rows.addEventListener('input', onChange);
    list.rows.addEventListener('click', function (e) {
      var del = e.target.closest('.gp-del');
      if (!del) return;
      del.closest('.gp-row').remove();
      syncListHead(list);
      onChange();
    });
    list.add.addEventListener('click', function () {
      var row = addRow(list, newRowFor(key));
      onChange();
      var amount = row.querySelector('[data-f="amount"]');
      amount.focus();
      amount.select();
    });
  });

  // The net-worth chart script reads this to draw the projection fan.
  window.__goalPlanner = {
    settings: readInputs,
    evaluate: function () { return evaluate(readInputs()); },
    projectValue: projectValue,
    fmtMoney: fmtMoney,
    BAND: BAND,
  };

  writeInputs(loadSettings());
  render();
})();

// --- Net worth chart: y-axis follows the visible x window -------------------
// Plotly keeps the y range pinned to the full series when you zoom the x axis,
// so a cropped window renders as a flat sliver against years of unused scale.
// Recompute y from the points actually in view after every x change, and again
// whenever traces are toggled. Same handler powers the goals switch.
(function () {
  var gd = document.getElementById('net-worth-chart');
  if (!gd || typeof Plotly === 'undefined') return;

  var Y_PAD = 0.08;      // headroom above/below the visible extremes
  var adjusting = false; // guards against our own relayout re-triggering us

  // Plotly hands back axis bounds as "YYYY-MM-DD HH:MM:SS.ffff", which older
  // parsers reject; normalise to ISO with at most millisecond precision.
  function toMs(v) {
    if (v instanceof Date) return v.getTime();
    if (typeof v === 'number') return v;
    var s = String(v).replace(' ', 'T');
    var dot = s.indexOf('.');
    if (dot >= 0 && s.length - dot > 4) s = s.slice(0, dot + 4);
    var t = Date.parse(s);
    return isNaN(t) ? Date.parse(String(v)) : t;
  }

  function visibleYRange() {
    var xaxis = gd._fullLayout && gd._fullLayout.xaxis;
    if (!xaxis || !xaxis.range) return null;
    var x0 = toMs(xaxis.range[0]);
    var x1 = toMs(xaxis.range[1]);
    if (isNaN(x0) || isNaN(x1)) return null;

    var lo = Infinity, hi = -Infinity;
    (gd.data || []).forEach(function (tr) {
      if (tr.visible === false || tr.visible === 'legendonly') return;
      if (!tr.x || !tr.y) return;
      for (var i = 0; i < tr.x.length; i++) {
        var xv = toMs(tr.x[i]);
        if (isNaN(xv) || xv < x0 || xv > x1) continue;
        var yv = Number(tr.y[i]);
        if (!isFinite(yv)) continue;
        if (yv < lo) lo = yv;
        if (yv > hi) hi = yv;
      }
    });
    if (!isFinite(lo) || !isFinite(hi)) return null;
    if (hi === lo) {
      var flat = Math.abs(hi) * 0.05 || 1;
      return [lo - flat, hi + flat];
    }
    var pad = (hi - lo) * Y_PAD;
    return [lo - pad, hi + pad];
  }

  function rescaleY() {
    if (adjusting) return;
    var range = visibleYRange();
    if (!range) return;
    adjusting = true;
    Plotly.relayout(gd, { 'yaxis.range': range }).then(function () {
      adjusting = false;
    }, function () {
      adjusting = false;
    });
  }

  // --- Goal projection fan, rebuilt from the Goal Planner's live inputs -----
  var FAN_COLOR = '#6366f1';
  var fanCount = 0;  // trailing traces on the chart that belong to the fan

  function buildFanTraces() {
    var gp = window.__goalPlanner;
    if (!gp) return null;
    var s = gp.settings();
    var ev = gp.evaluate();
    if (!(ev.years > 0)) return null;

    var months = Math.max(1, Math.round(ev.years * 12));
    var xs = [], central = [], low = [], high = [];
    var now = new Date();
    for (var k = 0; k <= months; k++) {
      var yrs = k / 12;
      xs.push(new Date(now.getTime() + yrs * 365.25 * 86400000));
      central.push(gp.projectValue(GOAL_CURRENT_VALUE, ev.plan, ev.r, yrs));
      low.push(gp.projectValue(GOAL_CURRENT_VALUE, ev.plan, Math.max(-0.99, ev.r - gp.BAND), yrs));
      high.push(gp.projectValue(GOAL_CURRENT_VALUE, ev.plan, ev.r + gp.BAND, yrs));
    }
    // Lump sums show as steps in the line; mark each one (drawn last, on top)
    // so it can be hovered.
    var lumpX = [], lumpY = [], lumpText = [];
    ev.plan.lumps.forEach(function (l) {
      if (l.k > months) return;
      lumpX.push(xs[l.k]);
      lumpY.push(central[l.k]);
      lumpText.push((l.amount > 0 ? '+' : '') + gp.fmtMoney(l.amount));
    });
    var lumpTraces = lumpX.length ? [
      { x: lumpX, y: lumpY, customdata: lumpText, mode: 'markers', name: 'Lump sum',
        legendgroup: 'proj',
        marker: { symbol: 'diamond', size: 9, color: FAN_COLOR,
                  line: { width: 1, color: '#ffffff' } },
        hovertemplate: '<b>Lump sum</b> %{x|%b %Y}<br>%{customdata} AUD<br>' +
                       'Projected after: $%{y:,.0f} AUD<extra></extra>' }
    ] : [];
    return [
      { x: xs, y: high, mode: 'lines', name: 'Projection band',
        line: { width: 0, color: FAN_COLOR }, showlegend: false, hoverinfo: 'skip' },
      { x: xs, y: low, mode: 'lines', name: 'Projection band',
        line: { width: 0, color: FAN_COLOR }, fill: 'tonexty',
        fillcolor: 'rgba(99,102,241,0.10)', legendgroup: 'proj', hoverinfo: 'skip' },
      { x: xs, y: central, mode: 'lines', name: 'Projection',
        line: { color: FAN_COLOR, width: 1.8, dash: 'dash' }, legendgroup: 'proj',
        hovertemplate: '<b>%{x|%b %Y}</b><br>Projected: $%{y:,.0f} AUD<extra></extra>' },
      { x: [ev.targetDate], y: [s.target], mode: 'markers+text', name: 'Goal target',
        marker: { symbol: 'star', size: 13, color: FAN_COLOR,
                  line: { width: 1, color: '#ffffff' } },
        text: ['Goal'], textposition: 'top center',
        textfont: { size: 10, color: FAN_COLOR },
        hovertemplate: '<b>Goal</b><br>Target: $%{y:,.0f} AUD by %{x|%b %Y}<extra></extra>' }
    ].concat(lumpTraces);
  }

  function removeFan() {
    if (!fanCount) return Promise.resolve();
    var idx = [];
    for (var i = gd.data.length - fanCount; i < gd.data.length; i++) idx.push(i);
    fanCount = 0;
    return Plotly.deleteTraces(gd, idx);
  }

  function updateFan() {
    var toggle = document.getElementById('nw-goals-toggle');
    adjusting = true;
    removeFan().then(function () {
      var traces = (toggle && toggle.checked) ? buildFanTraces() : null;
      if (traces) {
        fanCount = traces.length;
        return Plotly.addTraces(gd, traces);
      }
    }).then(function () {
      // Autoranging x stretches out to the goal date when the fan is on and
      // snaps back to history-only when it's off.
      return Plotly.relayout(gd, { 'xaxis.autorange': true });
    }).then(function () {
      adjusting = false;
      rescaleY();
    }, function () {
      adjusting = false;
    });
  }

  function attachGoalsToggle() {
    var toggle = document.getElementById('nw-goals-toggle');
    if (!toggle) return;
    toggle.addEventListener('change', updateFan);
    // Redraw the fan live while the planner inputs change.
    document.addEventListener('goal-updated', function () {
      if (toggle.checked) updateFan();
    });
  }

  function init() {
    if (typeof gd.on !== 'function' || !gd._fullLayout) {
      setTimeout(init, 50);
      return;
    }
    gd.on('plotly_relayout', function (ev) {
      // Skip the y-only relayout we issue ourselves; anything touching x wins.
      var keys = Object.keys(ev || {});
      if (keys.length && keys.every(function (k) { return k.indexOf('yaxis') === 0; })) return;
      rescaleY();
    });
    // Legend clicks and the goals switch both surface as restyle events.
    gd.on('plotly_restyle', function () { rescaleY(); });
    attachGoalsToggle();
    rescaleY();
  }

  init();
})();

// --- Charts: sized to their boxes, plus phone / touch adjustments -----------
// Plotly only re-measures on *window* resize, so a chart drawn before the rest
// of its grid row existed keeps the width it saw at that moment — the
// allocation donuts are drawn one panel at a time, and the first two came out
// far wider than their panels. A ResizeObserver refits each chart whenever its
// own box changes: at load, on rotation, and whenever the layout reflows.
(function () {
  if (typeof Plotly === 'undefined') {
    var note = document.getElementById('charts-offline');
    if (note) note.hidden = false;
    return;
  }
  var charts = Array.prototype.slice.call(document.querySelectorAll('.plotly-graph-div'));

  function fit(gd) {
    if (!gd._fullLayout) return;
    var p = Plotly.Plots.resize(gd);
    if (p && p.catch) p.catch(function () {});
  }
  if (window.ResizeObserver) {
    var ro = new ResizeObserver(function (entries) {
      entries.forEach(function (e) { fit(e.target); });
    });
    charts.forEach(function (gd) { ro.observe(gd); });
  } else {
    window.addEventListener('load', function () { charts.forEach(fit); });
  }

  // Phone layouts, keyed by chart id: shorter plots, the net-worth legend
  // moved under the plot (it collides with the 1M…All buttons at this width)
  // and the treemap's colour bar dropped to give the tiles room.
  var NARROW = {
    'net-worth-chart': function () {
      return { height: 360, 'margin.t': 40, 'legend.x': 0, 'legend.xanchor': 'left',
               'legend.y': -0.14, 'legend.yanchor': 'top', 'xaxis.rangeslider.visible': false };
    },
    'holdings-map': function () { return { height: 400, 'coloraxis.showscale': false }; },
    'corr-chart': function (gd, wide) {
      return { height: Math.min(wide.height, Math.round(gd.offsetWidth) + 60) };
    },
  };
  var wideLayouts = {};  // chart id -> the desktop values NARROW overrides

  function layoutValue(gd, path) {
    var v = path.split('.').reduce(function (o, k) { return o == null ? undefined : o[k]; }, gd.layout);
    return v === undefined ? null : v;  // null makes relayout restore Plotly's default
  }

  function relayout(gd, update) {
    if (update.height) gd.style.height = update.height + 'px';
    return Plotly.relayout(gd, update).catch(function () {});
  }

  var narrowMq = window.matchMedia('(max-width: 600px)');
  function applyWidth() {
    charts.forEach(function (gd) {
      var make = NARROW[gd.id];
      if (!make || !gd._fullLayout) return;
      if (!wideLayouts[gd.id]) {
        var wide = {};
        Object.keys(make(gd, { height: gd.layout.height })).forEach(function (k) {
          wide[k] = layoutValue(gd, k);
        });
        wideLayouts[gd.id] = wide;
      }
      relayout(gd, narrowMq.matches ? make(gd, wideLayouts[gd.id]) : wideLayouts[gd.id]);
    });
  }

  function init() {
    if (charts.some(function (gd) { return !gd._fullLayout; })) {
      setTimeout(init, 50);
      return;
    }
    // Touch: Plotly's drag-to-zoom claims every swipe that starts on a chart,
    // so the page can't be scrolled past it. With dragmode off a swipe scrolls
    // and a tap still shows the values.
    if (window.matchMedia('(hover: none) and (pointer: coarse)').matches) {
      charts.forEach(function (gd) { relayout(gd, { dragmode: false }); });
    }
    if (narrowMq.matches) applyWidth();
    if (narrowMq.addEventListener) narrowMq.addEventListener('change', applyWidth);
    else if (narrowMq.addListener) narrowMq.addListener(applyWidth);
  }
  init();
})();

// Holdings map "Organise by": each view is its own treemap trace; show one.
// The choice is remembered per browser.
(function () {
  var seg = document.getElementById('map-view');
  var gd = document.getElementById('holdings-map');
  if (!seg || !gd || typeof Plotly === 'undefined') return;
  var KEY = 'holdings-map-view';
  var buttons = Array.prototype.slice.call(seg.querySelectorAll('button'));

  function show(i) {
    if (!gd._fullLayout) { setTimeout(function () { show(i); }, 50); return; }
    var idx = buttons.map(function (_, j) { return j; });
    Plotly.restyle(gd, { visible: idx.map(function (j) { return j === i; }) }, idx)
      .catch(function () {});
    buttons.forEach(function (b, j) { b.setAttribute('aria-pressed', j === i ? 'true' : 'false'); });
  }
  seg.addEventListener('click', function (e) {
    var b = e.target.closest('button[data-view]');
    if (!b) return;
    show(buttons.indexOf(b));
    try { localStorage.setItem(KEY, b.getAttribute('data-view')); } catch (err) {}
  });
  // Buttons and traces share an order, so a button's position is its trace.
  var saved = null;
  try { saved = localStorage.getItem(KEY); } catch (err) {}
  var start = buttons.findIndex(function (b) { return b.getAttribute('data-view') === saved; });
  if (start > 0) show(start);
})();

{% if brief_json %}
const BRIEF_MD = {{ brief_json }};
const BRIEF_DATE = "{{ brief_date }}";

function exportBrief() {
  const blob = new Blob([BRIEF_MD], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'portfolio_brief_' + BRIEF_DATE + '.md';
  a.click();
  URL.revokeObjectURL(url);
}
{% endif %}
</script>
{% if targets_json %}
<!-- Its own block, so an error elsewhere in the page script can't take it down. -->
<script>
var TARGETS = {{ targets_json }};
{{ targets_js }}
</script>
{% endif %}

</body>
</html>
""")


def _net_worth_caption(snapshot_totals_df: pd.DataFrame, current_value: float) -> str:
    df = snapshot_totals_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    if df.empty:
        return ""

    parts = []
    one_year_ago = df["date"].max() - pd.Timedelta(days=365)
    prior = df[df["date"] <= one_year_ago]
    if not prior.empty:
        v = prior.iloc[-1]["total_value_aud"]
        chg = current_value - v
        pct = (chg / v * 100) if v else float("nan")
        sign = "+" if chg >= 0 else "−"
        parts.append(f"12-month change: {sign}${abs(chg):,.0f} ({sign}{abs(pct):.1f}%)")

    first = df.iloc[0]["total_value_aud"]
    chg = current_value - first
    sign = "+" if chg >= 0 else "−"
    parts.append(f"since {df.iloc[0]['date'].strftime('%b %Y')}: {sign}${abs(chg):,.0f}")
    return "   ·   ".join(parts)


def render_html(
    holdings_df: pd.DataFrame,
    aggregates: dict,
    brief_md: str | None,
    snapshot_totals_df: pd.DataFrame | None = None,
    fy_summary_df: pd.DataFrame | None = None,
    realised_df: pd.DataFrame | None = None,
    data_warning: str | None = None,
    performance: dict | None = None,
    analytics: dict | None = None,
    title: str = "Portfolio Dashboard",
    allocation: dict | None = None,
    public: bool = False,
    storage_key: str | None = None,
    web: bool = False,
) -> str:
    # Returns the dashboard's HTML. public=True renders the edition shared
    # with friends (see scripts/export_public.py): no rebalancing scenarios or
    # correlation heatmap. Sections whose inputs are None (CGT, income,
    # if-sold-today, the brief export) drop out on their own.
    # storage_key names this dashboard's saved browser settings (default: the
    # page's path). web=True puts the Targets section in web mode — see
    # src/render_targets.py.
    totals = aggregates["totals"]
    by_code = aggregates["by_code"]

    map_groupings = treemap_groupings(holdings_df, by_code, allocation)
    fig_treemap = treemap(by_code, map_groupings)
    map_views = [
        {"key": k, "label": label}
        for k, label in TREEMAP_VIEWS.items() if k in map_groupings
    ]

    chart_config = {"displayModeBar": False, "responsive": True}

    def _embed(fig: go.Figure, include_js: bool = False, **kwargs) -> str:
        return to_html(
            fig,
            full_html=False,
            include_plotlyjs="cdn" if include_js else False,
            config=kwargs.pop("config", chart_config),
            **kwargs,
        )

    treemap_html = to_html(
        fig_treemap, full_html=False, include_plotlyjs="cdn", config=chart_config,
        div_id="holdings-map",  # stable id for the page script's phone layout
    )

    # One panel per allocation dimension: donut + HTML legend carrying the
    # dollar values. Dimensions with nothing to show are dropped rather than
    # rendered as an empty panel.
    allocation_panels = []
    for dimension, panel_title in _ALLOC_TITLES.items():
        agg = _allocation_data(holdings_df, dimension)
        if agg.empty:
            continue
        allocation_panels.append({
            "title": panel_title,
            "chart_html": _embed(_allocation_donut(agg)),
            "legend_html": allocation_legend_html(agg),
        })

    table_html = holdings_table_html(by_code, holdings_df)

    net_worth_html = ""
    net_worth_caption = ""
    period_change = period_change_pct = period_change_class = None
    if snapshot_totals_df is not None and not snapshot_totals_df.empty:
        net_worth_html = _embed(
            net_worth_chart(snapshot_totals_df, performance),
            # Stable id so the y-refit / goals-toggle script can find the chart.
            div_id="net-worth-chart",
            # This chart is the one worth driving by hand, so it keeps a
            # trimmed mode bar rather than the dashboard-wide "no chrome".
            config={
                "responsive": True,
                "displayModeBar": True,
                "displaylogo": False,
                "modeBarButtonsToRemove": [
                    "select2d", "lasso2d", "toggleSpikelines",
                    "hoverClosestCartesian", "hoverCompareCartesian",
                ],
            },
        )
        net_worth_caption = _net_worth_caption(snapshot_totals_df, totals["total_value_aud"])
        # 12-month change card
        df_st = snapshot_totals_df.copy()
        df_st["date"] = pd.to_datetime(df_st["date"])
        df_st = df_st.sort_values("date")
        one_year_ago = df_st["date"].max() - pd.Timedelta(days=365)
        prior = df_st[df_st["date"] <= one_year_ago]
        base = prior.iloc[-1]["total_value_aud"] if not prior.empty else df_st.iloc[0]["total_value_aud"]
        chg = totals["total_value_aud"] - base
        pct = (chg / base * 100) if base else float("nan")
        sgn = "+" if chg >= 0 else "−"
        period_change = f"{sgn}${abs(chg):,.0f}"
        period_change_pct = f"{sgn}{abs(pct):.1f}%"
        period_change_class = "pos" if chg >= 0 else "neg"

    gains_table_html = ""
    cgt_disposals_html = ""
    cgt_tax_summary_html = ""
    realised_fy = realised_fy_class = None
    if fy_summary_df is not None and not fy_summary_df.empty:
        gains_table_html = cgt_fy_table_html(
            fy_summary_df, fy_working_steps(fy_summary_df), _current_fy_label()
        )
        cgt_disposals_html = disposals_by_stock_html(realised_df)
        cgt_tax_summary_html = cgt_tax_return_summary_html(
            fy_summary_df, _current_fy_label()
        )
        cur_fy = _current_fy_label()
        cur_row = fy_summary_df[fy_summary_df["fy"] == cur_fy]
        if not cur_row.empty:
            v = cur_row.iloc[0]["est_taxable_gain"]
            sgn = "+" if v >= 0 else "−"
            realised_fy = f"{sgn}${abs(v):,.0f}"
            realised_fy_class = "pos" if v >= 0 else "neg"

    # Crypto exposure card
    crypto_pct = None
    if "asset_class" in by_code.columns:
        crypto_val = by_code.loc[by_code["asset_class"] == "Crypto", "market_value_aud"].sum()
        tv = totals["total_value_aud"]
        if tv > 0:
            crypto_pct = f"{crypto_val / tv * 100:.1f}%"

    # Performance table + XIRR / vs-benchmark cards
    performance_table_html = ""
    xirr_details_html = ""
    xirr_all = xirr_all_class = None
    vs_bench = vs_bench_label = vs_bench_class = None
    if performance is not None and performance.get("windows"):
        performance_table_html = performance_table_frag_html(performance)
        xirr_details_html = per_holding_xirr_table_html(
            performance.get("per_holding_xirr") or {}, by_code
        )

    if performance is not None:
        px = performance.get("portfolio_xirr")
        if px is not None:
            sgn = "+" if px >= 0 else "−"
            xirr_all = f"{sgn}{abs(px) * 100:.1f}%"
            xirr_all_class = "pos" if px >= 0 else "neg"

        # "vs benchmark (12M)": excess TWR vs the first benchmark over the 12M window.
        for w in performance.get("windows", []):
            if w["window"] == "12M" and w["benchmarks"]:
                primary = w["benchmarks"][0]
                if primary["excess"] is not None:
                    ex = primary["excess"]
                    sgn = "+" if ex >= 0 else "−"
                    vs_bench = f"{sgn}{abs(ex) * 100:.1f}%"
                    vs_bench_label = primary["label"]
                    vs_bench_class = "pos" if ex >= 0 else "neg"
                break

    # Phase 5: income, risk, correlation, "if sold today".
    income_table_html = ""
    income_yield_caption = ""
    risk_cards = ""
    correlation_html = ""
    if_sold_today_html = ""
    if analytics is not None:
        if_sold_today_html = if_sold_today_table_html(analytics.get("disposals") or {})
        income_fy = analytics.get("income_fy_df")
        if income_fy is not None and not income_fy.empty:
            income_table_html = income_fy_table_html(income_fy)
            yld = analytics.get("income_yield") or {}
            yov = yld.get("yield_on_value")
            if yov is not None:
                income_yield_caption = (
                    f" Trailing-12m income ${yld.get('ttm_income_aud', 0):,.0f} "
                    f"({yov * 100:.2f}% on value)."
                )

        risk = analytics.get("risk") or {}
        risk_cards = risk_cards_html(risk)
        corr = risk.get("correlation")
        if not public and corr is not None and not corr.empty and corr.shape[0] >= 2:
            correlation_html = _embed(correlation_heatmap(corr), div_id="corr-chart")

    pl = totals["total_pl_aud"]
    pl_pct = totals["total_pl_pct"]
    pl_sign = "+" if pl >= 0 else "−"
    pct_sign = "+" if pl_pct >= 0 else "−"
    pl_class = "pos" if pl >= 0 else "neg"

    # Sticky nav: only link to sections that will actually render.
    nav_links = [{"anchor": a, "label": l} for a, l, present in (
        ("sec-goal", "Goal", True),
        ("sec-map", "Map", True),
        ("sec-holdings", "Holdings", True),
        ("sec-alloc", "Allocation", True),
        ("sec-targets", "Targets", allocation is not None),
        ("sec-history", "History", bool(net_worth_html)),
        ("sec-perf", "Performance", bool(performance_table_html)),
        ("sec-cgt", "CGT", bool(gains_table_html)),
        ("sec-sold", "If Sold Today", bool(if_sold_today_html)),
        ("sec-income", "Income", bool(income_table_html)),
        ("sec-risk", "Risk", bool(risk_cards)),
    ) if present]

    html = _TEMPLATE.render(
        title=title,
        storage_key_js=(
            json.dumps(storage_key).replace("</", "<\\/")
            if storage_key is not None else "location.pathname"
        ),
        updated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        data_warning=data_warning,
        nav_links=nav_links,
        current_value_js=json.dumps(round(float(totals["total_value_aud"]), 2)),
        total_value=f"${totals['total_value_aud']:,.0f}",
        total_pl=f"{pl_sign}${abs(pl):,.0f}",
        total_pl_pct=f"{pct_sign}{abs(pl_pct):.2f}%",
        num_assets=len(by_code),
        num_positions=totals["num_positions"],
        crypto_pct=crypto_pct,
        cur_fy=_current_fy_label(),
        realised_fy=realised_fy,
        realised_fy_class=realised_fy_class,
        period_change=period_change,
        period_change_pct=period_change_pct,
        period_change_class=period_change_class,
        pl_class=pl_class,
        treemap_html=treemap_html,
        map_views=map_views,
        holdings_table_html=table_html,
        allocation_panels=allocation_panels,
        net_worth_html=net_worth_html,
        net_worth_caption=net_worth_caption,
        gains_table_html=gains_table_html,
        cgt_disposals_html=cgt_disposals_html,
        cgt_tax_summary_html=cgt_tax_summary_html,
        if_sold_today_html=if_sold_today_html,
        xirr_details_html=xirr_details_html,
        income_table_html=income_table_html,
        income_yield_caption=income_yield_caption,
        risk_cards_html=risk_cards,
        correlation_html=correlation_html,
        performance_table_html=performance_table_html,
        xirr_all=xirr_all,
        xirr_all_class=xirr_all_class,
        vs_bench=vs_bench,
        vs_bench_label=vs_bench_label,
        vs_bench_class=vs_bench_class,
        targets_css=TARGETS_CSS,
        targets_html=targets_html(rebalancing=not public, web=web),
        targets_js=TARGETS_JS,
        targets_json=(
            targets_payload(allocation, by_code, CATEGORICAL, web=web)
            if allocation is not None else ""
        ),
        brief_json=json.dumps(brief_md) if brief_md is not None else "",
        brief_date=datetime.now().strftime("%Y-%m-%d"),
    )
    return html
