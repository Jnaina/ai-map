#!/usr/bin/env python3
"""Feed-failure notifications via ntfy.

Reads data/feed_health.json (produced by feed_health.py) and sends a push notification
when a news source actually breaks, so a dead feed doesn't go unnoticed for days.

What counts as "broken": status "down" (>=3 consecutive fetch failures, i.e. failing for
~3h straight) or "missing" (configured but never shows up in the logs). Deliberately NOT
"silent" (a low-frequency blog that just hasn't posted) or "failing" (a single transient
blip such as a one-off timeout) -- those would be noise.

State is kept in data/feed_alert_state.json so we only notify on CHANGES: a feed that newly
broke, or one that recovered. A steady set of broken feeds is not re-sent every hour.

    python3 pipeline/feed_alert.py              # normal: alert on changes, update state
    python3 pipeline/feed_alert.py --dry-run    # print what it would send; don't send/persist
    python3 pipeline/feed_alert.py --force       # (re)send the current broken set regardless of state
    python3 pipeline/feed_alert.py --test        # send a test notification and exit

Config (env):
    AIMAP_NTFY_URL   ntfy topic URL (default https://ntfy.sh/jag_alerts)
    AIMAP_ALERTS     set to "off" to disable sending entirely

It is invoked automatically at the end of run.sh (best-effort; never fails the build).
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEALTH = os.path.join(ROOT, "data", "feed_health.json")
STATE = os.path.join(ROOT, "data", "feed_alert_state.json")
NTFY_URL = os.environ.get("AIMAP_NTFY_URL", "https://ntfy.sh/jag_alerts")

# Statuses that mean a feed is genuinely broken (vs merely dormant/transient).
BROKEN = {"down", "missing"}


def ntfy(title, body, priority="default", tags=""):
    """POST a notification to the ntfy topic. Best-effort: never raises."""
    if os.environ.get("AIMAP_ALERTS") == "off":
        print(f"[feed-alert] AIMAP_ALERTS=off; would send: {title!r}")
        return False
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    req = urllib.request.Request(NTFY_URL, data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:  # noqa: BLE001 - notifications must never break the build
        print(f"[feed-alert] ntfy send failed: {e}")
        return False


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def broken_from(report):
    """{feed_name: problem_dict} for feeds currently in a BROKEN status."""
    out = {}
    for p in report.get("problems", []):
        if p.get("status") in BROKEN:
            out[p["feed"]] = p
    return out


def describe(feed, p):
    status = p.get("status", "?").upper()
    err = p.get("last_error")
    last = p.get("last_story")
    bits = [f"{feed} [{status}]"]
    if err:
        bits.append(err[:120])
    elif last:
        bits.append(f"last story {last[:10]}")
    return " - ".join(bits)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print what would be sent; don't send or persist")
    ap.add_argument("--force", action="store_true", help="send the current broken set even if unchanged")
    ap.add_argument("--test", action="store_true", help="send a test notification and exit")
    ap.add_argument("--health", default=HEALTH, help="path to feed_health.json")
    ap.add_argument("--state", default=STATE, help="path to the alert state file")
    a = ap.parse_args()

    if a.test:
        ok = ntfy("AI Map: feed alerts test",
                  "This is a test from feed_alert.py. If you see this, ntfy delivery works.",
                  priority="default", tags="satellite")
        print("[feed-alert] test notification " + ("sent" if ok else "NOT sent"))
        return

    report = load_json(a.health, None)
    if report is None:
        print(f"[feed-alert] no health report at {a.health}; nothing to do")
        return

    current = broken_from(report)
    state = load_json(a.state, {})
    prev = set(state.get("alerted", []))

    newly_broken = sorted(set(current) - prev)
    recovered = sorted(prev - set(current))
    still_broken = sorted(set(current) & prev)

    if not newly_broken and not recovered and not a.force:
        n = len(current)
        print(f"[feed-alert] no change ({n} feed(s) still broken)" if n else "[feed-alert] all feeds healthy")
        return

    sent_any = False

    # Failure alert: list the feeds that are down right now (new ones first).
    if newly_broken or (a.force and current):
        lines = [describe(f, current[f]) for f in newly_broken]
        lines += [describe(f, current[f]) for f in still_broken if f not in newly_broken]
        n = len(current)
        title = f"AI Map: {len(newly_broken) or n} news feed(s) down"
        body = "\n".join(lines) or "(see feed_health.json)"
        if a.dry_run:
            print(f"[feed-alert] DRY-RUN failure alert:\n  Title: {title}\n  " + "\n  ".join(lines))
        else:
            sent_any |= ntfy(title, body, priority="high", tags="warning")

    # Recovery alert.
    if recovered:
        title = f"AI Map: {len(recovered)} news feed(s) recovered"
        body = "\n".join(recovered)
        if a.dry_run:
            print(f"[feed-alert] DRY-RUN recovery alert:\n  Title: {title}\n  " + "\n  ".join(recovered))
        else:
            sent_any |= ntfy(title, body, priority="default", tags="white_check_mark")

    if a.dry_run:
        print("[feed-alert] dry-run; state not updated")
        return

    # Persist the new broken set so we don't re-alert next hour.
    state = dict(
        alerted=sorted(current),
        updated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    os.makedirs(os.path.dirname(a.state), exist_ok=True)
    json.dump(state, open(a.state, "w"), indent=2)

    if newly_broken:
        print(f"[feed-alert] alerted: {len(newly_broken)} newly down ({', '.join(newly_broken)})")
    if recovered:
        print(f"[feed-alert] recovered: {', '.join(recovered)}")
    if not sent_any and (newly_broken or recovered):
        print("[feed-alert] note: notification(s) failed to send; state still updated")


if __name__ == "__main__":
    main()
