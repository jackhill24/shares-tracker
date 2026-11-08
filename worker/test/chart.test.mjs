// The relay's rules (src/chart.js), no network: run with `node --test test/`.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  HttpError, addDays, cacheControl, isAllowedOrigin, parseChart, parseQuery, upstreamUrl,
} from "../src/chart.js";

const utc = (iso) => Date.parse(iso) / 1000;
const q = (o) => new URLSearchParams(o);
const TODAY = "2026-11-08";

function chart(timezone, rows, meta = {}) {
  return {
    chart: {
      result: [{
        meta: { currency: "AUD", exchangeTimezoneName: timezone, regularMarketPrice: 9.5, ...meta },
        timestamp: rows.map((r) => r[0]),
        indicators: { quote: [{ close: rows.map((r) => r[1]) }], adjclose: [{ adjclose: rows.map(() => 1) }] },
      }],
      error: null,
    },
  };
}

test("bars are dated in the exchange's time zone, across daylight saving", () => {
  // London FX: bars stamped at local midnight. In summer that's 23:00 UTC the
  // day before, which today's (winter) gmtoffset would misdate.
  const fx = parseChart(chart("Europe/London", [
    [utc("2026-06-30T23:00:00Z"), 0.66],  // 1 July 00:00 BST
    [utc("2026-11-02T00:00:00Z"), 0.65],  // 2 Nov 00:00 GMT
  ], { gmtoffset: 0 }), "2026-01-01", TODAY);
  assert.deepEqual(fx.closes, { "2026-07-01": 0.66, "2026-11-02": 0.65 });

  // ASX opens 10:00 Sydney: 23:00 UTC the day before in summer (AEDT).
  const asx = parseChart(chart("Australia/Sydney", [
    [utc("2026-01-14T23:00:00Z"), 10],  // 15 Jan 10:00 AEDT
    [utc("2026-07-01T00:00:00Z"), 11],  // 1 Jul 10:00 AEST
  ]), "2026-01-01", TODAY);
  assert.deepEqual(asx.closes, { "2026-01-15": 10, "2026-07-01": 11 });

  const us = parseChart(chart("America/New_York", [
    [utc("2026-03-06T14:30:00Z"), 200],  // 09:30 EST
    [utc("2026-03-09T13:30:00Z"), 201],  // 09:30 EDT
  ]), "2026-01-01", TODAY);
  assert.deepEqual(us.closes, { "2026-03-06": 200, "2026-03-09": 201 });
});

test("closes come from quote.close, trimmed to the range, gaps dropped", () => {
  const out = parseChart(chart("UTC", [
    [utc("2026-05-01T00:00:00Z"), 1],
    [utc("2026-05-02T00:00:00Z"), null],   // no trade
    [utc("2026-05-03T00:00:00Z"), 3],
    [utc("2026-05-04T00:00:00Z"), 4],      // after end: padding
  ]), "2026-05-01", "2026-05-03");
  assert.deepEqual(out.closes, { "2026-05-01": 1, "2026-05-03": 3 });
  assert.equal(out.last, 9.5);
  assert.equal(out.currency, "AUD");
});

test("a second bar on the same day (Yahoo's live bar) wins", () => {
  const out = parseChart(chart("UTC", [
    [utc("2026-11-06T00:00:00Z"), 5],
    [utc("2026-11-06T15:42:10Z"), 5.25],
  ]), "2026-11-01", TODAY);
  assert.deepEqual(out.closes, { "2026-11-06": 5.25 });
});

test("a range with no trading has no timestamps; no result is a 404", () => {
  const empty = { chart: { result: [{ meta: { exchangeTimezoneName: "UTC" }, indicators: { quote: [{}] } }] } };
  assert.deepEqual(parseChart(empty, "2026-01-01", "2026-01-31").closes, {});
  assert.equal(parseChart(empty, "2026-01-01", "2026-01-31").last, null);
  assert.throws(
    () => parseChart({ chart: { result: null, error: { description: "No data found" } } }, "2026-01-01", TODAY),
    (e) => e instanceof HttpError && e.status === 404 && /No data found/.test(e.message),
  );
});

test("requests are validated", () => {
  assert.deepEqual(parseQuery(q({ symbol: "AUDUSD=X", start: "2025-11-08", end: TODAY }), TODAY),
                   { symbol: "AUDUSD=X", start: "2025-11-08", end: TODAY });
  for (const bad of [
    { symbol: "vas.ax", start: "2025-01-01", end: TODAY },          // lower case
    { symbol: "A/B", start: "2025-01-01", end: TODAY },
    { symbol: "ABCDEFGHIJKLMNOP", start: "2025-01-01", end: TODAY }, // 16 long
    { symbol: "VAS.AX", start: "2025-02-30", end: TODAY },          // not a date
    { symbol: "VAS.AX", start: "2026-01-01", end: "2025-01-01" },   // backwards
    { symbol: "VAS.AX", start: "1960-01-01", end: TODAY },          // too early
    { symbol: "VAS.AX", start: "2025-01-01", end: "2026-12-01" },   // future
    { symbol: "VAS.AX", start: "1980-01-01", end: "2025-01-01" },   // > 40 years
    { start: "2025-01-01", end: TODAY },
  ]) {
    assert.throws(() => parseQuery(q(bad), TODAY), (e) => e.status === 400, JSON.stringify(bad));
  }
});

test("origins: the listed site and localhost only", () => {
  const allowed = ["https://someone.github.io"];
  assert.ok(isAllowedOrigin("https://someone.github.io", allowed));
  assert.ok(isAllowedOrigin("http://localhost:8000", allowed));
  assert.ok(isAllowedOrigin("http://127.0.0.1", allowed));
  for (const o of [null, "", "https://evil.example", "https://someone.github.io.evil.example",
                   "http://localhost.evil.example", "https://localhost:8000"]) {
    assert.ok(!isAllowedOrigin(o, allowed), String(o));
  }
});

test("upstream URL pads the range by two days", () => {
  const u = new URL(upstreamUrl("https://query1.finance.yahoo.com", "AUDUSD=X", "2026-01-10", "2026-01-20"));
  assert.equal(u.pathname, "/v8/finance/chart/AUDUSD%3DX");
  assert.equal(u.searchParams.get("period1"), String(utc("2026-01-08T00:00:00Z")));
  assert.equal(u.searchParams.get("period2"), String(utc("2026-01-22T00:00:00Z")));
  assert.equal(u.searchParams.get("interval"), "1d");
});

test("finished ranges cache for 30 days, live ones for 10 minutes", () => {
  assert.match(cacheControl(addDays(TODAY, -2), TODAY), /max-age=2592000/);
  assert.match(cacheControl(addDays(TODAY, -1), TODAY), /max-age=600/);
  assert.match(cacheControl(TODAY, TODAY), /max-age=600/);
});
