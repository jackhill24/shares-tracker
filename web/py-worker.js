// The tracker's Python, run by Pyodide in a background thread so the page
// stays responsive. Loading starts as soon as the page opens.
//
// The page sends {id, cmd, args} and gets back {id, result} or {id, error};
// cmd is one of src/webapp.py's entry points, each taking and returning JSON
// text. While loading, it sends {status} lines for the progress display.
import { loadPyodide } from "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs";

const PACKAGES = ["numpy", "pandas", "jinja2", "pyyaml", "micropip"];
const PLOTLY = "plotly==6.7.0";  // not bundled with Pyodide; installed from PyPI
const APP = "/home/pyodide/app";
const COMMANDS = new Set(["demo", "requirements", "build", "parse_targets"]);

const status = (text) => self.postMessage({ status: text });

const ready = (async () => {
  status("Downloading Python");
  const py = await loadPyodide();
  status("Loading pandas and numpy");
  await py.loadPackage(PACKAGES, { messageCallback: () => {} });
  status("Loading Plotly");
  await py.runPythonAsync(`import micropip\nawait micropip.install("${PLOTLY}")`);
  status("Unpacking the tracker");
  const res = await fetch(new URL("./py/app.zip", import.meta.url));
  if (!res.ok) throw new Error(`couldn't download the tracker's code (${res.status})`);
  py.unpackArchive(await res.arrayBuffer(), "zip", { extractDir: APP });
  status("Starting the tracker");
  py.runPython(`import os, sys\nos.chdir("${APP}")\nsys.path.insert(0, "${APP}")`);
  return py.pyimport("src.webapp");  // imports pandas, plotly etc. now, not on first use
})();

// If a download is cut off mid-way, Pyodide can reject an internal promise
// and leave loadPyodide pending forever; catch that so the page can say so.
// Once loaded, such an error means Python itself broke mid-call (so no reply
// is coming): send it as a crash, which fails the waiting call.
let loaded = false;
const fail = (e) => {
  const message = String(e?.message || e);
  if (!loaded) status(`failed: ${message}`);
  else self.postMessage({ crash: message });
};
self.addEventListener("unhandledrejection", (e) => fail(e.reason));
ready.then(() => { loaded = true; status("ready"); }, fail);

self.onmessage = async ({ data: { id, cmd, args = [] } }) => {
  try {
    const api = await ready;
    if (cmd === "init") return self.postMessage({ id, result: true });
    if (!COMMANDS.has(cmd)) throw new Error(`unknown command ${cmd}`);
    self.postMessage({ id, result: JSON.parse(api[cmd](...args)) });
  } catch (e) {
    self.postMessage({ id, error: String(e?.message || e) });
  }
};
