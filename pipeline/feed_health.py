#!/usr/bin/env python3
"""Feed-health report.

Parses the per-run build logs (logs/build-*.log) and summarises each RSS feed's intake
status over time, so connectivity issues are easy to spot: hard failures (HTTP errors,
rate-limits, timeouts), feeds that have gone silent, and recurring errors.

Writes data/feed_health.json and prints a short summary. Run anytime:

    python3 pipeline/feed_health.py            # all retained logs
    python3 pipeline/feed_health.py --runs 48  # just the most recent 48 runs

It is also invoked automatically at the end of run.sh.
"""
import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGDIR = os.path.join(ROOT, "logs")
OUT = os.path.join(ROOT, "data", "feed_health.json")

# A successful feed line looks like:  "  TechCrunch AI: 20 stories"  (also HN, and cached lines).
OK_RX = re.compile(r"^  (?P<name>[^!][^:]*?): (?P<n>\d+) stories")
# A failure line looks like:  "  ! VentureBeat AI: HTTP Error 429: Too Many Requests"
ERR_RX = re.compile(r"^  ! (?P<name>[^:]+?): (?P<err>.+)$")

SILENT_MIN_RUNS = 24  # only call a feed "silent" once we've seen it in at least this many runs


def run_time(path):
    m = re.search(r"build-(\d{8})-(\d{4})\.log$", path)
    return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M") if m else None


def parse_log(path):
    """One run -> {feed_name: ('ok', count) | ('fail', errmsg)}."""
    res = {}
    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                m = OK_RX.match(line)
                if m:
                    res[m.group("name")] = ("ok", int(m.group("n")))
                    continue
                m = ERR_RX.match(line)
                if m:
                    name = m.group("name")
                    if name.startswith("favicon") or name.endswith("cache write failed"):
                        continue  # favicon icons and our own cache-write notes aren't feed intake
                    res.setdefault(name, ("fail", m.group("err")[:140]))
    except OSError:
        pass
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=0, help="only the most recent N runs (0 = all retained)")
    a = ap.parse_args()

    logs = sorted(p for p in glob.glob(os.path.join(LOGDIR, "build-*.log")) if run_time(p))
    if a.runs:
        logs = logs[-a.runs:]
    if not logs:
        print("[feed-health] no build logs found")
        return
    runs = [(run_time(p), parse_log(p)) for p in logs]

    configured = set()
    try:
        sys.path.insert(0, os.path.join(ROOT, "pipeline"))
        import sources
        configured = {n for n, _, _ in sources.RSS_FEEDS} | {"Hacker News"}
    except Exception:
        pass

    names = set(configured)
    for _, r in runs:
        names |= set(r)

    feeds = {}
    for name in sorted(names):
        present = [(t, r[name]) for t, r in runs if name in r]
        successes = [(t, v[1]) for t, v in present if v[0] == "ok"]
        failures = [(t, v[1]) for t, v in present if v[0] == "fail"]
        last_nonzero = next((t for t, n in reversed(successes) if n > 0), None)
        cons_fail = 0
        for _, v in reversed(present):
            if v[0] == "fail":
                cons_fail += 1
            else:
                break
        zero_streak = 0
        for _, v in reversed(present):
            if v[0] == "ok" and v[1] == 0:
                zero_streak += 1
            else:
                break
        if name in configured and not present:
            status = "missing"          # configured but never appears in the logs
        elif cons_fail >= 3:
            status = "down"             # failing several runs in a row
        elif cons_fail >= 1:
            status = "failing"          # just started failing
        elif last_nonzero is None and len(present) >= SILENT_MIN_RUNS:
            status = "silent"           # no story in the whole retained window (dormant or broken)
        else:
            status = "ok"
        feeds[name] = dict(
            configured=name in configured,
            status=status,
            runs_seen=len(present),
            successes=len(successes),
            failures=len(failures),
            last_success=(successes[-1][0].isoformat() if successes else None),
            last_story=(last_nonzero.isoformat() if last_nonzero else None),
            last_stories=(successes[-1][1] if successes else None),
            consecutive_failures=cons_fail,
            zero_streak=zero_streak,
            last_error=(failures[-1][1] if failures else None),
        )

    errcount = {}
    for _, r in runs:
        for name, v in r.items():
            if v[0] == "fail":
                key = f"{name}: {v[1]}"
                errcount[key] = errcount.get(key, 0) + 1
    recurring = sorted(({"error": k, "count": c} for k, c in errcount.items()), key=lambda x: -x["count"])

    order = {"down": 0, "missing": 1, "failing": 2, "silent": 3}
    problems = sorted(
        (dict(feed=n, **{k: f[k] for k in ("status", "consecutive_failures", "zero_streak", "last_error", "last_story")})
         for n, f in feeds.items() if f["status"] in order),
        key=lambda p: order[p["status"]],
    )

    report = dict(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        runs_analyzed=len(runs),
        window={"from": runs[0][0].isoformat(), "to": runs[-1][0].isoformat()},
        summary={s: sum(1 for f in feeds.values() if f["status"] == s)
                 for s in ("ok", "failing", "down", "silent", "missing")},
        feeds=feeds,
        problems=problems,
        recurring_errors=recurring[:20],
    )
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(report, open(OUT, "w"), indent=2)

    span = f"{runs[0][0]:%Y-%m-%d %H:%M} -> {runs[-1][0]:%Y-%m-%d %H:%M}"
    print(f"[feed-health] {len(runs)} runs ({span}), {len(feeds)} feeds -> {OUT}")
    if problems:
        print(f"[feed-health] {len(problems)} need attention (down/missing/failing/silent):")
        for p in problems:
            when = f"last story {p['last_story'][:10]}" if p["last_story"] else "no story in window"
            err = f" | {p['last_error']}" if p["last_error"] else ""
            print(f"    {p['status'].upper():8} {p['feed']:28} fail={p['consecutive_failures']} zero={p['zero_streak']} {when}{err}")
    else:
        print("[feed-health] all feeds healthy")
    if recurring:
        print("[feed-health] recurring errors in window:")
        for e in recurring[:8]:
            print(f"    {e['count']:4}x  {e['error'][:96]}")


if __name__ == "__main__":
    main()
