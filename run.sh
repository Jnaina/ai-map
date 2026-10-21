#!/bin/bash
# Rebuild the AI Map data (fetch news -> entities -> heat -> web/data/data.json)
cd "$(dirname "$0")"
# Local settings (e.g. AIMAP_LLM_* on the server)
[ -f .env ] && set -a && . ./.env && set +a
# Use your GitHub CLI login for a higher GitHub API rate limit, if available
[ -z "$GITHUB_TOKEN" ] && command -v gh >/dev/null && export GITHUB_TOKEN="$(gh auth token 2>/dev/null)"

# Optional LLM entity extraction. Defaults to the "strata" provider in your pi config
# (Qwen3.8-Flash); set AIMAP_LLM_BASE_URL / AIMAP_LLM_MODEL / AIMAP_LLM_API_KEY to override,
# or AIMAP_LLM=off to disable.
if [ "${AIMAP_LLM:-on}" != "off" ] && [ -z "$AIMAP_LLM_BASE_URL" ] && [ -f "$HOME/.pi/agent/models.json" ]; then
  eval "$(python3 pipeline/llm_env.py)"
fi
if [ -n "$AIMAP_LLM_BASE_URL" ] && ! curl -s --max-time 6 -o /dev/null -H "Authorization: Bearer ${AIMAP_LLM_API_KEY:-none}" "$AIMAP_LLM_BASE_URL/models"; then
  echo "LLM endpoint $AIMAP_LLM_BASE_URL unreachable; continuing with dictionary-only extraction"
  unset AIMAP_LLM_BASE_URL
fi
mkdir -p logs
LOGFILE="logs/build-$(date +%Y%m%d-%H%M).log"
python3 pipeline/build.py "$@" 2>&1 | tee "$LOGFILE"

# Feed-health report: summarise feed intake/errors across the build logs (incl. this run)
# into data/feed_health.json, and print any feeds needing attention. Best-effort.
python3 pipeline/feed_health.py 2>&1 | tee -a "$LOGFILE" || true

# Feed-failure notifications: ntfy push ONLY when a source newly breaks (down/missing);
# sends nothing otherwise. Reads the health report above; de-dupes via
# data/feed_alert_state.json so a feed isn't re-alerted hourly while it stays down.
# Best-effort. Disable with AIMAP_ALERTS=off.
python3 pipeline/feed_alert.py 2>&1 | tee -a "$LOGFILE" || true

# Log rotation: keep ~14 days of per-run build/deploy logs.
find logs -name 'build-*.log' -mtime +14 -delete 2>/dev/null || true
find logs -name 'deploy-*.log' -mtime +14 -delete 2>/dev/null || true

# Optional: push the freshly built static site to Cloudflare Pages (set AIMAP_DEPLOY=pages
# in .env once the Pages project is set up). Best-effort: a deploy failure never fails the build.
if [ "${AIMAP_DEPLOY:-}" = "pages" ]; then
  echo "Deploying to Cloudflare Pages..."
  ./pages/build-deploy.sh 2>&1 | tee "logs/deploy-$(date +%Y%m%d-%H%M).log" || echo "Pages deploy failed (continuing)"
fi
