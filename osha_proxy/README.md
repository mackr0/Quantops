# OSHA Establishment-Search proxy (Cloudflare Worker)

Bypasses OSHA's CloudFront WAF block on our DigitalOcean prod IP. Worker
runs from Cloudflare's IP range (not blocked) and returns clean JSON
aggregates so the Python caller doesn't have to parse HTML server-side.

## Deploy (Dashboard path — fastest, no CLI install)

1. Cloudflare dashboard → **Workers & Pages** → **Create application** → **Create Worker**
2. Name: `osha-proxy` (worker URL becomes `https://osha-proxy.<your-handle>.workers.dev`)
3. Click **Deploy** to create the starter, then **Edit code**
4. Replace the editor contents with `worker.js` from this directory, click **Deploy**
5. Generate a random 32-char token (e.g. `openssl rand -hex 32`)
6. Worker → **Settings** → **Variables and Secrets** → **Add variable**
   - Type: **Secret**
   - Name: `PROXY_TOKEN`
   - Value: the random string you just generated
7. Smoke test from anywhere:
   ```
   curl https://osha-proxy.<your-handle>.workers.dev/health           # → "ok"
   curl -H "X-Proxy-Token: <token>" \
        "https://osha-proxy.<your-handle>.workers.dev/?establishment=EXXON"
   # → {"establishment":"EXXON","inspections_5y":17,"violations_5y":10,...}
   ```
8. Tell me the Worker URL + the token; I'll wire `OSHA_PROXY_URL` and
   `OSHA_PROXY_TOKEN` into prod `.env` and restore the OSHA scrape in
   `altdata_tier3.py`.

## Deploy (CLI path — if you'd rather wrangler)

```sh
npm install -g wrangler
cd osha_proxy
wrangler login            # opens browser
wrangler secret put PROXY_TOKEN   # paste the random token
wrangler deploy
```

## Free-tier headroom

100,000 requests/day on the free plan. Worst-case usage = 25 industrial
tickers × ~10 prompt-cycles/day × 1 request/ticker = 250 req/day → 0.25%
of the quota. Edge-cached for 24h, so most calls return from cache
without re-hitting OSHA at all.
