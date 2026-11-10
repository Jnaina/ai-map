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
# wrangler's usage-metrics fetch throws "TypeError: fetch failed" on a transient network blip and
# aborts the whole deploy; disabling it removes that failure mode (and the noise).
export WRANGLER_SEND_METRICS=false
# Deploy with retries: the Cloudflare API call still fails intermittently on a network blip, and a
# retry almost always succeeds. Exit non-zero only if EVERY attempt fails, so run.sh's exit-code
# check does not advance the deploy marker (data/.last-deploy.sig) on a genuine failure -> it retries
# next run. The `if` condition is exempt from `set -e`, so a failed attempt doesn't abort the script.
attempts=5
for n in $(seq 1 "$attempts"); do
  if npx --yes wrangler pages deploy \
       --project-name="$PROJECT" \
       --branch=main \
       --commit-dirty=true; then
    exit 0
  fi
  echo "wrangler deploy attempt $n/$attempts failed; retrying in 5s..." >&2
  sleep 5
done
echo "wrangler deploy failed after $attempts attempts" >&2
exit 1
