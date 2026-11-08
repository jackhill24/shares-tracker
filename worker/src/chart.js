// The price relay's rules, kept apart from the request handler (index.js) so
// they can be tested directly in Node: which requests are accepted, the
// upstream URL, how Yahoo's chart answer becomes {date: close}, and how long a
// browser may keep the result.
//
// The closes must match what the desktop tracker gets from yfinance with
// auto_adjust=False, because the dashboard maths is shared: the "Close"
// series (indicators.quote[0].close, not adjclose), dated in the exchange's
// own time zone.

export const SYMBOL_RE = /^[A-Z0-9.^=-]{1,15}$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
// Longest history one request may ask for, and the earliest start accepted.
export const MAX_SPAN_DAYS = 366 * 40;
export const EARLIEST = "1970-01-01";
// A range that ended before yesterday (UTC) can't change any more: every
// exchange's day is over by then, whatever its time zone.
export const FINAL_MAX_AGE = 30 * 24 * 3600;
export const LIVE_MAX_AGE = 10 * 60;

const LOCALHOST_RE = /^http:\/\/(localhost|127\.0\.0\.1)(:\d{1,5})?$/;

export class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export function addDays(iso, n) {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

export function todayUtc(now = new Date()) {
  return now.toISOString().slice(0, 10);
}

function isRealDate(s) {
  return DATE_RE.test(s) && addDays(s, 0) === s;  // rejects 2026-02-30
}

/** Allowed: an exact entry of ALLOWED_ORIGINS, or http://localhost on any port. */
export function isAllowedOrigin(origin, allowed) {
  return Boolean(origin) && (allowed.includes(origin) || LOCALHOST_RE.test(origin));
}

/** ?symbol=&start=&end= → {symbol, start, end}, or HttpError(400). */
export function parseQuery(params, today) {
  const symbol = (params.get("symbol") || "").trim();
  const start = params.get("start") || "";
  const end = params.get("end") || "";
  if (!SYMBOL_RE.test(symbol)) throw new HttpError(400, "symbol must be 1–15 of A–Z 0–9 . ^ = -");
  if (!isRealDate(start) || !isRealDate(end)) throw new HttpError(400, "start and end must be YYYY-MM-DD dates");
  if (start > end) throw new HttpError(400, "start is after end");
  if (start < EARLIEST) throw new HttpError(400, `start must be ${EARLIEST} or later`);
  if (end > addDays(today, 1)) throw new HttpError(400, "end is in the future");
  if (addDays(start, MAX_SPAN_DAYS) < end) throw new HttpError(400, `at most ${MAX_SPAN_DAYS} days per request`);
  return { symbol, start, end };
}

/** Yahoo's daily chart over [start, end], padded by two days either side so
 *  every exchange's local day is covered; parseChart trims to the range. */
export function upstreamUrl(base, symbol, start, end) {
  const secs = (iso) => Date.parse(`${iso}T00:00:00Z`) / 1000;
  const q = new URLSearchParams({
    period1: String(secs(addDays(start, -2))),
    period2: String(secs(addDays(end, 2))),
    interval: "1d",
    includePrePost: "false",
  });
  return `${base}/v8/finance/chart/${encodeURIComponent(symbol)}?${q}`;
}

function dateIn(timeZone) {
  const fmt = new Intl.DateTimeFormat("en-CA", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit",
  });
  return (seconds) => {
    const p = Object.fromEntries(
      fmt.formatToParts(new Date(seconds * 1000)).map((x) => [x.type, x.value]),
    );
    return `${p.year}-${p.month}-${p.day}`;
  };
}

/** Yahoo chart JSON → {currency, timezone, last, closes} for [start, end].
 *
 *  Bars are dated in the exchange's time zone (meta.exchangeTimezoneName), as
 *  yfinance does. Not by shifting with meta.gmtoffset: that's today's offset,
 *  so it misdates bars from the other side of a daylight-saving change (a
 *  London FX bar stamped 00:00 BST is 23:00 UTC the day before). */
export function parseChart(json, start, end) {
  const result = json?.chart?.result?.[0];
  if (!result) {
    const why = json?.chart?.error?.description || "no data";
    throw new HttpError(404, `price source has no chart: ${why}`);
  }
  const meta = result.meta || {};
  const timezone = meta.exchangeTimezoneName || "UTC";
  const toDate = dateIn(timezone);
  const stamps = result.timestamp || [];  // absent for a range with no trading
  const close = result.indicators?.quote?.[0]?.close || [];
  const closes = {};
  stamps.forEach((t, i) => {
    const c = close[i];
    if (typeof c !== "number" || !Number.isFinite(c)) return;
    const d = toDate(t);
    // Yahoo can send today's live bar as an extra row on the same date; the
    // later row wins, as yfinance merges it.
    if (d >= start && d <= end) closes[d] = c;
  });
  const last = meta.regularMarketPrice;
  return {
    currency: meta.currency ?? null,
    timezone,
    last: typeof last === "number" && Number.isFinite(last) ? last : null,
    closes,
  };
}

/** Cache-Control for a range: long for one that's over, short if it includes today. */
export function cacheControl(end, today) {
  return end < addDays(today, -1)
    ? `public, max-age=${FINAL_MAX_AGE}, immutable`
    : `public, max-age=${LIVE_MAX_AGE}`;
}
