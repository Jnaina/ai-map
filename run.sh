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
python3 pipeline/build.py "$@" 2>&1 | tee "logs/build-$(date +%Y%m%d-%H%M).log"
