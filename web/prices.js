// Fetches what market_requirements lists through the price relay, keeping
// closes in the browser so a return visit only asks for the days since.
//
// Each request is {kind, key, symbol, start, end}. The answer, ready for
// seed_cache, is the request plus {closes, last, currency}: closes inside
// [start, end] as the relay sends them (FX as Yahoo quotes the AUDXXX=X pair;
// seed_cache inverts it).
//
// Saved per symbol: {symbol, from, to, closes, last, currency, fetchedAt},
// where closes are complete over [from, to]. Coverage only grows by ranges
// that came back successfully, so a saved entry never claims days it hasn't
// got. The last few days are always fetched again (today's close may have
// been a live price), unless the entry was refreshed in the last 10 minutes.

export const TAIL_DAYS = 7;
export const FRESH_MS = 10 * 60 * 1000;

export class PriceError extends Error {}

export function addDays(iso, n) {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

const maxDate = (a, b) => (a > b ? a : b);
const minDate = (a, b) => (a < b ? a : b);

function within(closes, start, end) {
  const out = {};
  for (const [d, c] of Object.entries(closes)) if (d >= start && d <= end) out[d] = c;
  return out;
}

/** Saved closes that don't touch the request's range are no help. */
function usable(r, saved) {
  return Boolean(saved) && saved.to >= addDays(r.start, -1) && saved.from <= addDays(r.end, 1);
}

/** The ranges to ask the relay for, given what's saved. `tail` marks the one
 *  that reaches `end` (it carries the live price). */
export function plan(r, saved, now) {
  if (!usable(r, saved)) return [{ start: r.start, end: r.end, tail: true }];
  const ranges = [];
  if (r.start < saved.from) ranges.push({ start: r.start, end: addDays(saved.from, -1), tail: false });
  const fresh = saved.to >= r.end && now - saved.fetchedAt < FRESH_MS;
  if (!fresh) {
    ranges.push({ start: maxDate(r.start, addDays(saved.to, -TAIL_DAYS)), end: r.end, tail: true });
  }
  return ranges;
}

/** Fold fetched ranges into the saved entry. A range's answer replaces every
 *  saved close inside it (a stale live bar disappears with it). */
export function merge(r, saved, got, now) {
  const entry = usable(r, saved)
    ? { ...saved, closes: { ...saved.closes } }
    : { symbol: r.symbol, from: r.start, to: r.end, closes: {}, last: null, currency: null, fetchedAt: now };
  for (const { range, body } of got) {
    for (const d of Object.keys(entry.closes)) {
      if (d >= range.start && d <= range.end) delete entry.closes[d];
    }
    Object.assign(entry.closes, body.closes || {});
    entry.from = minDate(entry.from, range.start);
    entry.to = maxDate(entry.to, range.end);
    if (range.tail) entry.last = body.last ?? null;
    if (range.tail) entry.fetchedAt = now;
    entry.currency = body.currency ?? entry.currency;
  }
  return entry;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** GET one range from the relay → its JSON body. Retries once: after the
 *  relay's Retry-After on 429, or shortly after a network error or 5xx. */
async function getRange(ctx, symbol, range, onWait) {
  const url = `${ctx.relay}/chart?${new URLSearchParams({ symbol, start: range.start, end: range.end })}`;
  for (let attempt = 0; ; attempt++) {
    let res;
    try {
      res = await ctx.fetch(url);
    } catch (e) {
      if (attempt === 0) { await sleep(ctx.retryMs); continue; }
      throw new PriceError("couldn't reach the price service — check your connection");
    }
    if (res.ok) return res.json();
    const body = await res.json().catch(() => ({}));
    if (res.status === 404) return null;  // the relay doesn't know this symbol
    if (attempt === 0 && (res.status === 429 || res.status >= 500)) {
      const after = res.headers.get("Retry-After");
      const wait = res.status === 429
        ? (after == null || after === "" || isNaN(after) ? 30 : Math.min(60, Number(after))) * 1000
        : ctx.retryMs;
      if (res.status === 429) onWait?.(wait);
      await sleep(wait);
      continue;
    }
    throw new PriceError(body.error || `the price service answered ${res.status}`);
  }
}

function savedOn(entry) {
  return new Date(entry.fetchedAt).toLocaleDateString("en-AU", { day: "numeric", month: "short", year: "numeric" });
}

async function fetchOne(r, ctx, warnings, onWait) {
  const saved = await ctx.store.get(r.symbol);
  const ranges = plan(r, saved, ctx.now());
  const got = [];
  try {
    for (const range of ranges) {
      const body = await getRange(ctx, r.symbol, range, onWait);
      if (body === null) {
        warnings.push(`No prices found for ${r.symbol}. Check its code and exchange in your CSV.`);
        return { ...r, closes: {}, last: null, currency: null };
      }
      got.push({ range, body });
    }
  } catch (e) {
    if (!(e instanceof PriceError) || !saved) throw new PriceError(`${r.symbol}: ${e.message}`);
    warnings.push(`Couldn't update ${r.symbol} (${e.message}), so it uses prices saved on ${savedOn(saved)}.`);
    return { ...r, closes: within(saved.closes, r.start, r.end), last: saved.last, currency: saved.currency };
  }
  const entry = got.length ? merge(r, saved, got, ctx.now()) : saved;
  if (got.length) await ctx.store.put(entry);
  return { ...r, closes: within(entry.closes, r.start, r.end), last: entry.last, currency: entry.currency };
}

/** Fetch every request, `concurrency` at a time → {fetched, warnings}.
 *  Throws PriceError if a symbol has neither an answer nor saved prices. */
export async function fetchAll(requests, {
  relay, store, fetch: fetchImpl = (...a) => globalThis.fetch(...a), concurrency = 6,
  onProgress, onWait, now = () => Date.now(), retryMs = 1500,
}) {
  const ctx = { relay: relay.replace(/\/$/, ""), store, fetch: fetchImpl, now, retryMs };
  const fetched = new Array(requests.length);
  const warnings = [];
  const failures = [];
  let next = 0, done = 0;
  onProgress?.(0, requests.length);
  async function lane() {
    while (next < requests.length) {
      const i = next++;
      try {
        fetched[i] = await fetchOne(requests[i], ctx, warnings, onWait);
      } catch (e) {
        failures.push(e.message);
      }
      onProgress?.(++done, requests.length);
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, requests.length) }, lane));
  if (failures.length) {
    throw new PriceError(`Couldn't fetch prices for ${failures.length} of ${requests.length} items:\n${failures.join("\n")}`);
  }
  return { fetched, warnings };
}
