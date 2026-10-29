"""One-time backfill of story<->topic edges for articles already in the archive.

The Topics page reads `article_entities`, which the build only started writing from the run that
introduced it, so without this the page would fill in only going forward. This re-runs the cheap,
deterministic parts of the build's entity matching over every archived article and writes the
edges, seeding the page with the existing ~90 days immediately.

It matches on the stored TITLE plus the article's domain (company-site attribution) plus China-AI
routing -- it does NOT re-fetch article bodies and does NOT run the LLM disambiguation or orphan
enrichment that the live build does, because those need network and the full text we didn't keep.
So coverage is close to, but not identical to, what a fresh build would record for the same story.
Safe to re-run: edges are INSERT OR IGNORE, entity meta is upserted.

Run from the repo root:  python3 pipeline/backfill_topics.py
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import articles_db  # noqa: E402
from entities import CHINA_AI, Recogniser  # noqa: E402
from sources import domain_entity  # noqa: E402


def main():
    rec = Recogniser()
    conn = articles_db.connect()
    conn.executescript(articles_db.SCHEMA)  # ensure the new tables exist
    rows = conn.execute("SELECT url, title FROM articles").fetchall()
    print(f"[backfill] scanning {len(rows)} archived articles")

    edges = 0
    used = set()
    seen_china = "china-ai" in rec.ents
    now = int(time.time())
    with conn:
        for url, title in rows:
            ments = set(rec.find({"text": title or "", "title": title or ""}))
            owner = domain_entity(url)
            if owner and owner in rec.ents:
                ments.add(owner)
            if seen_china and (ments & CHINA_AI):
                ments.add("china-ai")
            for e in ments:
                if e not in rec.ents:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO article_entities(url, entity_id) VALUES (?,?)", (url, e))
                edges += 1
                used.add(e)
        for e in used:
            info = rec.ents[e]
            conn.execute(
                """INSERT INTO entities(id,label,type) VALUES (?,?,?)
                   ON CONFLICT(id) DO UPDATE SET label=excluded.label, type=excluded.type""",
                (e, info["label"], info["type"]))
    total_edges = conn.execute("SELECT COUNT(*) FROM article_entities").fetchone()[0]
    tagged = conn.execute("SELECT COUNT(DISTINCT url) FROM article_entities").fetchone()[0]
    conn.close()
    print(f"[backfill] wrote {edges} edges across {len(used)} topics; "
          f"archive now has {total_edges} edges over {tagged} tagged articles "
          f"(elapsed {time.time() - now:.1f}s)")


if __name__ == "__main__":
    main()
