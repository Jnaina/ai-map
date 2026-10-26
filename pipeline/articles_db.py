"""Persist every discovered link to SQLite and export a 90-day feed for the web UI.

The build only fetches the last 7 days, but we want a rolling 90-day archive of the
articles that fed the heat map. So each run records all of this run's stories into
`data/articles.db` (de-duplicated by URL, accumulating over time), then exports the
last 90 days to `web/data/articles.json` for the static Articles page to read.

Standard library only (sqlite3), to match the rest of the pipeline.
"""
import json
import os
import sqlite3
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone

import favicons

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "articles.db")
EXPORT_PATH = os.path.join(ROOT, "web", "data", "articles.json")
NEW_COUNTS_PATH = os.path.join(ROOT, "web", "data", "new_counts.json")
FAVICON_DIR = os.path.join(ROOT, "web", "assets", "favicons")
EXPORT_DAYS = 90
# Sources exempt from the 90-day display window: their full recorded history is always
# exported. Added 2026-10-19 so the sparse Anthropic feeds (no official RSS; mirrors hold
# only ~10-26 items) keep their one-off backfilled history instead of aging out.
FULL_HISTORY_SOURCES = {"Anthropic", "Anthropic Engineering"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
  url        TEXT PRIMARY KEY,
  title      TEXT,
  source     TEXT,
  domain     TEXT,
  kind       TEXT,
  published  INTEGER,   -- unix seconds, when the article was published
  engagement INTEGER,   -- points / upvotes / stars, raw
  discussion TEXT,       -- e.g. the Hacker News thread, if any
  first_seen INTEGER,   -- when this build first recorded the link
  last_seen  INTEGER     -- most recent build that saw it
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source);
"""


def _domain(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def connect(db_path=DB_PATH):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def first_seen_map(urls, db_path=DB_PATH):
    """{url: first_seen_unix} for the urls already in the archive (missing urls omitted).
    Used by the build to give a real recency to items whose feed date was unreliable."""
    urls = [u for u in urls if u]
    if not urls:
        return {}
    conn = connect(db_path)
    out = {}
    try:
        for i in range(0, len(urls), 400):  # stay under SQLite's bound-variable limit
            chunk = urls[i:i + 400]
            q = "SELECT url, first_seen FROM articles WHERE url IN (%s)" % ",".join("?" * len(chunk))
            for url, fs in conn.execute(q, chunk):
                if fs:
                    out[url] = fs
    finally:
        conn.close()
    return out


def record(stories, db_path=DB_PATH, log=print):
    """Upsert every story (link) into the archive. URL is the identity; a link seen
    again updates its fields and `last_seen` but keeps the original `first_seen`."""
    now = int(time.time())
    conn = connect(db_path)
    rows = 0
    with conn:
        for s in stories:
            url = s.get("url") or s.get("id")
            if not url:
                continue
            conn.execute(
                """INSERT INTO articles
                     (url,title,source,domain,kind,published,engagement,discussion,first_seen,last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(url) DO UPDATE SET
                     title=excluded.title, source=excluded.source, domain=excluded.domain,
                     kind=excluded.kind, published=excluded.published,
                     engagement=max(articles.engagement, excluded.engagement),
                     discussion=excluded.discussion, last_seen=excluded.last_seen""",
                (url, s.get("title") or "", s.get("source") or "", _domain(url),
                 s.get("kind") or "", int(s.get("published") or 0), int(s.get("engagement") or 0),
                 s.get("discussion") or "", now, now))
            rows += 1
    total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    conn.close()
    log(f"[articles] recorded {rows} links this run; archive now holds {total}")


def export_recent(db_path=DB_PATH, out_path=EXPORT_PATH, days=EXPORT_DAYS, log=print):
    """Write web/data/articles.json: the last `days` of links, grouped by source,
    newest first, with a per-source count for the sidebar."""
    cutoff = int(time.time()) - days * 86400
    conn = connect(db_path)
    # last `days` for normal sources, plus the full history of any FULL_HISTORY_SOURCES
    exempt = sorted(FULL_HISTORY_SOURCES)
    ph = ",".join("?" * len(exempt))
    where = "published >= ?" + (f" OR source IN ({ph})" if exempt else "")
    # Ordered by first_seen (discovery) DESC so the Articles page matches the on-map "incoming feed",
    # which is also discovery-ordered. first_seen is stable per URL (set once), so synthetic entries
    # stamped published=now for the heat map (HF Trending, Trending Repos) no longer flood the top.
    cur = conn.execute(
        f"""SELECT url,title,source,domain,kind,published,engagement,discussion,first_seen
           FROM articles WHERE {where} ORDER BY first_seen DESC, published DESC""", (cutoff, *exempt))
    by_source = defaultdict(list)
    total = 0
    for url, title, source, domain, kind, published, engagement, discussion, first_seen in cur:
        by_source[source or domain or "Other"].append(dict(
            url=url, title=title, domain=domain, kind=kind,
            published=published, engagement=engagement,
            discussion=discussion or None, first_seen=first_seen))
        total += 1
    conn.close()

    sources = []
    for s, a in by_source.items():
        dom = favicons.domain_for(s, a)
        sources.append({"source": s, "count": len(a), "domain": dom,
                        "favicon": favicons.ensure(dom, FAVICON_DIR, log=log)})
    sources.sort(key=lambda x: x["source"].lower())  # alphabetical by source name
    data = dict(
        generated_at=datetime.fromtimestamp(time.time(), timezone.utc).isoformat(timespec="minutes"),
        days=days, total=total, sources=sources, articles=by_source)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    json.dump(data, open(out_path, "w"), separators=(",", ":"))
    log(f"[articles] exported {total} links from {len(sources)} sources (last {days} days) -> {out_path}")


def write_new_counts(db_path=DB_PATH, out_path=NEW_COUNTS_PATH, window_s=3600,
                     latest_window_s=86400, latest_max=500, log=print):
    """Write web/data/new_counts.json: how many links were first discovered in the last
    `window_s` seconds (i.e. new since the previous hourly build), plus the on-map "Incoming feed"
    ticker items from the last `latest_window_s` seconds. The map page shows the tally under the
    Articles button and streams the ticker into the lower-left HUD."""
    now = int(time.time())
    conn = connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM articles WHERE first_seen >= ?", (now - window_s,)).fetchone()[0]
    # Items for the on-map "Incoming feed" ticker (title + source + link), covering the last
    # `latest_window_s` (24 h), newest-discovered first. Capped at `latest_max` so the tiny
    # new_counts.json the page polls every 90s can't balloon on a busy day; the HUD scrolls through
    # them and eases back to the top when the viewer stops scrolling.
    latest = [
        {"title": t, "source": s, "url": u, "first_seen": fs}
        for (t, s, u, fs) in conn.execute(
            "SELECT title, source, url, first_seen FROM articles "
            "WHERE title IS NOT NULL AND title <> '' AND first_seen >= ? "
            "ORDER BY first_seen DESC, published DESC LIMIT ?",
            (now - latest_window_s, latest_max)
        ).fetchall()
    ]
    conn.close()
    data = dict(new_last_hour=n, window_seconds=window_s, latest_window_seconds=latest_window_s,
                generated_at=datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="minutes"),
                latest=latest)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    json.dump(data, open(out_path, "w"), separators=(",", ":"))
    log(f"[articles] {n} new links in the last {window_s // 60} min; "
        f"feed spans last {latest_window_s // 3600}h ({len(latest)} items) -> {out_path}")
    return n
