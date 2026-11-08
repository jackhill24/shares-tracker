// node --test web/test/*.test.mjs
// prices.js against a fake relay and an in-memory store: what gets fetched
// on a first and a return visit, and what happens when the relay fails.
import assert from "node:assert/strict";
import { test } from "node:test";

import { FRESH_MS, PriceError, addDays, fetchAll, plan } from "../prices.js";

const DAY = 24 * 3600 * 1000;
const NOW = Date.parse("2026-11-08T03:00:00Z");

function memoryStore(entries = []) {
  const m = new Map(entries.map((e) => [e.symbol, e]));
  return { get: async (s) => m.get(s) ?? null, put: async (e) => { m.set(e.symbol, e); }, m };
}

// A relay with a close on every day of any range (value = day of month),
// recording each call. `fail(symbol, start)` can return a status to send.
function fakeRelay({ fail = () => null } = {}) {
  const calls = [];
  const fetch = async (url) => {
    const q = new URL(url).searchParams;
    const [symbol, start, end] = ["symbol", "start", "end"].map((k) => q.get(k));
    calls.push(`${symbol} ${start}..${end}`);
    const status = fail(symbol, start, calls.length);
    if (status === "network") throw new TypeError("Failed to fetch");
    if (status) {
      return { ok: false, status, headers: new Map([["Retry-After", "0"]]), json: async () => ({ error: `nope ${status}` }) };
    }
    const closes = {};
    for (let d = start; d <= end; d = addDays(d, 1)) closes[d] = Number(d.slice(8));
    return { ok: true, status: 200, json: async () => ({ symbol, currency: "AUD", last: 99, closes }) };
  };
  return { fetch, calls };
}

const REQ = { kind: "price", key: "VAS.AX", symbol: "VAS.AX", start: "2026-01-01", end: "2026-11-08" };
const opts = (relay, store, extra = {}) => ({
  relay: "https://relay.test/", store, fetch: relay.fetch, now: () => NOW, retryMs: 0, ...extra,
});

test("first visit fetches the whole range and saves it", async () => {
  const relay = fakeRelay();
  const store = memoryStore();
  const { fetched, warnings } = await fetchAll([REQ], opts(relay, store));
  assert.deepEqual(relay.calls, ["VAS.AX 2026-01-01..2026-11-08"]);
  assert.equal(fetched[0].last, 99);
  assert.equal(fetched[0].currency, "AUD");
  assert.equal(Object.keys(fetched[0].closes).length, 312);
  assert.equal(fetched[0].key, "VAS.AX");
  assert.deepEqual(warnings, []);
  const saved = store.m.get("VAS.AX");
  assert.equal(saved.from, "2026-01-01");
  assert.equal(saved.to, "2026-11-08");
  assert.equal(saved.fetchedAt, NOW);
});

test("a return visit fetches only the last week again", async () => {
  const store = memoryStore();
  await fetchAll([REQ], opts(fakeRelay(), store));
  const later = fakeRelay();
  const req = { ...REQ, end: "2026-11-20" };
  const { fetched } = await fetchAll([req], opts(later, store, { now: () => NOW + 12 * DAY }));
  assert.deepEqual(later.calls, ["VAS.AX 2026-11-01..2026-11-20"]);
  assert.equal(Object.keys(fetched[0].closes).length, 324);
  assert.equal(store.m.get("VAS.AX").to, "2026-11-20");
});

test("a rebuild within ten minutes fetches nothing", async () => {
  const store = memoryStore();
  await fetchAll([REQ], opts(fakeRelay(), store));
  const again = fakeRelay();
  await fetchAll([REQ], opts(again, store, { now: () => NOW + FRESH_MS - 1 }));
  assert.deepEqual(again.calls, []);
});

test("an earlier start fetches just the missing head", async () => {
  const store = memoryStore();
  await fetchAll([REQ], opts(fakeRelay(), store));
  const relay = fakeRelay();
  await fetchAll([{ ...REQ, start: "2025-06-01" }], opts(relay, store, { now: () => NOW + 1000 }));
  assert.deepEqual(relay.calls, ["VAS.AX 2025-06-01..2025-12-31"]);
  assert.equal(store.m.get("VAS.AX").from, "2025-06-01");
});

test("a refetched range replaces what was saved inside it", () => {
  const saved = { symbol: "X", from: "2026-01-01", to: "2026-11-05", closes: {}, fetchedAt: NOW - DAY };
  assert.deepEqual(plan({ ...REQ, symbol: "X" }, saved, NOW), [
    { start: "2026-10-29", end: "2026-11-08", tail: true },
  ]);
  // Saved closes that end long before the request starts are no help.
  const old = { ...saved, from: "2020-01-01", to: "2020-02-01" };
  assert.deepEqual(plan(REQ, old, NOW), [{ start: REQ.start, end: REQ.end, tail: true }]);
});

test("an unknown symbol warns and carries on", async () => {
  const relay = fakeRelay({ fail: (s) => (s === "NOPE" ? 404 : null) });
  const { fetched, warnings } = await fetchAll(
    [REQ, { ...REQ, key: "NOPE", symbol: "NOPE" }], opts(relay, memoryStore()),
  );
  assert.deepEqual(fetched[1].closes, {});
  assert.equal(fetched[1].last, null);
  assert.match(warnings[0], /No prices found for NOPE/);
});

test("a failed refresh falls back to saved prices, with a warning", async () => {
  const store = memoryStore();
  await fetchAll([REQ], opts(fakeRelay(), store));
  const down = fakeRelay({ fail: () => 502 });
  const { fetched, warnings } = await fetchAll([REQ], opts(down, store, { now: () => NOW + DAY }));
  assert.equal(down.calls.length, 2);  // retried once
  assert.equal(fetched[0].last, 99);
  assert.equal(Object.keys(fetched[0].closes).length, 312);
  assert.match(warnings[0], /Couldn't update VAS.AX \(nope 502\)/);
});

test("with nothing saved, a failure is an error naming the symbol", async () => {
  const down = fakeRelay({ fail: () => "network" });
  await assert.rejects(
    fetchAll([REQ], opts(down, memoryStore())),
    (e) => e instanceof PriceError && /VAS.AX: couldn't reach the price service/.test(e.message),
  );
});

test("a 429 waits and retries", async () => {
  let waited = null;
  const relay = fakeRelay({ fail: (s, start, n) => (n === 1 ? 429 : null) });
  const { fetched } = await fetchAll([REQ], opts(relay, memoryStore(), { onWait: (ms) => { waited = ms; } }));
  assert.equal(relay.calls.length, 2);
  assert.equal(waited, 0);  // the fake relay's Retry-After: 0
  assert.equal(fetched[0].last, 99);
});

test("progress counts every request, a few at a time", async () => {
  const relay = fakeRelay();
  const reqs = Array.from({ length: 9 }, (_, i) => ({ ...REQ, key: `T${i}`, symbol: `T${i}` }));
  const seen = [];
  await fetchAll(reqs, opts(relay, memoryStore(), { concurrency: 3, onProgress: (d, t) => seen.push(`${d}/${t}`) }));
  assert.equal(relay.calls.length, 9);
  assert.equal(seen.at(0), "0/9");
  assert.equal(seen.at(-1), "9/9");
});
