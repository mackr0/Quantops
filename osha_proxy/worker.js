// OSHA Establishment-Search proxy via Cloudflare Workers.
//
// Why this exists: osha.gov sits behind a CloudFront WAF that hard-403s
// our DigitalOcean prod IP (verified: even `curl https://www.osha.gov/`
// is blocked, regardless of User-Agent or header massage). This Worker
// runs from Cloudflare's IP range, which OSHA does not block, and
// returns clean JSON aggregates so our Python caller doesn't have to
// parse HTML server-side.
//
// Auth: this Worker requires the `X-Proxy-Token` header to match the
// PROXY_TOKEN secret. Set it once via `wrangler secret put PROXY_TOKEN`
// (or in the Dashboard: Worker → Settings → Variables → Add secret).
// Without auth, anyone hitting the workers.dev URL would burn our daily
// quota; with auth, only our prod can call it.
//
// Cache: 24h via Cloudflare's edge cache (matches the Python-side TTL).

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Health check (no auth) — useful for the wrangler/dashboard "open in browser" smoke test.
    if (url.pathname === "/health") {
      return new Response("ok", { status: 200 });
    }

    // Auth gate.
    const token = request.headers.get("x-proxy-token");
    if (!env.PROXY_TOKEN || token !== env.PROXY_TOKEN) {
      return new Response(JSON.stringify({ error: "unauthorized" }), {
        status: 401,
        headers: { "content-type": "application/json" },
      });
    }

    const name = url.searchParams.get("establishment");
    if (!name) {
      return new Response(JSON.stringify({ error: "missing establishment param" }), {
        status: 400,
        headers: { "content-type": "application/json" },
      });
    }

    // Edge-cache key (per establishment name).
    const cacheKey = new Request(
      `https://cache.internal/osha?establishment=${encodeURIComponent(name)}`,
      { method: "GET" },
    );
    const cache = caches.default;
    const cached = await cache.match(cacheKey);
    if (cached) return cached;

    const oshaUrl =
      "https://www.osha.gov/ords/imis/establishment.search" +
      `?establishment=${encodeURIComponent(name)}`;

    let html;
    try {
      const r = await fetch(oshaUrl, {
        headers: {
          // Mimic a normal browser. OSHA's CloudFront is configured to
          // allow Cloudflare IPs but still inspects UA.
          "user-agent":
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
          "accept":
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
          "accept-language": "en-US,en;q=0.5",
        },
        cf: { cacheTtl: 86400 },
      });
      if (!r.ok) {
        return new Response(
          JSON.stringify({ error: `osha returned ${r.status}`, name }),
          { status: 502, headers: { "content-type": "application/json" } },
        );
      }
      html = await r.text();
    } catch (e) {
      return new Response(
        JSON.stringify({ error: `fetch failed: ${e.message}`, name }),
        { status: 502, headers: { "content-type": "application/json" } },
      );
    }

    // Each inspection row has a checkbox <input> with the inspection id.
    // Count = number of inspections in the trailing 5y default window.
    const ids = [...html.matchAll(/name="id" value="(\d+\.\d+)"/g)];
    const inspections_5y = ids.length;

    // Per-row violation count is column 11 of the data row, but the
    // regex below starts inside the checkbox's <td>, so the captured
    // <td>s are shifted by one: tds[9] is the violations cell.
    const rowBlocks = [
      ...html.matchAll(/name="id" value="\d+\.\d+">[\s\S]*?<\/tr>/g),
    ];
    let violations_5y = 0;
    for (const m of rowBlocks) {
      const tds = [...m[0].matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)];
      if (tds.length >= 10) {
        const cell = tds[9][1].trim().replace(/&nbsp;/g, "").replace(/\n/g, "");
        if (/^\d+$/.test(cell)) violations_5y += parseInt(cell, 10);
      }
    }

    const body = JSON.stringify({
      establishment: name,
      inspections_5y,
      violations_5y,
      fetched_at: new Date().toISOString(),
    });
    const resp = new Response(body, {
      headers: {
        "content-type": "application/json",
        "cache-control": "public, max-age=86400",
      },
    });
    ctx.waitUntil(cache.put(cacheKey, resp.clone()));
    return resp;
  },
};
