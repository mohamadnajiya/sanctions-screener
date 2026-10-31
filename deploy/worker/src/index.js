/**
 * screen-gw — Cloudflare Worker gateway for the GCC Sanctions & Terror-List Screener.
 *
 * Responsibilities (and nothing else):
 *   1. Rate limit: free tier per client, keyed tier per API key (Cloudflare Rate Limiting binding).
 *   2. API keys: X-API-Key or Authorization: Bearer <key>, looked up in KV. Free tools need no key.
 *   3. Body size cap and method filtering.
 *   4. Pass everything else straight to the origin (the Cloudflare Tunnel → 127.0.0.1:8785 / :8786).
 *
 * Privacy: this Worker logs nothing. No console.log, no analytics engine, no request bodies read except to
 * size-check. The rate-limit key for the free tier is a hash of the client address that never leaves
 * Cloudflare's rate-limit counter and is not stored anywhere.
 */

const JSON_HEADERS = { "content-type": "application/json; charset=utf-8" };

function json(status, body, extra = {}) {
  return new Response(JSON.stringify(body), { status, headers: { ...JSON_HEADERS, ...extra } });
}

function apiKeyOf(request) {
  const h = request.headers;
  const x = h.get("x-api-key");
  if (x) return x.trim();
  const auth = h.get("authorization") || "";
  if (auth.toLowerCase().startsWith("bearer ")) return auth.slice(7).trim();
  return null;
}

async function sha256Hex(s) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function toolFromPath(pathname) {
  const m = pathname.match(/^\/v1\/tools\/([a-z_]+)$/);
  return m ? m[1] : null;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    // --- method / size hygiene
    if (!["GET", "POST", "DELETE", "OPTIONS", "HEAD"].includes(request.method)) {
      return json(405, { error: "method not allowed" });
    }
    const len = Number(request.headers.get("content-length") || "0");
    if (len > Number(env.MAX_BODY_BYTES || "262144")) {
      return json(413, { error: "request body too large" });
    }

    // --- which surface is this?
    const isMcp = path === "/mcp" || path.startsWith("/mcp/");
    const tool = toolFromPath(path);

    // Meta routes: light free-tier limit only.
    // Tool routes: free tools pass with the free limit; paid tools need a key.
    const key = apiKeyOf(request);
    let plan = null;
    if (key) {
      const rec = await env.ORACLE_KEYS.get(key, { type: "json" });
      if (!rec) return json(401, { error: "unknown API key" });
      plan = rec.plan || "paid";
    }

    const freeTools = (env.FREE_TOOLS || "").split(",").map((s) => s.trim()).filter(Boolean);
    if (tool && !freeTools.includes(tool) && !plan) {
      return json(402, {
        error: "API key required for this tool",
        free_tools: freeTools,
        how: "send X-API-Key: <key> (or Authorization: Bearer <key>)",
      });
    }
    if (isMcp && env.REQUIRE_KEY_FOR_MCP === "true" && !plan) {
      return json(401, { error: "API key required", how: "send X-API-Key: <key> (or Authorization: Bearer <key>)" });
    }

    // --- rate limiting
    let rl, rlKey;
    if (plan) {
      rl = env.RL_KEYED;
      rlKey = await sha256Hex("k:" + key);
    } else {
      rl = env.RL_FREE;
      const ip = request.headers.get("cf-connecting-ip") || "0.0.0.0";
      rlKey = await sha256Hex("ip:" + ip); // hashed; never stored, never logged
    }
    if (rl) {
      const { success } = await rl.limit({ key: rlKey });
      if (!success) {
        return json(429, { error: "rate limit exceeded" }, { "retry-after": "10" });
      }
    }

    // --- pass through to the tunnel origin. Strip the key so the origin never sees it.
    const headers = new Headers(request.headers);
    headers.delete("x-api-key");
    headers.delete("authorization");
    headers.set("x-oracle-plan", plan || "free");
    const upstream = new Request(request, { headers });
    const resp = await fetch(upstream);
    const out = new Response(resp.body, resp);
    out.headers.set("x-oracle-gateway", "screen-gw");
    out.headers.set("cache-control", "no-store");
    return out;
  },
};
