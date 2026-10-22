# Jag's AI Map — Cloudflare Pages deploy + private visitor analytics

The AI Map front end is 100% static (`/opt/ai-map/web`). This directory publishes it to
**Cloudflare Pages** (global edge CDN, no origin to overload) and adds a **private
visitor-analytics** dashboard backed by Cloudflare **D1**.

olaresone keeps doing the hourly pipeline (fetch → Qwen/Strata LLM entity extraction →
`web/data/*.json`). The LLM stays 100% internal; only the ~1 MB of generated static files
are pushed to Pages each hour. Nothing public ever calls the LLM.

```
olaresone (private)                    Cloudflare Pages + D1 (edge)
hourly: fetch + LLM + build.py   ──▶   static site served globally from the edge
  → web/data/*.json                    Functions: _middleware logs each view → D1
  → pages/build-deploy.sh (wrangler)   /api/visits (token-gated) ← /stats.html dashboard
```

## Files
- `functions/_middleware.js` — logs one row per page view to D1 (geo from `request.cf`).
- `functions/api/visits.js` — token-gated JSON aggregates for the dashboard.
- `public_extra/stats.html` — the private dashboard (served at `/stats.html`).
- `public_extra/_headers` — edge cache policy (assets long, data 60 s, api/stats no-store).
- `schema.sql` — D1 table.
- `wrangler.toml` — project + D1 binding (database_id filled by setup).
- `setup-pages.sh` — one-time: project, D1, schema, secrets, first deploy, custom domain.
- `build-deploy.sh` — assemble `dist/` and deploy (run hourly after the build).

## First-time setup
1. Create a scoped Cloudflare API token (My Profile → API Tokens → Create):
   - **Account → Cloudflare Pages → Edit**
   - **Account → D1 → Edit**
   - **Zone → DNS → Edit** (zone `jagular.io`) — for the custom domain
   Then: `export CLOUDFLARE_API_TOKEN=<token>`  (or run `npx wrangler login`).
2. Pick the stats password + salt:
   `export STATS_TOKEN='something-only-you-know'`  `export IP_SALT="$(openssl rand -hex 8)"`
3. Run: `./setup-pages.sh`
   This creates the project + D1, applies the schema, sets the secrets, deploys once, and
   attaches `ai-news.jagular.io` to Pages (Cloudflare creates/updates the DNS record).

## Going live / cutover
- `ai-news.jagular.io` currently points at the old **cloudflared tunnel → nginx**. Adding the
  Pages custom domain repoints DNS to Pages. Once the site looks good, retire the tunnel:
  `systemctl disable --now cloudflared`  (nginx site can stay or be removed).
- Turn on hourly auto-deploy by adding to `/opt/ai-map/.env`:  `AIMAP_DEPLOY=pages`
  and store the deploy token at `pages/.cf_deploy_token` (chmod 600) — a token with only
  **Pages: Edit** + **D1: Edit** is enough for the recurring deploy (no DNS scope needed).

## The analytics
- Visit the dashboard at `https://ai-news.jagular.io/stats.html` and enter `STATS_TOKEN`.
- Shows: page views + unique visitors, views/day, top countries/cities, pages, referrers,
  networks, and a recent-visits table — all from `request.cf` geo, no raw IPs stored
  (uniques use a per-day salted hash).
- Also available for free with zero code: **Cloudflare dashboard → Analytics** (per-country
  traffic, cache hit ratio, bandwidth) once the domain is proxied through Pages.
- For stronger protection of the dashboard than the shared key, put **Cloudflare Access**
  (Zero Trust) in front of `/stats*` scoped to your email.

## Caching / offload
Pages serves everything from Cloudflare's edge. `_headers` caches assets for a day and the
hourly `data/*.json` for 60 s; each deploy refreshes changed files automatically. With the
whole site < 1 MB, effectively all traffic is edge-served.

## Notes
- Hourly redeploys use `wrangler pages deploy` (direct upload); D1 data persists across
  deploys (it's a bound database, not part of the upload).
- If hourly redeploys ever feel heavy, switch the 3 JSON files to R2/KV updated by the
  pipeline and serve them via a Function — the static shell then rarely redeploys.
