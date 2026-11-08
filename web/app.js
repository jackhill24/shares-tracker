// The web edition's page: runs the tracker's Python (py-worker.js) on an
// uploaded CSV or the demo, fetches prices through the relay (prices.js), and
// shows the dashboard it builds in an iframe.
//
// A build: requirements (checks the trades, lists what to fetch) → fetch →
// build (the HTML). The dashboard keeps its own settings in localStorage
// under its storage key ("demo" or "mine"); its Targets section posts edits
// here, and they're passed back in on the next build.
import { DEFAULT_SETTINGS, RELAY_URL } from "./config.js";
import { PriceError, fetchAll } from "./prices.js";
import { Python, StalledError } from "./python.js";
import * as store from "./store.js";

const $ = (id) => document.getElementById(id);
const MAX_CSV_BYTES = 5 * 1024 * 1024;
const SYMBOL_RE = /^[A-Z0-9.^=-]{1,15}$/;
const MAX_BENCHMARKS = 4;
const FIRST_VISIT_HINT = "The first visit downloads about 25 MB. After that it comes from your browser's cache.";
const SLOW_MS = 45000;
const SLOW_HINT = "This is taking longer than usual. On a slow connection it can take a few minutes; " +
  "if nothing changes, reload the page to try again.";
const DASHBOARDS = {
  demo: { title: "Demo Portfolio", badge: "Demo", download: "shares-tracker-demo-allocation.yaml" },
  mine: { title: "Portfolio Dashboard", badge: "Your portfolio", download: "shares-tracker-allocation.yaml" },
};

// --- Python, loading in the background from the start ------------------------------

// "Loading Plotly" → "loading Plotly", for use mid-sentence.
const midSentence = (s) => s.charAt(0).toLowerCase() + s.slice(1);

const py = new Python((status) => {
  const el = $("py-status");
  if (status === "ready") el.textContent = "Python is ready.";
  else if (status.startsWith("failed")) el.textContent = `Python couldn't load (${status.slice(8)}). Try reloading the page.`;
  else el.textContent = `Getting Python ready in the background: ${midSentence(status)}…`;
  if (currentStep === "python") stepDetail("python", status === "ready" ? "" : midSentence(status));
});

// --- Views ------------------------------------------------------------------------

function show(view) {
  for (const id of ["landing", "working", "problem", "dash"]) $(id).hidden = id !== view;
  const onDash = view === "dash";
  $("dash-badge").hidden = !onDash;
  $("dash-actions").hidden = !onDash;
  document.body.classList.toggle("on-dash", onDash);
  if (view === "landing") refreshLanding();
  window.scrollTo(0, 0);
}

let currentStep = null;
function step(name) {
  currentStep = name;
  let reached = false;
  for (const li of $("steps").children) {
    const here = li.dataset.step === name;
    li.className = here ? "active" : reached ? "" : "done";
    if (here) reached = true;
    if (reached) li.querySelector(".detail").textContent = "";
  }
  if (name === "python" && py.status !== "ready") stepDetail("python", midSentence(py.status));
}
function stepDetail(name, text) {
  const li = $("steps").querySelector(`[data-step="${name}"] .detail`);
  if (li) li.textContent = text ? `· ${text}` : "";
}

function problem({ title, intro = "", errors = [], retry = null, pick = true }) {
  $("problem-title").textContent = title;
  $("problem-intro").textContent = intro;
  const list = $("problem-list");
  list.replaceChildren(...errors.map((e) => Object.assign(document.createElement("li"), { textContent: e })));
  $("problem-retry").hidden = !retry;
  $("problem-retry").onclick = retry;
  $("problem-pick").hidden = !pick;
  show("problem");
}

function setNotices(lines) {
  const box = $("notices");
  box.hidden = !lines.length;
  if (!lines.length) return;
  $("notices-summary").textContent = lines.length === 1
    ? "1 note about this dashboard"
    : `${lines.length} notes about this dashboard`;
  $("notices-list").replaceChildren(
    ...lines.map((l) => Object.assign(document.createElement("li"), { textContent: l })),
  );
}
$("notices-close").addEventListener("click", () => { $("notices").hidden = true; });

async function refreshLanding() {
  const saved = await store.kv.get("trades");
  $("returning").hidden = !saved;
  if (saved) {
    const when = new Date(saved.savedAt).toLocaleDateString("en-AU", { day: "numeric", month: "long", year: "numeric" });
    $("returning-text").textContent = `Your trades (${saved.name}, saved ${when}) are in this browser.`;
  }
}

// --- Building a dashboard ---------------------------------------------------------

// The dashboard on screen: which one, its trades, and the prices it was built
// from (reused when only the targets change).
let session = null;
let runId = 0;

async function settings() {
  return (await store.kv.get("settings")) || null;
}

async function targetsFor(key) {
  const saved = await store.kv.get(`targets.${key}`);
  if (saved || key !== "demo") return saved;
  const demo = await py.call("demo");
  return demo.ok ? demo.targets : null;
}

/** Build and show a dashboard. `fileName` is set for a newly chosen CSV,
 *  which is saved only once it has built successfully. */
async function run(key, csv, { fileName = null } = {}) {
  const id = ++runId;
  const stale = () => id !== runId;
  const isNewFile = fileName !== null;
  $("work-title").textContent = key === "demo" ? "Building the demo" : "Building your dashboard";
  $("work-hint").textContent = FIRST_VISIT_HINT;
  $("work-hint").hidden = py.status === "ready";
  show("working");
  const retry = () => run(key, csv, { fileName });

  step("python");
  const slow = setTimeout(() => {
    if (!stale() && currentStep === "python") $("work-hint").textContent = SLOW_HINT;
  }, SLOW_MS);
  try {
    await py.ready;
  } catch (e) {
    return problem({
      title: "Python couldn't load",
      intro: "A download was interrupted or blocked. Check your connection (and any ad blocker) and try again.",
      errors: [e.message], retry: () => location.reload(), pick: false,
    });
  } finally {
    clearTimeout(slow);
  }
  if (stale()) return;

  step("check");
  const settingsJson = JSON.stringify(await settings());
  const req = await py.call("requirements", csv, settingsJson).catch(bug);
  if (stale() || !req) return;
  if (!req.ok) {
    return problem({
      title: key === "demo" ? "The demo didn't work"
        : isNewFile ? "That file didn't work" : "Your saved trades didn't work",
      intro: "Fix these in your spreadsheet, save it as CSV and choose it again. Rows are numbered as in a spreadsheet, with the header as row 1.",
      errors: req.errors,
    });
  }
  stepDetail("check", `${req.trades} trades`);

  step("prices");
  let fetched, priceWarnings;
  try {
    ({ fetched, warnings: priceWarnings } = await fetchAll(req.requirements.requests, {
      relay: RELAY_URL,
      store: store.prices,
      onProgress: (done, total) => { if (!stale()) stepDetail("prices", `${done} of ${total}`); },
      onWait: (ms) => { if (!stale()) stepDetail("prices", `the price service is busy, waiting ${Math.round(ms / 1000)} s`); },
    }));
  } catch (e) {
    if (stale()) return;
    if (!(e instanceof PriceError)) return bug(e);
    const [first, ...rest] = e.message.split("\n");
    return problem({ title: "Couldn't get prices", intro: first, errors: rest, retry, pick: false });
  }
  if (stale()) return;

  session = { key, csv, settingsJson, fetched, warnings: [...req.warnings, ...priceWarnings, ...benchmarkWarnings(fetched)] };
  await build(isNewFile ? fileName : null);
}

/** The build step, from the prices already in `session`. */
async function build(newFileName = null) {
  const id = runId;
  const { key, csv, settingsJson, fetched } = session;
  const dash = DASHBOARDS[key];
  step("build");
  let targets;
  try {
    targets = await targetsFor(key);
  } catch (e) {
    return bug(e);
  }
  const out = await py.call(
    "build", csv, settingsJson, JSON.stringify(targets), JSON.stringify(fetched),
    key, dash.title, dash.download,
  ).catch(bug);
  if (id !== runId || !out) return;
  if (!out.ok) return problem({ title: "Couldn't build the dashboard", errors: out.errors });

  if (newFileName !== null) {
    await store.kv.put("trades", { csv, name: newFileName, savedAt: new Date().toISOString() });
  }
  $("dash-badge").textContent = dash.badge;
  $("data-button").hidden = key !== "mine";
  setNotices(unique([...session.warnings, ...out.warnings]));
  $("frame").srcdoc = out.html;
  show("dash");
  if (!store.persistent && key === "mine") {
    setNotices(unique([
      "This browser isn't letting the site save anything, so your trades will be forgotten when you close this tab.",
      ...session.warnings, ...out.warnings,
    ]));
  }
}

function benchmarkWarnings(fetched) {
  return fetched
    .filter((f) => f.kind === "price" && f.currency && f.currency !== "AUD" && isBenchmark(f.symbol))
    .map((f) => `The benchmark ${f.symbol} is priced in ${f.currency}, not AUD, so comparisons with it are wrong. Change it in Settings.`);
}
let benchmarkSymbols = new Set(DEFAULT_SETTINGS.benchmarks.map((b) => b.ticker));
const isBenchmark = (symbol) => benchmarkSymbols.has(symbol);

function unique(lines) {
  return [...new Set(lines)];
}

function bug(e) {
  console.error(e);
  if (e instanceof StalledError) {
    problem({
      title: "Python stopped responding",
      intro: "This device stopped running the tracker, most likely because it ran out of memory. " +
        "Phones, iPhones especially, can hit this. Close other tabs and try again, or use a computer.",
      errors: [e.message], retry: () => location.reload(), pick: false,
    });
    return null;
  }
  problem({
    title: "Something went wrong",
    intro: "This is a bug in the tracker, not a problem with your file. Reloading the page may help.",
    errors: [String(e.message || e).split("\n").slice(-3).join("\n")],
    retry: () => location.reload(), pick: false,
  });
  return null;
}

// --- Starting points ------------------------------------------------------------------

async function startDemo() {
  show("working");
  step("python");
  try {
    await py.ready;
  } catch {
    return run("demo", "");  // shows the loading problem
  }
  const demo = await py.call("demo").catch(bug);
  if (demo?.ok) run("demo", demo.csv);
}

async function startMine() {
  const saved = await store.kv.get("trades");
  if (saved) run("mine", saved.csv);
  else show("landing");
}

async function useTradesFile(file) {
  if (!file) return;
  if (file.size > MAX_CSV_BYTES) {
    return problem({ title: "That file is too big", errors: [`${file.name} is over 5 MB. Is it the right file?`] });
  }
  let text;
  try {
    text = await file.text();
  } catch (e) {
    return problem({ title: "Couldn't read that file", errors: [e.message] });
  }
  closeDialogs();
  run("mine", text, { fileName: file.name });
}

$("try-demo").addEventListener("click", startDemo);
$("open-mine").addEventListener("click", startMine);

for (const btn of document.querySelectorAll("[data-pick]")) {
  btn.addEventListener("click", () => $(`pick-${btn.dataset.pick}`).click());
}
$("pick-trades").addEventListener("change", (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  useTradesFile(file);
});

const drop = $("drop");
for (const type of ["dragenter", "dragover"]) {
  drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.add("over"); });
}
for (const type of ["dragleave", "drop"]) {
  drop.addEventListener(type, () => drop.classList.remove("over"));
}
drop.addEventListener("drop", (e) => {
  e.preventDefault();
  useTradesFile(e.dataTransfer.files[0]);
});
// A file dropped anywhere else shouldn't make the browser navigate away to it.
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());

for (const el of document.querySelectorAll("[data-go]")) {
  el.addEventListener("click", () => { runId++; show(el.dataset.go); });
}

// --- Targets, from the dashboard ------------------------------------------------------

window.addEventListener("message", async (e) => {
  if (e.source !== $("frame").contentWindow || !session) return;
  const msg = e.data || {};
  if (msg.type === "sharesTracker.targets" && msg.config) {
    await store.kv.put(`targets.${session.key}`, msg.config);
  } else if (msg.type === "sharesTracker.importTargets" && typeof msg.yaml === "string") {
    const out = await py.call("parse_targets", msg.yaml).catch(bug);
    if (!out) return;
    if (!out.ok) return setNotices(out.errors);
    await store.kv.put(`targets.${session.key}`, out.targets);
    session.warnings = unique([...out.warnings.map((w) => `Imported targets: ${w}`), ...session.warnings]);
    runId++;
    show("working");
    await build();
  }
});

// --- Settings -------------------------------------------------------------------------

function benchRow(b = { ticker: "", label: "" }) {
  const row = document.createElement("div");
  row.className = "bench-row";
  row.innerHTML = `
    <input type="text" class="bench-ticker" aria-label="Yahoo ticker" placeholder="VAS.AX" maxlength="15" autocapitalize="characters" spellcheck="false">
    <input type="text" class="bench-label" aria-label="Label" placeholder="ASX 300" maxlength="40">
    <button class="icon-btn" type="button" aria-label="Remove benchmark">×</button>`;
  row.querySelector(".bench-ticker").value = b.ticker;
  row.querySelector(".bench-label").value = b.label;
  row.querySelector("button").addEventListener("click", () => { row.remove(); syncBenchAdd(); });
  return row;
}
function syncBenchAdd() {
  $("bench-add").hidden = $("bench-rows").children.length >= MAX_BENCHMARKS;
}
function fillSettings(s) {
  $("bench-rows").replaceChildren(...s.benchmarks.map(benchRow));
  $("risk-free").value = +(s.risk.rba_cash_rate * 100).toFixed(4);
  $("settings-error").textContent = "";
  syncBenchAdd();
}
function readSettings() {
  const benchmarks = [];
  for (const row of $("bench-rows").children) {
    const ticker = row.querySelector(".bench-ticker").value.trim().toUpperCase();
    const label = row.querySelector(".bench-label").value.trim();
    if (!ticker && !label) continue;
    if (!SYMBOL_RE.test(ticker)) throw new Error(`"${ticker || label}" isn't a Yahoo ticker. Use letters, digits and . ^ = - only, e.g. VAS.AX.`);
    benchmarks.push({ ticker, label: label || ticker });
  }
  if (!benchmarks.length) throw new Error("Add at least one benchmark.");
  const rate = Number($("risk-free").value);
  if ($("risk-free").value === "" || !(rate >= 0 && rate <= 20)) throw new Error("The risk-free rate must be between 0 and 20%.");
  return { benchmarks, risk: { rba_cash_rate: rate / 100 } };
}

$("bench-add").addEventListener("click", () => {
  $("bench-rows").append(benchRow());
  syncBenchAdd();
  $("bench-rows").lastElementChild.querySelector("input").focus();
});
$("settings-reset").addEventListener("click", () => fillSettings(DEFAULT_SETTINGS));
$("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  let s;
  try {
    s = readSettings();
  } catch (err) {
    $("settings-error").textContent = err.message;
    return;
  }
  await store.kv.put("settings", s);
  benchmarkSymbols = new Set(s.benchmarks.map((b) => b.ticker));
  $("settings-dialog").close();
  if (session) run(session.key, session.csv);
});

// --- Your data ------------------------------------------------------------------------

async function fillDataSummary() {
  const saved = await store.kv.get("trades");
  const rows = saved ? saved.csv.trim().split("\n").length - 1 : 0;
  $("data-summary").textContent = saved
    ? `Saved in this browser: ${saved.name} (${rows} rows), from ${new Date(saved.savedAt).toLocaleString("en-AU")}.`
    : "Nothing saved yet.";
}

function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

$("export-data").addEventListener("click", async () => {
  const data = {
    app: "shares-tracker",
    version: 1,
    exported: new Date().toISOString(),
    trades: await store.kv.get("trades"),
    targets: await store.kv.get("targets.mine"),
    settings: await store.kv.get("settings"),
    dashboard: store.localEntries("mine"),
  };
  const day = new Date().toISOString().slice(0, 10);
  download(`shares-tracker-data-${day}.json`, JSON.stringify(data, null, 2), "application/json");
});

$("pick-restore").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  let data;
  try {
    data = JSON.parse(await file.text());
    if (data?.app !== "shares-tracker" || !data.trades?.csv) throw new Error("not an export from this site");
  } catch (err) {
    closeDialogs();
    return problem({ title: "Couldn't restore from that file", errors: [`${file.name}: ${err.message}.`], pick: false });
  }
  await store.kv.put("trades", {
    csv: String(data.trades.csv),
    name: String(data.trades.name || "trades.csv"),
    savedAt: data.trades.savedAt || new Date().toISOString(),
  });
  if (data.targets) await store.kv.put("targets.mine", data.targets);
  if (data.settings) await store.kv.put("settings", data.settings);
  store.restoreLocalEntries(data.dashboard, "mine");
  await loadBenchmarkSymbols();
  closeDialogs();
  startMine();
});

$("clear-data").addEventListener("click", async () => {
  if (!confirm("Remove your trades, targets, settings, goal planner and saved prices from this browser? This can't be undone.")) return;
  await store.clearAll();
  session = null;
  runId++;
  $("frame").srcdoc = "";
  benchmarkSymbols = new Set(DEFAULT_SETTINGS.benchmarks.map((b) => b.ticker));
  closeDialogs();
  show("landing");
});

// --- Dialogs --------------------------------------------------------------------------

for (const btn of document.querySelectorAll("[data-open]")) {
  btn.addEventListener("click", async () => {
    const id = btn.dataset.open;
    if (id === "settings-dialog") fillSettings((await settings()) || DEFAULT_SETTINGS);
    if (id === "data-dialog") await fillDataSummary();
    $(id).showModal();
  });
}
for (const btn of document.querySelectorAll("[data-close]")) {
  btn.addEventListener("click", () => btn.closest("dialog").close());
}
function closeDialogs() {
  for (const d of document.querySelectorAll("dialog[open]")) d.close();
}

// --- Start ----------------------------------------------------------------------------

async function loadBenchmarkSymbols() {
  const s = await settings();
  if (s) benchmarkSymbols = new Set(s.benchmarks.map((b) => b.ticker));
}

await loadBenchmarkSymbols();
// A return visit with saved trades goes straight to the dashboard.
if (await store.kv.get("trades")) startMine();
else show("landing");
