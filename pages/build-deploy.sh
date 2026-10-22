#!/usr/bin/env bash
# Assemble the Pages deploy directory (static site + Functions + stats page + headers)
# and push it to Cloudflare Pages. Run after each hourly pipeline build so the freshly
# generated web/data/*.json goes live. Idempotent; safe to run any time.
#
# Auth: needs either `npx wrangler login` done once, or CLOUDFLARE_API_TOKEN exported
# (a scoped token: Account > Cloudflare Pages: Edit, Account > D1: Edit).
set -euo pipefail

ROOT=/opt/ai-map
PAGES="$ROOT/pages"
DIST="$PAGES/dist"
PROJECT=ai-map

# read a stored deploy token if present (chmod 600), unless already in the environment
if [ -z "${CLOUDFLARE_API_TOKEN:-}" ] && [ -f "$PAGES/.cf_deploy_token" ]; then
  CLOUDFLARE_API_TOKEN="$(cat "$PAGES/.cf_deploy_token")"; export CLOUDFLARE_API_TOKEN
fi

# 1. assemble dist/ = the static site + edge Functions + private stats page + cache headers
rm -rf "$DIST"; mkdir -p "$DIST"
cp -a "$ROOT"/web/. "$DIST"/
cp -a "$PAGES"/functions "$DIST"/functions
cp -a "$PAGES"/public_extra/stats.html "$DIST"/stats.html
cp -a "$PAGES"/public_extra/_headers "$DIST"/_headers

# 2. deploy (production branch -> updates the custom domain).
# Output dir comes from wrangler.toml (pages_build_output_dir=dist), so no positional arg.
cd "$PAGES"
export CLOUDFLARE_ACCOUNT_ID="${CLOUDFLARE_ACCOUNT_ID:-4d92d2ca307430c0f6706269f613fb47}"
npx --yes wrangler pages deploy \
  --project-name="$PROJECT" \
  --branch=main \
  --commit-dirty=true
