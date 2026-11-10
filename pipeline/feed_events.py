#!/usr/bin/env python3
"""Source failure/recovery ledger.

`feed_health.py` answers "what is wrong right now?". This answers "what has been wrong over
time, and for how long?" — the evidence needed to decide per source whether to keep it,
change its pull interval, or drop it.

It walks the same build logs (reusing feed_health's log dir, timestamp parsing and line
patterns) and records an event whenever a source's state actually changes:

Severity is one of `error` (a real outage: fail/recover), `quiet` (reachable but nothing new:
stale/empty/active - tracked, never counted as broken) or `info` (feed_health's coarse view).

    fail      a live fetch attempt errored (HTTP status, timeout, TLS, ...)          [error]
    recover   an errored source's pulls worked again                                  [error]
    stale     a live fetch came back empty so the run reused the cache                [quiet]
    empty     a live fetch succeeded with nothing inside the window                    [quiet]
    active    a quiet source delivered content again                                    [quiet]
    silent    nothing across the whole retained window, per feed_health.json            [info]

State only changes on runs where the source was *attempted*, so the ~1h lane produces one
observation per pull rather than one per 2-minute run. Cached replays carry no information
and are skipped.

Outputs:
    data/feed_events.jsonl           append-only event log (one JSON object per event)
    data/feed_events.state.json      ledger state, so re-running never duplicates an event
    data/feed_events_summary.json    per-source aggregates + an advisory recommendation

Usage:
    python3 pipeline/feed_events.py             # process runs newer than the ledger
    python3 pipeline/feed_events.py --backfill  # re-read all retained logs too
    python3 pipeline/feed_events.py --dry-run   # report what would be recorded
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import feed_health as FH  # noqa: E402  (reuses LOGDIR, run_time, OK_RX, ERR_RX)

DATA = os.path.join(ROOT, "data")
LEDGER = os.path.join(DATA, "feed_events.jsonl")
STATE = os.path.join(DATA, "feed_events.state.json")
SUMMARY = os.path.join(DATA, "feed_events_summary.json")
HEALTH = os.path.join(DATA, "feed_health.json")
CURSOR = LEDGER + ".cursor"

# The throttled API sources (feed_health's OK_RX only matches "... N stories"). Live attempts
# are logged under their own headings, which map back to the throttle key used when cached.
CACHED_MARK = "(cached"
STALE_MARK = "(stale cache"
API_RX = re.compile(r"^  (?P<name>Hugging Face Papers|Hugging Face trending models|"
                    r"GitHub new AI repos|Trending AI repos):[^\d]*(?P<n>\d+)")
API_KEY = {
    "Hugging Face Papers": "HF papers",
    "Hugging Face trending models": "HF models",
    "GitHub new AI repos": "GitHub",
    "Trending AI repos": "GitHub trending",
}
API_DEAD_RX = re.compile(r"^  Trending AI repos: pool empty")

QUIET = ("stale", "empty")       # reachable, just nothing new: not an outage
INTERVALS = {}                   # source -> configured pull interval (hours), from sources.py
DEFAULT_INTERVAL_H = 1.0

REVIEW_NOTE = {
    "review_due": "2026-10-26",
    "review_action": ("Joint decision per source: keep / change pull interval / drop. Weigh "
                      "failures + minutes_down_total (broken?) against days_since_last_story "
                      "(dormant?) and the errors map (rate limited? blocked?) below."),
}


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def kind(err):
    """Coarse error class, so 305 separate 429s become one decision signal."""
    e = (err or "").lower()
    for pat, name in (("429", "rate-limited(429)"), ("403", "blocked(403)"), ("503", "unavailable(503)"),
                      ("500", "server(500)"), ("timed out", "timeout"), ("timeout", "timeout"),
                      ("unreachable", "network-unreachable"), ("connection reset", "connection-reset"),
                      ("404", "not-found(404)"), ("certificate", "tls"), ("ssl", "tls"),
                      ("decode", "bad-payload"), ("json", "bad-payload"), ("xml", "bad-payload")):
        if pat in e:
            return name
    return "other"


def canon(name):
    """Fold per-item failures onto the source they belong to, so the ledger stays source-level."""
    for pat, key in (("trending seed ", "GitHub trending (seeds)"),
                     ("trending topic", "GitHub trending (topics)"),
                     ("GitHub topic:", "GitHub (topics)")):
        if name.startswith(pat):
            return key
    return name


def attempt_outcomes(path):
    """One run -> {source: (outcome, detail)} for sources *attempted* in this run."""
    out = {}
    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                m = FH.ERR_RX.match(line)
                if m:
                    name = m.group("name")
                    if name.startswith(("favicon", "body ")) or name.endswith("cache write failed"):
                        continue        # article icons, article bodies and our own cache notes
                    out.setdefault(canon(name), ("fail", m.group("err")[:140]))
                    continue
                if CACHED_MARK in line:
                    continue            # not attempted this run
                if API_DEAD_RX.match(line):
                    out["GitHub trending"] = ("fail", "trending pool empty (API unreachable)")
                    continue
                m = API_RX.match(line)
                if m:
                    key, n = API_KEY[m.group("name")], int(m.group("n"))
                    out[key] = ("ok", None) if n > 0 else ("empty", "no items in window")
                    continue
                if STALE_MARK in line:
                    m = FH.OK_RX.match(line)
                    if m:
                        out[m.group("name")] = ("stale", "live fetch empty; cache reused")
                    continue
                m = FH.OK_RX.match(line)
                if m:
                    n = int(m.group("n"))
                    out[m.group("name")] = ("ok", None) if n > 0 else ("empty", "no story in window")
    except OSError:
        pass
    return out


def transition(state, feed, outcome, detail, ts, emit):
    """Advance one source's state machine, emitting only real state changes.

    A transient blip therefore still leaves a trace (fail, then recover on the next pull),
    while a source that stays in the same state across many pulls records nothing new.
    """
    s = state.setdefault(feed, {})
    prev = s.get("status")
    cls = "error" if outcome == "fail" else ("quiet" if outcome in QUIET else "healthy")
    pcls = "error" if prev == "fail" else ("quiet" if prev in QUIET else "unknown")

    if outcome == "fail":
        errs = dict(Counter(s.get("errors") or {}))
        errs[kind(detail)] = errs.get(kind(detail), 0) + 1
        s["errors"] = errs
        s["last_error"] = detail
    if outcome in QUIET:
        s["quiet_pulls"] = s.get("quiet_pulls", 0) + 1

    if prev != outcome:
        if cls == "error":
            if pcls != "error":
                s["err_since"], s["err_runs"] = ts, 1
            else:
                s["err_runs"] = s.get("err_runs", 0) + 1
            s["failures"] = s.get("failures", 0) + 1
        elif cls == "quiet" and pcls != "quiet":
            s["quiet_since"] = ts
        if pcls == "error" and cls != "error":
            # Error streak ended: the pull worked again, whether or not it had content.
            s["outages"] = s.get("outages", 0) + 1
            started = s.get("err_since")
            if started:
                mins = round((ts - started) / 60.0, 1)
                s["minutes_down_total"] = round(s.get("minutes_down_total", 0) + mins, 1)
                s["longest_minutes"] = max(s.get("longest_minutes", 0), mins)
            s["last_recovery"] = iso(ts)
            emit(feed, "recover", prev,
                 f"pulls succeeding again after {s.get('err_runs', 1)} failed pull(s)"
                 + (f" ({round((ts - started) / 60.0)} min)" if started else "")
                 + ("; nothing new in window yet" if outcome != "ok" else ""), ts, "error")
            s["err_since"] = None
        if outcome == "ok":
            if pcls == "quiet":
                emit(feed, "active", prev, "content again", ts, "info")
            s["last_story"] = ts
        s["status"] = outcome
        s["since"] = ts
        if cls in ("error", "quiet"):
            emit(feed, outcome, prev, detail, ts, cls)
    elif cls == "error":
        s["err_runs"] = s.get("err_runs", 0) + 1
        s["since"] = ts


def advice(v):
    """Heuristic steer for the keep / retune / drop decision. Advisory only, never acts."""
    errs = Counter(v["errors"])
    notes = [f"{e} x{c}" for e, c in errs.most_common(3)]
    iv = v["interval_h"] or DEFAULT_INTERVAL_H
    if errs.get("rate-limited(429)", 0) >= 3:
        notes.append(f"rate limited: try a longer interval than {iv:g}h, or drop")
    if errs.get("blocked(403)", 0) >= 3:
        notes.append("bot-blocked: needs browser impersonation, or drop")
    if v["failures"] >= 8:
        notes.append("frequently erroring: cache fallback is masking it")
    if v["days_since_last_story"] is not None and v["days_since_last_story"] > 21:
        notes.append(f"dormant {v['days_since_last_story']:.0f}d: verify feed, else drop")
    if v["status"] == "fail":
        notes.append(f"currently erroring: {v.get('last_error') or ''}"[:60])
    return "; ".join(n for n in notes if n) or "healthy"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="also re-read retained logs")
    ap.add_argument("--dry-run", action="store_true", help="write nothing")
    a = ap.parse_args()

    state = load_json(STATE, {})
    cursor = load_json(CURSOR, {}).get("ts", 0)
    if not state:                       # first ledger run: rebuild history from retained logs
        a.backfill, cursor = True, 0

    try:
        import sources
        INTERVALS.update({n: sources.FEED_INTERVAL_HOURS.get(n, sources.DEFAULT_FEED_INTERVAL_HOURS)
                          for n, _, _ in sources.RSS_FEEDS})
        for k in ("HF papers", "HF models", "GitHub", "GitHub trending"):
            INTERVALS[k] = sources.DEFAULT_FEED_INTERVAL_HOURS
    except Exception:
        pass

    runs = []
    for p in glob.glob(os.path.join(FH.LOGDIR, "build-*.log")):
        t = FH.run_time(p)
        if t and t.timestamp() > cursor:
            runs.append((t.timestamp(), attempt_outcomes(p)))
    runs.sort(key=lambda r: r[0])

    events = []

    def emit(feed, outcome, prev, detail, ts, severity):
        events.append({"ts": iso(ts), "feed": feed, "event": outcome, "from": prev,
                       "severity": severity, "detail": detail, "interval_h": INTERVALS.get(feed)})

    for ts, outcomes in runs:
        for feed, (outcome, detail) in outcomes.items():
            transition(state, feed, outcome, detail, ts, emit)

    # Fold in feed_health's coarse view: dormancy across the whole retained window.
    health = load_json(HEALTH, {})
    for feed, f in health.get("feeds", {}).items():
        s = state.setdefault(feed, {})
        st = f.get("status")
        if st == "silent" and not s.get("silent_since"):
            s["silent_since"] = health.get("generated_at")
            emit(feed, "silent", s.get("status"),
                 f"no story across {health.get('runs_analyzed')} runs",
                 datetime.now(timezone.utc).timestamp(), "info")
        elif st != "silent" and s.get("silent_since"):
            s["silent_since"] = None

    now = datetime.now(timezone.utc)
    kept = [FH.run_time(p) for p in glob.glob(os.path.join(FH.LOGDIR, "build-*.log"))]
    kept = [t.timestamp() for t in kept if t]
    retained_days = round((now.timestamp() - min(kept)) / 86400, 1) if kept else 0
    feeds = {}
    for feed, s in sorted(state.items()):
        age_d = round((now - datetime.fromtimestamp(s["last_story"], timezone.utc)).total_seconds() / 86400, 1) \
            if s.get("last_story") else None
        feeds[feed] = dict(status=s.get("status"), interval_h=INTERVALS.get(feed),
                           failures=s.get("failures", 0), outages=s.get("outages", 0),
                           minutes_down_total=s.get("minutes_down_total", 0),
                           longest_minutes=s.get("longest_minutes", 0),
                           quiet_pulls=s.get("quiet_pulls", 0), errors=dict(s.get("errors") or {}),
                           last_error=s.get("last_error"), last_error_ts=s.get("since") and iso(s["since"]),
                           last_story=iso(s["last_story"]) if s.get("last_story") else None,
                           days_since_last_story=age_d, last_recovery=s.get("last_recovery"),
                           silent_since=s.get("silent_since"))
    for v in feeds.values():
        v["advice"] = advice(v)

    errs_seen = [e for e in events if e["severity"] == "error"]
    print(f"[feed-events] {len(runs)} runs read, {len(events)} new events "
          f"({len(errs_seen)} error/recovery) "
          + ("(dry run, nothing written)" if a.dry_run else f"-> {LEDGER}"))
    for e in errs_seen[-20:]:
        print(f"    {e['ts']}  {e['feed'][:30]:30} {e['event']:8} {('was ' + str(e['from']) + '; ') if e['from'] else ''}{e['detail'] or ''}"[:148])
    if len(errs_seen) > 20:
        print(f"    ... {len(errs_seen) - 20} earlier error events not shown")

    broken = sorted((f for f, v in feeds.items() if v["failures"]), key=lambda f: -feeds[f]["failures"])
    print(f"[feed-events] sources that have errored at least once: {len(broken)}")
    for f in broken[:15]:
        v = feeds[f]
        print(f"    fails={v['failures']:3} outages={v['outages']:3} down={v['minutes_down_total']:7.0f}m "
              f"max={v['longest_minutes']:6.0f}m  {f[:26]:26} iv={v['interval_h'] or '-':>4}  {v['advice'][:56]}")
    dormant = sorted((f for f, v in feeds.items() if (v["days_since_last_story"] or 0) > 21),
                     key=lambda f: -feeds[f]["days_since_last_story"])
    print(f"[feed-events] sources with nothing new for >21 days: {len(dormant)}")
    for f in dormant[:15]:
        v = feeds[f]
        print(f"    {v['days_since_last_story']:6.1f} d quiet  {f[:30]:30} fails={v['failures']:3} "
              f"quiet_pulls={v['quiet_pulls']:4}  {v['advice'][:44]}")
    none = [f for f, v in feeds.items() if v["last_story"] is None]
    print(f"[feed-events] sources with no content anywhere in the retained logs: {len(none)} "
          f"(strongest drop candidates; log retention is {retained_days} d)")
    for f in none[:15]:
        v = feeds[f]
        print(f"    {f[:34]:34} fails={v['failures']:3} quiet_pulls={v['quiet_pulls']:4} "
              f"errors={sum(v['errors'].values()):4}  {v['advice'][:40]}")

    if a.dry_run:
        return
    os.makedirs(DATA, exist_ok=True)
    if events:
        with open(LEDGER, "a") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
    for path, payload in ((CURSOR, {"ts": max([t for t, _ in runs], default=cursor)}),
                          (STATE, state),
                          (SUMMARY, dict(REVIEW_NOTE, generated_at=now.isoformat(timespec="seconds"),
                                         runs_read=len(runs), new_events=len(events), feeds=feeds))):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=1 if path == STATE else 2)
        os.replace(tmp, path)


if __name__ == "__main__":
    main()
