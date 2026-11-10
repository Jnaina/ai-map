#!/bin/bash
# Weekly emerging-entities digest -> ntfy. Run by ai-map-emerging.timer (Mondays).
# Reads data/llm-cache.json + data/articles.db (no network beyond ntfy); changes nothing.
cd "$(dirname "$0")"
# Local settings (AIMAP_NTFY_URL / AIMAP_ALERTS), same as run.sh
[ -f .env ] && set -a && . ./.env && set +a
exec python3 pipeline/emerging_entities.py --days 7 --min-stories 3 --top 40 --ntfy
