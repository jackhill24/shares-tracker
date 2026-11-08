"""Dashboard "Targets" section: allocation vs target and rebalancing scenarios.

Everything interactive runs client-side against one embedded JSON payload
(``targets_payload``): the saved allocation config, current holding values
and per-parcel CGT inputs. Edits are kept as a browser draft until the user
downloads them back to allocation.yaml (see src/allocation.py).

Web mode (``web=True``, the browser edition) has no file to save to: the web
page that embeds the dashboard keeps the config and passes it back in on each
rebuild, so it is the source of truth and there's no draft. The dashboard
talks to that page with ``postMessage``:

  {type: "sharesTracker.targets", config}      on every edit — store it
  {type: "sharesTracker.importTargets", yaml}  Import YAML picked a file —
                                               parse it (parse_allocation),
                                               store it and rebuild

"Save to file" becomes "Export YAML", which downloads the same YAML.

The markup, styles and script are injected into render.py's template as
plain variables, so Jinja never parses the JavaScript.
"""
import json
import math

import pandas as pd

TARGETS_CSS = """
    /* Targets (allocation vs target + rebalancing scenarios) */
    .tg-banner {
      display: flex; flex-wrap: wrap; align-items: center; gap: 6px 14px;
      border-radius: 10px; padding: 10px 14px; margin-bottom: 12px; font-size: 0.84rem;
      background: #f0fdfa; border: 1px solid #99f6e4; color: #134e4a;
    }
    .tg-banner.warn { background: #fff7ed; border-color: #fdba74; color: #7c2d12; }
    .tg-banner .tg-banner-text { flex: 1 1 320px; }
    .tg-banner a { color: inherit; font-weight: 600; }
    .tg-btn {
      background: #14b8a6; color: #ffffff; border: none; border-radius: 8px;
      padding: 7px 14px; font-size: 0.8rem; font-weight: 600; cursor: pointer; font-family: inherit;
    }
    .tg-btn:hover { background: #0d9488; }
    .tg-btn.ghost { background: none; border: 1px solid #cbd5e1; color: #475569; }
    .tg-btn.ghost:hover { border-color: #94a3b8; color: #1a2b3c; background: #ffffff; }

    .tg-top { display: grid; grid-template-columns: minmax(0, 1fr) minmax(280px, 380px); gap: 20px; margin-bottom: 20px; }
    .tg-headline { font-size: 0.95rem; font-weight: 600; color: #1a2b3c; margin-bottom: 14px; line-height: 1.45; }
    .tg-headline .tg-sub { display: block; font-size: 0.8rem; font-weight: 400; color: #64748b; margin-top: 2px; }
    .tg-row { padding: 10px 0; border-bottom: 1px solid #f0f3f6; }
    .tg-row:last-child { border-bottom: none; }
    .tg-row-head { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; margin-bottom: 7px; font-size: 0.86rem; }
    .tg-dot { width: 10px; height: 10px; border-radius: 3px; flex: none; }
    .tg-name { font-weight: 650; color: #1a2b3c; }
    .tg-nums { margin-left: auto; color: #475569; font-variant-numeric: tabular-nums; font-size: 0.82rem; }
    .tg-nums b { color: #1a2b3c; }
    .tg-chip { border-radius: 999px; padding: 1px 9px; font-size: 0.7rem; font-weight: 700; white-space: nowrap; }
    .tg-chip.on { background: #e6fffb; color: #0f766e; }
    .tg-chip.over { background: #fff7ed; color: #b45309; }
    .tg-chip.under { background: #eff6ff; color: #1d4ed8; }
    .tg-track { position: relative; height: 12px; border-radius: 999px; background: #f1f5f9; }
    .tg-bandzone { position: absolute; top: 0; bottom: 0; background: repeating-linear-gradient(135deg, #cbd5e1 0 2px, transparent 2px 6px); opacity: 0.8; border-radius: 3px; }
    .tg-fill { position: absolute; top: 2px; bottom: 2px; left: 0; border-radius: 999px; transition: width 0.25s; }
    .tg-tick { position: absolute; top: -4px; bottom: -4px; width: 2px; margin-left: -1px; background: #1a2b3c; border-radius: 1px; }
    .tg-codes { font-size: 0.74rem; color: #94a3b8; margin-top: 6px; }
    .tg-key { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 0.72rem; color: #94a3b8; margin-top: 10px; }
    .tg-key span { display: inline-flex; align-items: center; gap: 6px; }
    .tg-key i { display: inline-block; }
    .tg-key .k-fill { width: 14px; height: 8px; border-radius: 999px; background: #94a3b8; }
    .tg-key .k-tick { width: 2px; height: 12px; background: #1a2b3c; }
    .tg-key .k-band { width: 14px; height: 10px; background: repeating-linear-gradient(135deg, #cbd5e1 0 2px, transparent 2px 6px); }

    .tg-settings { display: flex; flex-direction: column; gap: 14px; }
    .tg-brow, .tg-brow-head { display: grid; grid-template-columns: 10px minmax(0, 1fr) 76px 24px; gap: 8px; align-items: center; }
    .tg-brow-head span { font-size: 0.64rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.07em; color: #94a3b8; }
    .tg-brows { display: flex; flex-direction: column; gap: 6px; }
    .tg-settings input, .tg-settings select, .tg-scen input, .tg-scen select {
      width: 100%; border: 1px solid #e7ebf0; border-radius: 9px; padding: 6px 9px;
      font-size: 0.84rem; color: #1a2b3c; background: #fbfcfd; font-family: inherit;
      font-variant-numeric: tabular-nums;
    }
    .tg-settings input:focus, .tg-settings select:focus, .tg-scen input:focus, .tg-scen select:focus {
      outline: 2px solid #14b8a6; outline-offset: -1px; background: #ffffff;
    }
    .tg-sum { font-size: 0.8rem; font-weight: 600; color: #0f766e; }
    .tg-sum.bad { color: #b45309; }
    .tg-tol { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .tg-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; border-top: 1px solid #f0f3f6; padding-top: 12px; }
    .tg-actions .gp-hint { margin: 0; flex-basis: 100%; }
    details.tg-assign { border-top: 1px solid #f0f3f6; padding-top: 10px; }
    details.tg-assign summary { cursor: pointer; color: #0d9488; font-weight: 600; font-size: 0.82rem; user-select: none; }
    .tg-arow { display: grid; grid-template-columns: minmax(0, 0.9fr) auto minmax(0, 1.4fr); gap: 8px; align-items: center; padding: 4px 0; font-size: 0.82rem; }
    .tg-arow .tg-acode { font-weight: 650; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .tg-arow .tg-aval { color: #64748b; font-variant-numeric: tabular-nums; text-align: right; }
    .tg-arow.unassigned .tg-acode { color: #b45309; }
    .tg-arow.unassigned select { border-color: #fdba74; background: #fffbeb; }
    .tg-arows { margin-top: 8px; max-height: 360px; overflow-y: auto; }

    .tg-scen { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(170px, 100%), 1fr)); gap: 14px; margin-bottom: 16px; }
    .tg-scen label { display: block; font-size: 0.7rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.07em; color: #94a3b8; margin-bottom: 4px; }
    .tg-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr)); gap: 16px; margin-bottom: 16px; }
    .tg-card { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
    .tg-card h4 { font-size: 0.95rem; font-weight: 700; color: #1a2b3c; }
    .tg-card .tg-desc { font-size: 0.78rem; color: #64748b; line-height: 1.4; margin-top: 2px; }
    .tg-tag { display: inline-block; margin-left: 6px; vertical-align: 2px; border-radius: 6px; padding: 1px 7px; font-size: 0.64rem; font-weight: 700; background: #e6fffb; color: #0d9488; }
    ul.tg-do { list-style: none; display: flex; flex-direction: column; gap: 6px; font-size: 0.84rem; }
    ul.tg-do li { line-height: 1.4; }
    .tg-verb { display: inline-block; min-width: 34px; font-size: 0.66rem; font-weight: 800; text-transform: uppercase; letter-spacing: 0.05em; }
    .tg-verb.buy { color: #0f766e; }
    .tg-verb.sell { color: #b45309; }
    .tg-muted { color: #94a3b8; font-size: 0.76rem; }
    .tg-metrics { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 14px; background: #f8fafc; border-radius: 10px; padding: 10px 12px; }
    .tg-metric .card-label { margin-bottom: 1px; font-size: 0.62rem; }
    .tg-metric b { font-size: 0.98rem; font-variant-numeric: tabular-nums; }
    table.tg-after { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
    table.tg-after th { text-align: right; color: #94a3b8; font-weight: 600; font-size: 0.64rem; text-transform: uppercase; letter-spacing: 0.04em; padding: 4px 6px; border-bottom: 1px solid #e7ebf0; }
    table.tg-after td { text-align: right; padding: 5px 6px; border-bottom: 1px solid #f6f8fa; font-variant-numeric: tabular-nums; }
    table.tg-after th:first-child, table.tg-after td:first-child { text-align: left; }
    table.tg-after td.tg-off { color: #b45309; font-weight: 700; }
    .tg-foot { font-size: 0.78rem; color: #475569; border-top: 1px solid #f0f3f6; padding-top: 10px; margin-top: auto; line-height: 1.5; }
    .tg-foot b { color: #1a2b3c; }
    .tg-empty { font-size: 0.86rem; color: #64748b; padding: 18px; text-align: center; background: #f8fafc; border-radius: 10px; }
    #tg-path-chart { width: 100%; height: 300px; }

    @media (max-width: 760px) {
      .tg-top { grid-template-columns: 1fr; }
    }
    @media (max-width: 600px) {
      .tg-nums { margin-left: 0; flex-basis: 100%; }
      /* iOS zooms the page into any focused input under 16px */
      .tg-settings input, .tg-settings select, .tg-scen input, .tg-scen select { font-size: 16px; }
      #tg-path-chart { height: 260px; }
    }
"""

# The section is assembled by ``targets_html`` from these pieces, so the
# "Ways to get to target" scenarios can be left out (the public edition does).
_HTML_INTRO = """
<div class="section" id="sec-targets">
  <p class="section-title">Targets</p>
  <p class="caption">
    How your portfolio compares with your target mix of strategy buckets, and
    three ways to get there. Edits are kept in this browser straight away;
    <b>Save to file</b> makes them permanent. Prices held flat —
    estimates, not financial or tax advice.
  </p>
"""

_HTML_INTRO_WEB = """
<div class="section" id="sec-targets">
  <p class="section-title">Targets</p>
  <p class="caption">
    How your portfolio compares with your target mix of strategy buckets.
    Edits are saved in this browser straight away; <b>Export YAML</b> keeps a
    copy. Estimates, not financial advice.
  </p>
"""

_HTML_INTRO_NO_REBALANCE = """
<div class="section" id="sec-targets">
  <p class="section-title">Targets</p>
  <p class="caption">
    How your portfolio compares with your target mix of strategy buckets.
    Edits are kept in this browser straight away; <b>Save to file</b> makes
    them permanent. Estimates, not financial advice.
  </p>
"""

_HTML_BUCKETS = """
  <div id="tg-banners"></div>

  <div class="tg-top">
    <div class="panel">
      <div class="tg-headline" id="tg-headline"></div>
      <div id="tg-bars"></div>
      <div class="tg-key">
        <span><i class="k-fill"></i>Actual share</span>
        <span><i class="k-tick"></i>Target</span>
        <span><i class="k-band"></i>Tolerance band</span>
      </div>
    </div>

    <div class="panel tg-settings">
      <div>
        <div class="gp-list-head">
          <span class="gp-list-title">Buckets &amp; targets</span>
          <button class="gp-add" id="tg-add-bucket" type="button">+ Add bucket</button>
        </div>
        <div class="tg-brows" id="tg-brows">
          <div class="tg-brow-head"><span></span><span>Name</span><span>Target %</span><span></span></div>
        </div>
        <div class="tg-sum" id="tg-sum" style="margin-top:8px"></div>
      </div>
      <div>
        <span class="gp-list-title">Tolerance band</span>
        <p class="gp-hint">A bucket counts as on target within ± the smaller of these.</p>
        <div class="tg-tol">
          <div class="gp-field">
            <label for="tg-tol-abs">± points</label>
            <input type="number" id="tg-tol-abs" min="0" max="50" step="0.5">
          </div>
          <div class="gp-field">
            <label for="tg-tol-rel">± % of target</label>
            <input type="number" id="tg-tol-rel" min="0" max="100" step="5">
          </div>
        </div>
      </div>
      <details class="tg-assign" id="tg-assign">
        <summary id="tg-assign-summary">Assign holdings</summary>
        <div class="tg-arows" id="tg-arows"></div>
      </details>
"""

_HTML_ACTIONS = """      <div class="tg-actions">
        <button class="tg-btn" id="tg-save" type="button">Save to file</button>
        <button class="tg-btn ghost" id="tg-discard" type="button" hidden>Discard changes</button>
        <p class="gp-hint" id="tg-save-hint"></p>
      </div>
"""

_HTML_ACTIONS_WEB = """      <div class="tg-actions">
        <button class="tg-btn" id="tg-save" type="button">Export YAML</button>
        <button class="tg-btn ghost" id="tg-import" type="button">Import YAML</button>
        <input type="file" id="tg-import-file" accept=".yaml,.yml" hidden>
        <p class="gp-hint" id="tg-save-hint"></p>
      </div>
"""

_HTML_BUCKETS_END = """    </div>
  </div>
"""

_HTML_REBALANCE = """
  <p class="subsection-title">Ways to get to target</p>
  <p class="caption">
    Play with the inputs — all three strategies update live.
    New money always goes to whichever bucket is furthest below its target first.
  </p>
  <div class="panel tg-scen">
    <div>
      <label for="tg-lump">Lump sum to invest now (AUD)</label>
      <input type="number" id="tg-lump" min="0" step="500">
    </div>
    <div>
      <label for="tg-monthly">Monthly contribution (AUD)</label>
      <input type="number" id="tg-monthly" min="0" step="100">
    </div>
    <div>
      <label for="tg-tax">Marginal tax rate (%)</label>
      <input type="number" id="tg-tax" min="0" max="60" step="0.5">
    </div>
    <div>
      <label for="tg-trim">When selling, trim</label>
      <select id="tg-trim">
        <option value="tax">Tax-smart: lowest tax first</option>
        <option value="prorata">Pro-rata: every holding, FIFO</option>
      </select>
    </div>
  </div>
  <div class="tg-cards" id="tg-cards"></div>

  <div class="panel" id="tg-path-panel">
    <div class="panel-title">Path to target — contributions only</div>
    <p class="caption" id="tg-path-caption"></p>
    <div id="tg-path-chart"></div>
  </div>
"""

_HTML_METHOD_OPEN = """
  <details class="method" style="margin-top:12px">
    <summary>How this works</summary>
    <div class="method-body">
      <p><b>Buckets.</b> Each holding (by ticker, across all platforms) is assigned to
        one bucket by you — nothing is guessed. Unassigned holdings are left out of
        the percentages until you assign them.</p>
      <p><b>Tolerance band.</b> A bucket is "on target" within ± the smaller of the
        absolute points and the percentage of its target (the 5/25 rule), so a 10%
        bucket gets ±2.5 points rather than ±5.</p>
"""

_HTML_METHOD_REBALANCE = """
      <p><b>New money</b> is routed to whichever bucket is furthest below its target,
        relative to its size, then to the next once they're level ("water-filling").
        With enough of it, every bucket lands exactly on target without selling.</p>
      <p><b>Contributions only</b> never sells, so there's no CGT; it just takes time.
        <b>Full rebalance</b> sells overweight buckets down to target today.
        <b>Hybrid</b> invests the lump sum first, then sells only enough to bring each
        bucket back to the edge of its band — usually the smallest sale.</p>
      <p><b>Tax estimate.</b> Each sale is matched to your open parcels. Tax-smart
        sells loss-making parcels first, then those eligible for the 50% CGT discount,
        then the rest; pro-rata trims every holding in the bucket proportionally using
        FIFO parcels (what the CGT section books). Losses are netted against
        non-discount gains first, then discount gains, before the 50% discount.
        The estimate ignores this financial year's other realised gains and any
        carried-forward losses. Choosing specific parcels needs your broker to support
        (and record) parcel identification.</p>
"""

_HTML_METHOD_CLOSE = """
    </div>
  </details>
</div>
"""


def targets_html(rebalancing: bool = True, web: bool = False) -> str:
    """The Targets section markup; ``rebalancing=False`` drops the scenarios,
    ``web=True`` swaps Save to file for Export / Import YAML."""
    buckets = (_HTML_BUCKETS + (_HTML_ACTIONS_WEB if web else _HTML_ACTIONS)
               + _HTML_BUCKETS_END)
    if web:
        intro = _HTML_INTRO_WEB
    else:
        intro = _HTML_INTRO if rebalancing else _HTML_INTRO_NO_REBALANCE
    if not rebalancing:
        return intro + buckets + _HTML_METHOD_OPEN + _HTML_METHOD_CLOSE
    return (intro + buckets + _HTML_REBALANCE
            + _HTML_METHOD_OPEN + _HTML_METHOD_REBALANCE + _HTML_METHOD_CLOSE)


# The page script. Plain string (not Jinja) — see the module docstring.
TARGETS_JS = r"""
// --- Targets: allocation vs target + rebalancing scenarios -------------------
// All maths runs client-side against TARGETS (embedded by render_targets.py).
// Bucket edits are a browser *draft* tagged with the fingerprint of the file
// they were based on; if allocation.yaml has changed since, the file wins.
// In web mode the embedding page keeps the config instead (module docstring).
(function () {
  var root = document.getElementById('sec-targets');
  if (!root || typeof TARGETS === 'undefined') return;
  var A = TARGETS;
  var WEB = A.mode === 'web';
  var KEY_DRAFT = 'sharesTracker.alloc.draft.' + STORAGE_KEY;
  var KEY_SCEN = 'sharesTracker.alloc.scenario.' + STORAGE_KEY;
  var KEY_GOAL = 'sharesTracker.goal.' + STORAGE_KEY;
  var EPS = 0.5;  // dollars: anything smaller isn't worth a line
  // targets_html(rebalancing=False) leaves the scenarios' markup off the page.
  var REBALANCE = !!document.getElementById('tg-cards');

  function $(id) { return document.getElementById(id); }
  function load(key) {
    try { return JSON.parse(localStorage.getItem(key)); } catch (e) { return null; }
  }
  function store(key, v) {
    try {
      if (v === null) localStorage.removeItem(key);
      else localStorage.setItem(key, JSON.stringify(v));
    } catch (e) {}
  }
  function num(v, d) { var n = Number(v); return v !== '' && v !== null && isFinite(n) ? n : d; }
  function sum(a) { return a.reduce(function (s, x) { return s + x; }, 0); }
  function money(v) {
    return (v < -0.5 ? '−$' : '$') + Math.round(Math.abs(v)).toLocaleString('en-AU');
  }
  function pct(frac) { return (frac * 100).toFixed(1) + '%'; }
  function pts(v) { return (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(1) + 'pp'; }
  function units(u) {
    var dp = u >= 100 ? 0 : u >= 1 ? 2 : 4;
    return u.toLocaleString('en-AU', { maximumFractionDigits: dp });
  }
  function duration(n) {
    return n >= 24 ? +(n / 12).toFixed(1) + ' years' : n + ' month' + (n === 1 ? '' : 's');
  }
  var ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return ESC[c]; }); }
  function monthLabel(offset) {
    var d = new Date();
    d = new Date(d.getFullYear(), d.getMonth() + offset, 1);
    return d.toLocaleDateString('en-AU', { month: 'short', year: 'numeric' });
  }

  // --- Config state ---------------------------------------------------------
  function normalise(c) {
    c = c || {};
    var seen = {};
    var buckets = (Array.isArray(c.buckets) ? c.buckets : []).filter(function (b) {
      if (!b || !b.id || seen[b.id]) return false;
      seen[b.id] = true;
      return true;
    }).map(function (b) {
      return { id: String(b.id), label: String(b.label || b.id), target: Math.max(0, num(b.target, 0)) };
    });
    var tol = c.tolerance || {};
    var holdings = {};
    Object.keys(c.holdings || {}).sort().forEach(function (code) {
      if (seen[c.holdings[code]]) holdings[code] = c.holdings[code];
    });
    return {
      buckets: buckets,
      tolerance: {
        absolute_pp: Math.max(0, num(tol.absolute_pp, 5)),
        relative_pct: Math.max(0, num(tol.relative_pct, 25)),
      },
      holdings: holdings,
    };
  }
  function same(a, b) { return JSON.stringify(normalise(a)) === JSON.stringify(normalise(b)); }
  // The embedding page; a srcdoc dashboard's parent is always the page that
  // built it, so there's no other window that could receive the message.
  function tellPage(msg) {
    if (window.parent === window) return false;
    window.parent.postMessage(msg, '*');
    return true;
  }

  var FILE = normalise(A.config);
  var state, notice = null, downloaded = false;
  (function () {
    if (WEB) { state = normalise(FILE); return; }
    var d = load(KEY_DRAFT);
    if (d && d.config && d.base === A.fingerprint) {
      state = normalise(d.config);
      return;
    }
    // A draft based on an older file: drop it. Silent when it matches the
    // file anyway (the usual case right after saving and re-running).
    if (d && d.config && !same(d.config, FILE)) {
      notice = A.fileName + ' has changed since you last edited targets in this ' +
        'browser, so those unsaved edits were replaced by the file.';
    }
    store(KEY_DRAFT, null);
    state = normalise(FILE);
  })();
  function dirty() { return !WEB && !same(state, FILE); }
  function commit() {
    if (WEB) tellPage({ type: 'sharesTracker.targets', config: state });
    else store(KEY_DRAFT, dirty() ? { base: A.fingerprint, config: state } : null);
    downloaded = false;
    render();
  }

  // --- Scenario inputs ------------------------------------------------------
  function scenDefaults() {
    var g = load(KEY_GOAL);
    return {
      lump: 0,
      monthly: g && isFinite(g.monthly) ? Number(g.monthly) : 1000,
      taxRate: 32,
      trim: 'tax',
    };
  }
  var scen = Object.assign(scenDefaults(), load(KEY_SCEN) || {});

  // --- Model ------------------------------------------------------------------
  function model() {
    var byId = {}, tol = state.tolerance;
    var buckets = state.buckets.map(function (b, i) {
      var band = Math.min(tol.absolute_pp, tol.relative_pct / 100 * b.target);
      var m = {
        id: b.id, label: b.label, target: b.target, t: b.target / 100,
        color: A.palette[i % A.palette.length], band: band,
        lower: Math.max(0, b.target - band) / 100, upper: (b.target + band) / 100,
        value: 0, codes: [],
      };
      byId[b.id] = m;
      return m;
    });
    var unassigned = [], total = 0, grand = 0;
    A.holdings.forEach(function (h) {
      grand += h.value;
      var m = byId[state.holdings[h.code]];
      if (m) { m.value += h.value; m.codes.push(h.code); total += h.value; }
      else unassigned.push(h);
    });
    buckets.forEach(function (m) { m.actual = total > 0 ? m.value / total : 0; });
    var tsum = sum(buckets.map(function (m) { return m.target; }));
    return {
      buckets: buckets, total: total, grand: grand, unassigned: unassigned,
      targetSum: tsum, sumOk: Math.abs(tsum - 100) < 0.01,
    };
  }
  function status(w, m) {
    if (w > m.upper + 1e-9) return 'over';
    if (w < m.lower - 1e-9) return 'under';
    return 'on';
  }
  function values(M) { return M.buckets.map(function (m) { return m.value; }); }
  function fracs(M) { return M.buckets.map(function (m) { return m.t; }); }
  function add(a, b) { return a.map(function (x, i) { return x + b[i]; }); }
  function zeros(M) { return M.buckets.map(function () { return 0; }); }
  function outOfBand(M, vals) {
    var T = sum(vals);
    return M.buckets.filter(function (m, i) {
      return T > 0 && status(vals[i] / T, m) !== 'on';
    });
  }

  // Split `cash` so the most underweight buckets (value ÷ target lowest) fill
  // first: find the level L where Σ max(0, L·t − V) = cash. Every bucket that
  // gets money ends at L·t — on target for a portfolio worth L. Mirrors
  // waterfill() in src/allocation.py.
  function waterfill(vals, ts, cash) {
    var buys = vals.map(function () { return 0; });
    if (!(cash > 0)) return buys;
    var idx = [];
    ts.forEach(function (t, i) { if (t > 0) idx.push(i); });
    if (!idx.length) return buys;
    idx.sort(function (a, b) { return vals[a] / ts[a] - vals[b] / ts[b]; });
    var sv = 0, st = 0, level = 0, k;
    for (k = 0; k < idx.length; k++) {
      sv += vals[idx[k]];
      st += ts[idx[k]];
      level = (cash + sv) / st;
      var next = k + 1 < idx.length ? vals[idx[k + 1]] / ts[idx[k + 1]] : Infinity;
      if (level <= next) break;
    }
    for (var j = 0; j <= k && j < idx.length; j++) {
      buys[idx[j]] = Math.max(0, level * ts[idx[j]] - vals[idx[j]]);
    }
    return buys;
  }

  // New money to reach every target with no selling (null: impossible, a
  // held bucket has a 0% target), and the smaller amount to get in band.
  function needToTarget(M) {
    var need = M.total;
    for (var i = 0; i < M.buckets.length; i++) {
      var m = M.buckets[i];
      if (m.value > EPS) {
        if (m.t <= 0) return null;
        need = Math.max(need, m.value / m.t);
      }
    }
    return Math.max(0, need - M.total);
  }
  function needToBand(M) {
    var hi = needToTarget(M);
    if (hi === null) return null;
    var V = values(M), ts = fracs(M);
    function ok(c) { return outOfBand(M, add(V, waterfill(V, ts, c))).length === 0; }
    if (ok(0)) return 0;
    var lo = 0;
    for (var n = 0; n < 60; n++) {
      var mid = (lo + hi) / 2;
      if (ok(mid)) hi = mid; else lo = mid;
    }
    return hi;
  }

  // --- Selling: which parcels, and the CGT they'd realise --------------------
  function holdingValue(code) {
    for (var i = 0; i < A.holdings.length; i++) {
      if (A.holdings[i].code === code) return A.holdings[i].value;
    }
    return 0;
  }
  // Tax per dollar of proceeds, lowest first: losses, then discounted gains.
  function taxPerDollar(p) {
    var g = p.gain_per_unit;
    return (g > 0 && p.discount_eligible ? g / 2 : g) / p.value_per_unit;
  }
  function sellFrom(m, amount, mode, lines) {
    function line(code) {
      if (!lines[code]) {
        lines[code] = { code: code, bucket: m.label, amount: 0, units: 0,
                        disc: 0, nonDisc: 0, loss: 0, unpriced: 0 };
      }
      return lines[code];
    }
    function take(code, p, dollars) {
      var u = dollars / p.value_per_unit, g = u * p.gain_per_unit, L = line(code);
      L.amount += dollars;
      L.units += u;
      if (g < 0) L.loss -= g;
      else if (p.discount_eligible) L.disc += g;
      else L.nonDisc += g;
    }
    function drain(list, left) {
      list.forEach(function (x) {
        if (left <= 1e-6 || !(x.p.value_per_unit > 0)) return;
        var d = Math.min(left, x.p.units * x.p.value_per_unit);
        take(x.code, x.p, d);
        left -= d;
      });
      return left;
    }
    if (mode === 'tax') {
      var pool = [];
      m.codes.forEach(function (code) {
        (A.parcels[code] || []).forEach(function (p) { pool.push({ code: code, p: p }); });
      });
      pool.sort(function (a, b) { return taxPerDollar(a.p) - taxPerDollar(b.p); });
      var left = drain(pool, amount);
      if (left > 1e-6 && m.codes.length) {  // no parcel data for the rest
        var L = line(m.codes[0]);
        L.amount += left;
        L.unpriced += left;
      }
    } else {
      m.codes.forEach(function (code) {
        var share = m.value > 0 ? amount * holdingValue(code) / m.value : 0;
        var fifo = (A.parcels[code] || []).map(function (p) { return { code: code, p: p }; });
        var rest = drain(fifo, share);
        if (rest > 1e-6) {
          var L = line(code);
          L.amount += rest;
          L.unpriced += rest;
        }
      });
    }
  }
  // ATO netting: losses reduce non-discount gains first, then discount gains,
  // and the 50% discount applies to what's left (see summarise_by_fy).
  function netTax(lines) {
    var disc = 0, nonDisc = 0, loss = 0, unpriced = 0;
    lines.forEach(function (L) {
      disc += L.disc; nonDisc += L.nonDisc; loss += L.loss; unpriced += L.unpriced;
    });
    var nonAfter = Math.max(0, nonDisc - loss), rest = Math.max(0, loss - nonDisc);
    var discAfter = Math.max(0, disc - rest);
    rest = Math.max(0, rest - disc);
    var taxable = nonAfter + discAfter / 2;
    return { taxable: taxable, tax: taxable * scen.taxRate / 100, carried: rest,
             loss: loss, unpriced: unpriced };
  }

  // --- The three strategies ---------------------------------------------------
  function finish(M, buys, sells, lines) {
    var after = M.buckets.map(function (m, i) { return m.value + buys[i] - sells[i]; });
    var sellLines = Object.keys(lines).map(function (k) { return lines[k]; })
      .filter(function (L) { return L.amount > EPS; })
      .sort(function (a, b) { return b.amount - a.amount; });
    return {
      buys: buys, sells: sells, after: after, sellLines: sellLines,
      bought: sum(buys), sold: sum(sells), tax: netTax(sellLines),
      off: outOfBand(M, after),
    };
  }
  function sellBuckets(M, sells) {
    var lines = {};
    M.buckets.forEach(function (m, i) {
      if (sells[i] > 1e-6) sellFrom(m, sells[i], scen.trim, lines);
    });
    return lines;
  }

  function contributionsOnly(M) {
    var buys = waterfill(values(M), fracs(M), scen.lump);
    return finish(M, buys, zeros(M), {});
  }

  function fullRebalance(M) {
    var T2 = M.total + scen.lump, buys = zeros(M), sells = zeros(M);
    M.buckets.forEach(function (m, i) {
      var d = m.t * T2 - m.value;
      if (d > 0) buys[i] = d; else sells[i] = -d;
    });
    return finish(M, buys, sells, sellBuckets(M, sells));
  }

  function hybrid(M) {
    var V = values(M), ts = fracs(M), T2 = M.total + scen.lump;
    var buys = waterfill(V, ts, scen.lump), sells = zeros(M);
    var cur = add(V, buys);
    // 1. Trim every over-band bucket back to the top of its band.
    M.buckets.forEach(function (m, i) {
      var over = cur[i] - m.upper * T2;
      if (over > 1e-6) { sells[i] = over; cur[i] -= over; }
    });
    var proceeds = sum(sells);
    // 2. Proceeds lift under-band buckets to the bottom of theirs first…
    function shortfalls() {
      return M.buckets.map(function (m, i) { return Math.max(0, m.lower * T2 - cur[i]); });
    }
    var short = shortfalls(), S = sum(short);
    if (proceeds > 1e-6 && S > 1e-6) {
      var used = Math.min(proceeds, S);
      short.forEach(function (s, i) { var a = s * used / S; buys[i] += a; cur[i] += a; });
      proceeds -= used;
    }
    // 3. …then anything left heads toward target as usual.
    if (proceeds > 1e-6) {
      var more = waterfill(cur, ts, proceeds);
      buys = add(buys, more);
      cur = add(cur, more);
    }
    // 4. Still under band with nothing over band to sell: trim the buckets
    //    sitting above target (inside their band) just enough to close it.
    short = shortfalls();
    S = sum(short);
    if (S > 1e-6) {
      var excess = M.buckets.map(function (m, i) { return Math.max(0, cur[i] - m.t * T2); });
      var E = sum(excess), moved = Math.min(S, E);
      if (moved > 1e-6) {
        excess.forEach(function (e, i) { var s = e * moved / E; sells[i] += s; cur[i] -= s; });
        short.forEach(function (s, i) { var a = s * moved / S; buys[i] += a; cur[i] += a; });
      }
    }
    return finish(M, buys, sells, sellBuckets(M, sells));
  }

  // --- Rendering ----------------------------------------------------------------
  function renderBanners(M) {
    var out = [];
    if (notice) out.push({ cls: 'warn', html: esc(notice) });
    (A.warnings || []).forEach(function (w) { out.push({ cls: 'warn', html: esc(w) }); });
    if (M.unassigned.length) {
      var uv = sum(M.unassigned.map(function (h) { return h.value; }));
      var n = M.unassigned.length;
      out.push({
        cls: 'warn',
        html: '<b>' + n + ' holding' + (n > 1 ? 's' : '') + '</b> (' + money(uv) +
          (M.grand > 0 ? ', ' + pct(uv / M.grand) + ' of the portfolio' : '') +
          ') ' + (n > 1 ? "aren't" : "isn't") + ' assigned to a bucket and ' +
          (n > 1 ? 'are' : 'is') + ' left out below: ' +
          esc(M.unassigned.map(function (h) { return h.code; }).join(', ')) +
          '. <a href="#tg-assign" data-open-assign>Assign holdings</a>',
      });
    }
    if (dirty()) {
      out.push({
        cls: '',
        html: downloaded
          ? 'Saved as <b>' + esc(A.downloadName) + '</b> in your Downloads folder. ' +
            'The next time you run the tracker it moves it into data/' + esc(A.fileName) + '.'
          : 'You have <b>unsaved changes</b> to targets or assignments. They are kept in ' +
            'this browser; save to file to make them permanent.',
        buttons: !downloaded,
      });
    }
    $('tg-banners').innerHTML = out.map(function (b) {
      return '<div class="tg-banner ' + b.cls + '"><span class="tg-banner-text">' + b.html +
        '</span>' + (b.buttons
          ? '<button class="tg-btn" type="button" data-save>Save to file</button>' +
            '<button class="tg-btn ghost" type="button" data-discard>Discard</button>'
          : '') + '</div>';
    }).join('');
    if ($('tg-discard')) $('tg-discard').hidden = !dirty();
    $('tg-save-hint').textContent = 'Downloads ' + A.downloadName + (WEB
      ? ' — a backup you can import here again, or use with the desktop tracker.'
      : '; the next run of the tracker picks it up from your Downloads folder.');
  }

  function renderBars(M) {
    var head = $('tg-headline'), bars = $('tg-bars');
    if (!M.buckets.length) {
      head.textContent = 'Add a bucket to get started.';
      bars.innerHTML = '';
      return;
    }
    if (M.total <= 0) {
      head.innerHTML = 'Assign your holdings to buckets to compare them with your targets.' +
        '<span class="tg-sub">Open “Assign holdings” on the right and pick a bucket for each.</span>';
    } else {
      var worst = M.buckets.slice().sort(function (a, b) {
        return Math.abs(b.actual - b.t) - Math.abs(a.actual - a.t);
      })[0];
      var off = M.buckets.filter(function (m) { return status(m.actual, m) !== 'on'; });
      var drift = (worst.actual - worst.t) * 100, gap = worst.value - worst.t * M.total;
      head.innerHTML = off.length
        ? esc(worst.label) + ' is furthest off target: ' + pct(worst.actual) + ' vs ' +
          worst.target + '% (' + pts(drift) + ', ' + money(Math.abs(gap)) +
          (gap > 0 ? ' over' : ' under') + ').' +
          '<span class="tg-sub">' + off.length + ' of ' + M.buckets.length +
          ' buckets are outside their tolerance band.</span>'
        : '✓ Every bucket is within its tolerance band.' +
          '<span class="tg-sub">Largest drift: ' + esc(worst.label) + ' ' + pts(drift) + '.</span>';
    }
    var top = Math.max.apply(null, M.buckets.map(function (m) { return Math.max(m.actual, m.upper); }));
    var scale = Math.min(100, Math.max(10, Math.ceil(top * 115 / 10) * 10)) / 100;
    function x(frac) { return Math.min(100, frac / scale * 100) + '%'; }
    bars.innerHTML = M.buckets.map(function (m) {
      var st = M.total > 0 ? status(m.actual, m) : 'on';
      var gap = m.value - m.t * M.total, drift = (m.actual - m.t) * 100;
      var chip = M.total <= 0 ? ''
        : st === 'on' ? '<span class="tg-chip on">On target</span>'
        : '<span class="tg-chip ' + st + '">' + (st === 'over' ? 'Over' : 'Under') + ' by ' +
          money(Math.abs(gap)) + ' · ' + pts(drift) + '</span>';
      return '<div class="tg-row">' +
        '<div class="tg-row-head"><span class="tg-dot" style="background:' + m.color + '"></span>' +
        '<span class="tg-name">' + esc(m.label) + '</span>' + chip +
        '<span class="tg-nums"><b>' + pct(m.actual) + '</b> · target ' + m.target + '% · ' +
        money(m.value) + '</span></div>' +
        '<div class="tg-track" role="img" aria-label="' + esc(m.label) + ': ' + pct(m.actual) +
        ' actual, ' + m.target + '% target">' +
        '<div class="tg-bandzone" style="left:' + x(m.lower) + ';width:calc(' + x(m.upper) +
        ' - ' + x(m.lower) + ')"></div>' +
        '<div class="tg-fill" style="width:' + x(m.actual) + ';background:' + m.color + '"></div>' +
        '<div class="tg-tick" style="left:' + x(m.t) + '"></div></div>' +
        '<div class="tg-codes">' + (m.codes.length ? esc(m.codes.join(' · ')) : 'No holdings assigned') +
        '</div></div>';
    }).join('');
  }

  function renderBucketRows() {
    var box = $('tg-brows');
    Array.prototype.slice.call(box.querySelectorAll('.tg-brow')).forEach(function (r) { r.remove(); });
    state.buckets.forEach(function (b, i) {
      var row = document.createElement('div');
      row.className = 'tg-brow';
      row.dataset.id = b.id;
      row.innerHTML =
        '<span class="tg-dot" style="background:' + A.palette[i % A.palette.length] + '"></span>' +
        '<input type="text" data-f="label" maxlength="40">' +
        '<input type="number" data-f="target" min="0" max="100" step="1">' +
        '<button class="gp-del" type="button" title="Remove bucket">×</button>';
      row.querySelector('[data-f="label"]').value = b.label;
      row.querySelector('[data-f="label"]').setAttribute('aria-label', 'Bucket name');
      row.querySelector('[data-f="target"]').value = b.target;
      row.querySelector('[data-f="target"]').setAttribute('aria-label', b.label + ' target %');
      row.querySelector('.gp-del').setAttribute('aria-label', 'Remove ' + b.label);
      box.appendChild(row);
    });
    $('tg-tol-abs').value = state.tolerance.absolute_pp;
    $('tg-tol-rel').value = state.tolerance.relative_pct;
  }

  function bucketOptions(selected) {
    return '<option value="">— Unassigned —</option>' + state.buckets.map(function (b) {
      return '<option value="' + esc(b.id) + '"' + (b.id === selected ? ' selected' : '') + '>' +
        esc(b.label) + '</option>';
    }).join('');
  }
  function renderAssign() {
    $('tg-arows').innerHTML = A.holdings.map(function (h) {
      var bid = state.holdings[h.code] || '';
      return '<div class="tg-arow' + (bid ? '' : ' unassigned') + '" data-code="' + esc(h.code) + '">' +
        '<span class="tg-acode">' + esc(h.code) + '</span>' +
        '<span class="tg-aval">' + money(h.value) + '</span>' +
        '<select aria-label="Bucket for ' + esc(h.code) + '">' + bucketOptions(bid) + '</select></div>';
    }).join('');
  }
  function refreshAssignLabels() {
    Array.prototype.slice.call($('tg-arows').querySelectorAll('option')).forEach(function (o) {
      var b = state.buckets.filter(function (x) { return x.id === o.value; })[0];
      if (b) o.textContent = b.label;
    });
  }

  function renderSum(M) {
    var el = $('tg-sum'), diff = 100 - M.targetSum;
    el.className = 'tg-sum' + (M.sumOk ? '' : ' bad');
    el.textContent = M.sumOk ? 'Targets add up to 100% ✓'
      : 'Targets add up to ' + +M.targetSum.toFixed(2) + '% — ' +
        (diff > 0 ? +diff.toFixed(2) + '% still to allocate' : +(-diff).toFixed(2) + '% too much');
    var n = M.unassigned.length;
    $('tg-assign-summary').textContent = 'Assign holdings' +
      (n ? ' (' + n + ' unassigned)' : ' (all ' + A.holdings.length + ' assigned)');
  }

  function metric(label, value, cls) {
    return '<div class="tg-metric"><div class="card-label">' + label + '</div><b' +
      (cls ? ' class="' + cls + '"' : '') + '>' + value + '</b></div>';
  }
  function afterTable(M, S) {
    var T = sum(S.after);
    return '<table class="tg-after"><thead><tr><th>Bucket</th><th>Now</th><th>After</th>' +
      '<th>Target</th></tr></thead><tbody>' + M.buckets.map(function (m, i) {
        var w = T > 0 ? S.after[i] / T : 0;
        return '<tr><td><span class="tg-dot" style="display:inline-block;margin-right:6px;background:' +
          m.color + '"></span>' + esc(m.label) + '</td><td>' + pct(m.actual) + '</td>' +
          '<td' + (status(w, m) !== 'on' ? ' class="tg-off"' : '') + '>' + pct(w) + '</td>' +
          '<td>' + m.target + '%</td></tr>';
      }).join('') + '</tbody></table>';
  }
  function actionList(M, S) {
    var items = [];
    S.sellLines.forEach(function (L) {
      var t = L.disc + L.nonDisc - L.loss;
      items.push('<li><span class="tg-verb sell">Sell</span> ' + money(L.amount) + ' of <b>' +
        esc(L.code) + '</b> <span class="tg-muted">≈' + units(L.units) + ' units · ' +
        (L.unpriced > EPS ? 'no parcel data for ' + money(L.unpriced)
          : (t >= 0 ? 'gain ' : 'loss ') + money(Math.abs(t))) + '</span></li>');
    });
    M.buckets.forEach(function (m, i) {
      var net = S.buys[i] - S.sells[i];
      if (net > EPS) {
        items.push('<li><span class="tg-verb buy">Buy</span> ' + money(net) + ' of <b>' +
          esc(m.label) + '</b> <span class="tg-muted">' +
          (m.codes.length ? 'e.g. ' + esc(m.codes.slice(0, 3).join(', ')) : 'no holdings yet') +
          '</span></li>');
      }
    });
    return items.length ? '<ul class="tg-do">' + items.join('') + '</ul>'
      : '<ul class="tg-do"><li>Nothing to buy or sell with these inputs.</li></ul>';
  }
  function taxMetrics(S) {
    if (S.sold <= EPS) return metric('Sold', '$0') + metric('Est. CGT', '$0 · none');
    var tx = S.tax;
    return metric('Sold', money(S.sold)) +
      metric('Est. CGT', money(tx.tax), tx.tax > EPS ? 'neg' : 'pos') +
      metric('Taxable gain', money(tx.taxable)) +
      metric('Buckets off band after', String(S.off.length), S.off.length ? 'neg' : 'pos');
  }
  function footFor(S) {
    var bits = [];
    if (S.tax.carried > EPS) bits.push('Leaves ' + money(S.tax.carried) + ' of capital losses to carry forward.');
    if (S.tax.unpriced > EPS) bits.push(money(S.tax.unpriced) + ' of the sale has no parcel data, so no tax is estimated for it.');
    return bits.length ? '<div class="tg-foot">' + bits.join(' ') + '</div>' : '';
  }

  function renderStrategies(M) {
    var box = $('tg-cards');
    var path = $('tg-path-panel');
    if (!M.sumOk || M.total <= 0 || !M.buckets.length) {
      box.innerHTML = '<div class="panel tg-empty">' + (M.total <= 0
        ? 'Assign holdings to buckets to see strategies.'
        : 'Set targets that add up to 100% to see strategies (they add up to ' +
          +M.targetSum.toFixed(2) + '% now).') + '</div>';
      path.hidden = true;
      return null;
    }
    path.hidden = false;
    var C = contributionsOnly(M), F = fullRebalance(M), H = hybrid(M);

    var need = needToTarget(M), needBand = needToBand(M);
    function when(amount) {
      if (amount === null) return 'not possible without selling';
      if (amount <= scen.lump + EPS) return scen.lump > EPS ? 'reached with the lump sum' : 'already there';
      if (!(scen.monthly > 0)) return 'needs a monthly contribution';
      var n = Math.ceil((amount - scen.lump) / scen.monthly);
      return '≈' + duration(n) + ' (' + monthLabel(n) + ')';
    }
    var afterLump = add(values(M), C.buys);
    var next = waterfill(afterLump, fracs(M), scen.monthly);
    var nextTxt = M.buckets.map(function (m, i) {
      return next[i] > EPS ? money(next[i]) + ' → ' + esc(m.label) : null;
    }).filter(Boolean).join(', ');
    var zeroHeld = M.buckets.filter(function (m) { return m.t <= 0 && m.value > EPS; });

    var contribFoot = '<div class="tg-foot">' +
      (need === null
        ? '<b>' + esc(zeroHeld.map(function (m) { return m.label; }).join(', ')) +
          '</b> has a 0% target but still holds shares, so contributions alone can’t reach target.'
        : 'New money to reach target without selling: <b>' + money(need) + '</b> — ' + when(need) + '.' +
          (needBand !== null && needBand < need - EPS
            ? '<br>To get inside every band: <b>' + money(needBand) + '</b> — ' + when(needBand) + '.' : '')) +
      (nextTxt ? '<br>Then each month: ' + nextTxt + '.' : '') + '</div>';

    box.innerHTML = [
      card('Contributions only', 'No selling, so no CGT — new money goes to the most underweight buckets.',
           'No CGT', C,
           metric('New money in', money(C.bought)) +
           metric('Buckets off band after', String(C.off.length), C.off.length ? 'neg' : 'pos'),
           contribFoot),
      card('Hybrid within band', 'Invest the lump sum first, then sell only enough to bring each bucket back inside its band.',
           'Smallest sale', H, taxMetrics(H), footFor(H)),
      card('Full rebalance now', 'Sell overweight buckets and buy underweight ones so every bucket is exactly on target today.',
           'Exact target', F, taxMetrics(F), footFor(F)),
    ].join('');
    return C;

    function card(title, desc, tag, S, metricsHtml, foot) {
      return '<div class="panel tg-card"><div><h4>' + title + '<span class="tg-tag">' + tag +
        '</span></h4><div class="tg-desc">' + desc + '</div></div>' +
        actionList(M, S) + '<div class="tg-metrics">' + metricsHtml + '</div>' +
        afterTable(M, S) + (foot || '') + '</div>';
    }
  }

  // Each bucket's share month by month as contributions are water-filled in.
  function renderPath(M) {
    var el = $('tg-path-chart'), cap = $('tg-path-caption');
    if (typeof Plotly === 'undefined') { $('tg-path-panel').hidden = true; return; }
    var need = needToTarget(M);
    if (!(scen.monthly > 0) || need === null) {
      cap.textContent = need === null
        ? 'A bucket with a 0% target still holds shares, so contributions alone never reach target.'
        : 'Add a monthly contribution to see how long contributions alone take to reach target.';
      Plotly.purge(el);
      el.style.display = 'none';
      return;
    }
    el.style.display = '';
    var months = Math.ceil(Math.max(0, need - scen.lump) / scen.monthly);
    var horizon = Math.min(240, Math.max(12, months + 3));
    var V = values(M), ts = fracs(M), xs = [], ys = M.buckets.map(function () { return []; });
    var start = new Date();
    for (var k = 0; k <= horizon; k++) {
      var cash = scen.lump + k * scen.monthly;
      var vals = add(V, waterfill(V, ts, cash)), T = sum(vals);
      xs.push(new Date(start.getFullYear(), start.getMonth() + k, 1));
      vals.forEach(function (v, i) { ys[i].push(T > 0 ? v / T * 100 : 0); });
    }
    var traces = [];
    M.buckets.forEach(function (m, i) {
      traces.push({
        x: xs, y: ys[i], mode: 'lines', name: m.label, line: { color: m.color, width: 2.5 },
        hovertemplate: '<b>' + esc(m.label) + '</b><br>%{x|%b %Y}: %{y:.1f}% (target ' + m.target +
          '%)<extra></extra>',
      });
      traces.push({
        x: [xs[0], xs[xs.length - 1]], y: [m.target, m.target], mode: 'lines', showlegend: false,
        hoverinfo: 'skip', line: { color: m.color, width: 1, dash: 'dot' },
      });
    });
    cap.textContent = months > 0
      ? 'At ' + money(scen.monthly) + '/month' + (scen.lump > EPS ? ' after a ' + money(scen.lump) + ' lump sum' : '') +
        ', contributions alone reach target in about ' + duration(months) +
        ' (' + monthLabel(months) + '). Dotted lines are targets; prices held flat.'
      : 'Contributions alone already reach target. Dotted lines are targets; prices held flat.';
    Plotly.react(el, traces, {
      margin: { t: 10, r: 10, b: 40, l: 48 }, height: el.offsetHeight || 300,
      paper_bgcolor: '#ffffff', plot_bgcolor: '#ffffff',
      font: { family: "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif", color: '#1a2b3c', size: 12 },
      yaxis: { ticksuffix: '%', gridcolor: '#e7ebf0', rangemode: 'tozero', zeroline: false },
      xaxis: { gridcolor: '#f0f3f6' },
      legend: { orientation: 'h', y: -0.18, x: 0 },
      hovermode: 'closest',
    }, { displayModeBar: false, responsive: true });
  }

  function render() {
    var M = model();
    renderBanners(M);
    renderBars(M);
    renderSum(M);
    if (REBALANCE && renderStrategies(M)) renderPath(M);
  }

  // --- Events -----------------------------------------------------------------
  function readBuckets() {
    var byId = {};
    state.buckets.forEach(function (b) { byId[b.id] = b; });
    state.buckets = Array.prototype.slice.call($('tg-brows').querySelectorAll('.tg-brow')).map(function (row) {
      return {
        id: row.dataset.id,
        label: row.querySelector('[data-f="label"]').value.trim() || byId[row.dataset.id].label,
        target: Math.max(0, num(row.querySelector('[data-f="target"]').value, 0)),
      };
    });
    state.tolerance = {
      absolute_pp: Math.max(0, num($('tg-tol-abs').value, 0)),
      relative_pct: Math.max(0, num($('tg-tol-rel').value, 0)),
    };
  }
  $('tg-brows').addEventListener('input', function () {
    readBuckets();
    refreshAssignLabels();
    commit();
  });
  ['tg-tol-abs', 'tg-tol-rel'].forEach(function (id) {
    $(id).addEventListener('input', function () { readBuckets(); commit(); });
  });
  $('tg-brows').addEventListener('click', function (e) {
    var del = e.target.closest('.gp-del');
    if (!del) return;
    var id = del.closest('.tg-brow').dataset.id;
    var b = state.buckets.filter(function (x) { return x.id === id; })[0];
    var held = Object.keys(state.holdings).filter(function (c) { return state.holdings[c] === id; });
    if (held.length && !window.confirm('Remove “' + b.label + '”? Its ' + held.length +
        ' holding' + (held.length > 1 ? 's' : '') + ' will become unassigned.')) return;
    readBuckets();
    state.buckets = state.buckets.filter(function (x) { return x.id !== id; });
    held.forEach(function (c) { delete state.holdings[c]; });
    renderBucketRows();
    renderAssign();
    commit();
  });
  $('tg-add-bucket').addEventListener('click', function () {
    readBuckets();
    var n = 1, ids = {};
    state.buckets.forEach(function (b) { ids[b.id] = true; });
    while (ids['bucket_' + n]) n++;
    var left = Math.max(0, 100 - sum(state.buckets.map(function (b) { return b.target; })));
    state.buckets.push({ id: 'bucket_' + n, label: 'New bucket', target: +left.toFixed(2) });
    renderBucketRows();
    renderAssign();
    commit();
    var rows = $('tg-brows').querySelectorAll('.tg-brow [data-f="label"]');
    var input = rows[rows.length - 1];
    input.focus();
    input.select();
  });
  $('tg-arows').addEventListener('change', function (e) {
    var row = e.target.closest('.tg-arow');
    if (!row) return;
    var code = row.dataset.code;
    if (e.target.value) state.holdings[code] = e.target.value;
    else delete state.holdings[code];
    row.classList.toggle('unassigned', !e.target.value);
    commit();
  });

  // YAML the loader in src/allocation.py reads back. Every scalar is a JSON
  // string (valid YAML), so tickers like NO or 0700 can't be retyped.
  function toYaml(c) {
    var q = JSON.stringify;
    var out = [
      '# Shares tracker — target allocation.',
      '# Saved from the dashboard on ' + new Date().toISOString().slice(0, 10) +
        '. Edit here or in the dashboard\'s Targets section.',
      'buckets:',
    ];
    c.buckets.forEach(function (b) {
      out.push('  - id: ' + q(b.id), '    label: ' + q(b.label), '    target: ' + b.target);
    });
    out.push('tolerance:', '  absolute_pp: ' + c.tolerance.absolute_pp,
             '  relative_pct: ' + c.tolerance.relative_pct);
    var codes = Object.keys(c.holdings).sort();
    out.push('holdings:' + (codes.length ? '' : ' {}'));
    codes.forEach(function (code) { out.push('  ' + q(code) + ': ' + q(c.holdings[code])); });
    return out.join('\n') + '\n';
  }
  function save() {
    var blob = new Blob([toYaml(state)], { type: 'application/x-yaml' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = A.downloadName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    if (WEB) return;
    // Keep the draft (the file isn't imported until the next run) but make
    // sure it exists even if the saved config matched the file already.
    store(KEY_DRAFT, dirty() ? { base: A.fingerprint, config: state } : null);
    downloaded = true;
    renderBanners(model());
  }
  function discard() {
    state = normalise(FILE);
    notice = null;
    renderBucketRows();
    renderAssign();
    commit();
  }
  $('tg-save').addEventListener('click', save);
  if ($('tg-discard')) $('tg-discard').addEventListener('click', discard);
  if (WEB) {
    // The page parses the file with the same loader as the desktop tracker
    // (parse_allocation) and rebuilds the dashboard with it.
    $('tg-import').addEventListener('click', function () { $('tg-import-file').click(); });
    $('tg-import-file').addEventListener('change', function () {
      var input = this, file = input.files && input.files[0];
      if (!file) return;
      var reader = new FileReader();
      reader.onload = function () {
        if (!tellPage({ type: 'sharesTracker.importTargets', yaml: String(reader.result) })) {
          notice = 'Importing needs the shares tracker web page around this dashboard.';
          render();
        }
      };
      reader.readAsText(file);
      input.value = '';
    });
  }
  $('tg-banners').addEventListener('click', function (e) {
    if (e.target.closest('[data-save]')) save();
    else if (e.target.closest('[data-discard]')) discard();
    else if (e.target.closest('[data-open-assign]')) $('tg-assign').open = true;
  });

  // Scenario inputs.
  var SCEN = { lump: 'tg-lump', monthly: 'tg-monthly', taxRate: 'tg-tax', trim: 'tg-trim' };
  if (REBALANCE) Object.keys(SCEN).forEach(function (k) {
    var el = $(SCEN[k]);
    el.value = scen[k];
    el.addEventListener(k === 'trim' ? 'change' : 'input', function () {
      scen[k] = k === 'trim' ? el.value : Math.max(0, num(el.value, 0));
      store(KEY_SCEN, scen);
      render();
    });
  });

  // The path chart is drawn here, not by to_html, so refit it ourselves.
  if (REBALANCE && window.ResizeObserver && typeof Plotly !== 'undefined') {
    new ResizeObserver(function () {
      var el = $('tg-path-chart');
      if (el._fullLayout) Plotly.Plots.resize(el);
    }).observe($('tg-path-chart'));
  }

  renderBucketRows();
  renderAssign();
  $('tg-assign').open = model().unassigned.length > 0;
  render();
})();
"""


def targets_payload(
    allocation: dict, by_code: pd.DataFrame, palette: list[str], web: bool = False
) -> str:
    """The page's TARGETS object, as JSON safe to inline in a <script>."""
    holdings = []
    for _, r in by_code.iterrows():
        v = r["market_value_aud"]
        holdings.append({
            "code": str(r["code"]),
            "value": round(float(v), 2) if pd.notna(v) and math.isfinite(v) else 0.0,
        })
    payload = {
        "config": allocation["config"].to_dict(),
        "fingerprint": allocation["fingerprint"],
        "holdings": holdings,
        "parcels": allocation.get("parcels") or {},
        "fileName": allocation.get("file_name", "allocation.yaml"),
        "downloadName": allocation.get("download_name", "shares-tracker-allocation.yaml"),
        "palette": palette,
    }
    if web:
        # The command line prints config warnings; the web edition has no
        # terminal, so the section shows them instead.
        payload["mode"] = "web"
        payload["warnings"] = list(allocation.get("warnings") or [])
    # "</" would end the <script> block early if a label ever contained it.
    return json.dumps(payload).replace("</", "<\\/")
