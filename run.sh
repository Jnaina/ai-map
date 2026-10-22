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

# Feed-failure notifications: ntfy push whenever a source is down/missing, re-sent each run
# as an hourly reminder while it stays broken; sends nothing when all feeds are healthy.
# Reads the health report above. Best-effort. Disable with AIMAP_ALERTS=off.
python3 pipeline/feed_alert.py 2>&1 | tee -a "$LOGFILE" || true

# Log rotation: keep ~14 days of per-run build/deploy logs.
find logs -name 'build-*.log' -mtime +14 -delete 2>/dev/null || true
find logs -name 'deploy-*.log' -mtime +14 -delete 2>/dev/null || true

# Optional: push the freshly built static site to Cloudflare Pages (set AIMAP_DEPLOY=pages
# in .env once the Pages project is set up). Best-effort: a deploy failure never fails the build.
# Deploy-on-change: on the fast (~2 min) cadence most runs produce no new content (Hacker News is
# the only thing polled every run), so we only deploy when the story-content signature written by
# build.py changes. This keeps Cloudflare deploys proportional to real changes, not to the timer.
if [ "${AIMAP_DEPLOY:-}" = "pages" ]; then
  SIG="$(cat data/content.sig 2>/dev/null)"
  LAST="$(cat data/.last-deploy.sig 2>/dev/null)"
  if [ -n "$SIG" ] && [ "$SIG" = "$LAST" ]; then
    echo "No content change since last deploy; skipping Cloudflare Pages deploy."
  else
    echo "Content changed; deploying to Cloudflare Pages..."
    ./pages/build-deploy.sh 2>&1 | tee "logs/deploy-$(date +%Y%m%d-%H%M).log"
    if [ "${PIPESTATUS[0]}" -eq 0 ]; then
      echo "$SIG" > data/.last-deploy.sig   # only advance the marker on a successful deploy
    else
      echo "Pages deploy failed (continuing; will retry next run)"
    fi
  fi
fi
