#!/bin/sh
# Hourly bot re-tagging of the ai-news visit stats (added 2026-11-24): runs backfill_bots.py in its default mode
# (only untagged rows), which also applies the delayed rules that cannot run at request time: probe-ip and
# noscript (main page loaded but the page script never pinged). Cron: /etc/cron.d/ai-map-bots.
cd /opt/ai-map/pages || exit 1
mkdir -p /opt/ai-map/logs
exec timeout 600 python3 backfill_bots.py >> /opt/ai-map/logs/bot-retag.log 2>&1
