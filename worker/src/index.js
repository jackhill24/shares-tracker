// Price relay for the shares tracker's web edition.
//
//   GET /chart?symbol=VAS.AX&start=YYYY-MM-DD&end=YYYY-MM-DD
//   → {symbol, currency, timezone, last, closes: {date: close}}
//
// Browsers can't read Yahoo's chart data from another website (CORS), so the
// web page asks this Worker instead; quotes, price history and FX pairs
// (AUDUSD=X) all use the one endpoint. It only ever sees ticker symbols and
// date ranges, never trades. It keeps nothing: no request logs (the tickers
// someone asks for say what they hold) and no shared cache. Each browser
// caches its own answers (Cache-Control) and the page keeps closes it has
// already fetched.
//
// The upstream request is an ordinary one that says who it is. If Yahoo stops
// answering it, switch provider here; don't disguise the relay as a browser.
import {
  HttpError, cacheControl, isAllowedOrigin, parseChart, parseQuery, todayUtc, upstreamUrl,
} from "./chart.js";

const YAHOO = "https://query1.finance.yahoo.com";
const USER_AGENT = "shares-tracker price relay (personal project)";

function reply(body, status, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...headers },
  });
}

export default {
  async fetch(request, env) {
    // Other websites' pages can't use the relay. Scripts can fake an Origin,
    // so the per-visitor rate limit below is the real guard.
    const origin = request.headers.get("Origin");
    const allowed = (env.ALLOWED_ORIGINS || "").split(",").map((s) => s.trim()).filter(Boolean);
    if (!isAllowedOrigin(origin, allowed)) return reply({ error: "origin not allowed" }, 403);
    const cors = { "Access-Control-Allow-Origin": origin, Vary: "Origin" };

    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: { ...cors, "Access-Control-Allow-Methods": "GET", "Access-Control-Max-Age": "86400" },
      });
    }
    if (request.method !== "GET") return reply({ error: "GET only" }, 405, { ...cors, Allow: "GET" });
    const url = new URL(request.url);
    if (url.pathname !== "/chart") return reply({ error: "not found" }, 404, cors);

    if (env.LIMITER) {
      const key = request.headers.get("CF-Connecting-IP") || "unknown";
      const { success } = await env.LIMITER.limit({ key });
      if (!success) {
        return reply({ error: "too many requests — wait a minute" }, 429, { ...cors, "Retry-After": "60" });
      }
    }

    try {
      const today = todayUtc();
      const { symbol, start, end } = parseQuery(url.searchParams, today);
      const upstream = await fetch(upstreamUrl(env.UPSTREAM || YAHOO, symbol, start, end), {
        headers: { "User-Agent": USER_AGENT, Accept: "application/json" },
      });
      if (upstream.status === 404) return reply({ error: `unknown symbol ${symbol}` }, 404, cors);
      if (!upstream.ok) return reply({ error: `price source answered ${upstream.status}` }, 502, cors);
      const chart = parseChart(await upstream.json(), start, end);
      return reply({ symbol, ...chart }, 200, { ...cors, "Cache-Control": cacheControl(end, today) });
    } catch (e) {
      if (e instanceof HttpError) return reply({ error: e.message }, e.status, cors);
      return reply({ error: "relay error" }, 502, cors);
    }
  },
};
