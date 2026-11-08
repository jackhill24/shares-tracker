// The Worker end to end in `wrangler dev` (Cloudflare's local runtime), with
// a mock Yahoo serving made-up prices — Yahoo itself only answers the deployed
// relay. Checks the origin rules, validation, the response, cache headers,
// that the upstream request is honest, and the rate limit. Run: npm test
import http from "node:http";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const SITE = "https://someone.github.io";
const utc = (iso) => Date.parse(iso) / 1000;
let failures = 0;
function check(name, ok, detail = "") {
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${ok || !detail ? "" : ` — ${detail}`}`);
  if (!ok) failures++;
}

// --- mock upstream ------------------------------------------------------------
const seen = [];
const mock = http.createServer((req, res) => {
  seen.push({ url: req.url, headers: req.headers });
  const symbol = decodeURIComponent(req.url.split("/v8/finance/chart/")[1].split("?")[0]);
  const send = (status, body) => { res.writeHead(status, { "content-type": "application/json" }); res.end(JSON.stringify(body)); };
  if (symbol === "NOPE.AX") return send(404, { chart: { result: null, error: { code: "Not Found", description: "No data found, symbol may be delisted" } } });
  if (symbol === "BUSY.AX") return send(429, { error: "Too Many Requests" });
  send(200, { chart: { result: [{
    meta: { currency: "AUD", symbol, exchangeTimezoneName: "Australia/Sydney", gmtoffset: 39600, regularMarketPrice: 12.5 },
    timestamp: [utc("2026-01-14T23:00:00Z"), utc("2026-07-01T00:00:00Z"), utc("2026-07-02T00:00:00Z")],
    indicators: { quote: [{ close: [10.25, 11.5, null] }], adjclose: [{ adjclose: [9, 9, 9] }] },
  }], error: null } });
});
await new Promise((r) => mock.listen(0, "127.0.0.1", r));
const mockPort = mock.address().port;

// --- the Worker -----------------------------------------------------------------
const port = 8700 + Math.floor(Math.random() * 200);
const dev = spawn(path.join(ROOT, "node_modules/.bin/wrangler"), [
  "dev", "--ip", "127.0.0.1", "--port", String(port),
  "--var", `UPSTREAM:http://127.0.0.1:${mockPort}`, "--var", `ALLOWED_ORIGINS:${SITE}`,
], { cwd: ROOT, env: { ...process.env, WRANGLER_SEND_METRICS: "false" } });
let devLog = "";
await new Promise((resolve, reject) => {
  const t = setTimeout(() => reject(new Error("wrangler dev didn't start:\n" + devLog)), 60000);
  const on = (d) => { devLog += d; if (/Ready on/.test(devLog)) { clearTimeout(t); resolve(); } };
  dev.stdout.on("data", on); dev.stderr.on("data", on);
});
const base = `http://127.0.0.1:${port}`;
const get = (q, origin = SITE, method = "GET") =>
  fetch(`${base}${q}`, { method, headers: origin ? { Origin: origin } : {} });

try {
  let r = await get("/chart?symbol=TEST.AX&start=2026-01-01&end=2026-07-31");
  const body = await r.json();
  check("allowed origin gets 200", r.status === 200, `${r.status} ${JSON.stringify(body)}`);
  check("CORS header echoes the site", r.headers.get("access-control-allow-origin") === SITE);
  check("closes dated in Sydney time, nulls dropped",
        JSON.stringify(body.closes) === JSON.stringify({ "2026-01-15": 10.25, "2026-07-01": 11.5 }),
        JSON.stringify(body.closes));
  check("last and currency passed on", body.last === 12.5 && body.currency === "AUD" && body.symbol === "TEST.AX");
  check("finished range cached 30 days", /max-age=2592000/.test(r.headers.get("cache-control") || ""));
  const up = seen.at(-1);
  check("upstream request is honest (own User-Agent, no cookies)",
        /shares-tracker/.test(up.headers["user-agent"] || "") && !up.headers.cookie, JSON.stringify(up.headers));

  r = await get("/chart?symbol=TEST.AX&start=2026-01-01&end=" + new Date().toISOString().slice(0, 10));
  check("range including today cached 10 minutes", /max-age=600\b/.test(r.headers.get("cache-control") || ""));

  r = await get("/chart?symbol=TEST.AX&start=2026-01-01&end=2026-07-31", "http://localhost:5173");
  check("localhost allowed", r.status === 200);
  r = await get("/chart?symbol=TEST.AX&start=2026-01-01&end=2026-07-31", "https://evil.example");
  check("other origin refused", r.status === 403 && !r.headers.get("access-control-allow-origin"));
  r = await get("/chart?symbol=TEST.AX&start=2026-01-01&end=2026-07-31", null);
  check("no origin refused", r.status === 403);
  r = await get("/chart?symbol=TEST.AX&start=2026-01-01&end=2026-07-31", SITE, "POST");
  check("POST refused", r.status === 405);
  r = await get("/chart?symbol=TEST.AX&start=2026-01-01&end=2026-07-31", SITE, "OPTIONS");
  check("preflight answered", r.status === 204 && r.headers.get("access-control-allow-methods") === "GET");
  r = await get("/chart?symbol=bad/sym&start=2026-01-01&end=2026-07-31");
  check("bad symbol is 400 with CORS", r.status === 400 && r.headers.get("access-control-allow-origin") === SITE);
  r = await get("/other");
  check("other paths 404", r.status === 404);
  r = await get("/chart?symbol=NOPE.AX&start=2026-01-01&end=2026-07-31");
  check("unknown symbol is 404", r.status === 404);
  r = await get("/chart?symbol=BUSY.AX&start=2026-01-01&end=2026-07-31");
  check("upstream refusal is 502", r.status === 502);

  const statuses = await Promise.all(Array.from({ length: 140 }, () =>
    get("/chart?symbol=TEST.AX&start=2026-01-01&end=2026-07-31").then((x) => x.status)));
  const limited = statuses.filter((s) => s === 429).length;
  check("rate limit kicks in past 120 a minute", limited > 0, `${limited} of 140 limited`);
} finally {
  dev.kill();
  mock.close();
}
console.log(failures ? `\n${failures} check(s) failed` : "\nAll integration checks passed.");
process.exit(failures ? 1 : 0);
