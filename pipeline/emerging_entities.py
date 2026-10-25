#!/usr/bin/env python3
"""Emerging-entities report: names the LLM keeps extracting from headlines that are NOT yet in the
curated dictionary (entities.py SEED), ranked by how many recent stories mention them.

The map stays current automatically because llm_extract pulls entities the dictionary misses -- but
those stay uncurated (no aliases, no domain attribution, no disambiguation, no concept routing). This
report turns that gap into a to-do list: "these new models/companies showed up >= N times this week;
promote them into SEED." It only reads data the pipeline already produces (data/llm-cache.json +
data/articles.db); it changes nothing.

Usage:
  python3 pipeline/emerging_entities.py [--days 14] [--min-stories 2] [--top 40] [--json PATH]
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))
import entities  # noqa: E402

LLM_CACHE = os.path.join(ROOT, "data", "llm-cache.json")
DB = os.path.join(ROOT, "data", "articles.db")

# Generic terms the LLM is told to ignore but occasionally emits; never worth curating as a node.
STOP = {"ai", "a.i.", "llm", "llms", "ml", "model", "models", "api", "apis", "gpu", "gpus", "cpu",
        "agent", "agents", "chatbot", "chatbots", "app", "apps", "startup", "startups", "framework",
        "tool", "tools", "open source", "open-source", "neural network", "neural networks", "dataset",
        "datasets", "algorithm", "algorithms", "software", "hardware", "the ai", "an ai"}


def curated():
    """A SEED-only recogniser (no LLM-added entities) + its id set, for the 'already covered?' test."""
    rec = entities.Recogniser()
    return rec, set(rec.ents.keys())


def is_curated(rec, seed_ids, name):
    """True if `name` already maps onto a dictionary entity the way _merge does (exact slug or the
    partial-label match). These never spawn a new node, so they're not 'emerging'."""
    sid = entities.slug(name)
    if sid in seed_ids:
        return True
    nl = name.lower()
    for info in rec.ents.values():
        lab = info["label"].lower()
        if lab == nl or lab.endswith(" " + nl) or nl.endswith(" " + lab):
            return True
    return False


def near_entities(rec, name):
    """Dictionary entities whose aliases DO fire on this name (what rec.find sees) even though _merge
    would still create a separate node. Non-empty -> the candidate is a variant/duplicate to fold in
    as an alias (e.g. 'Mistral' -> mistral-ai); empty -> genuinely new, add as its own entity."""
    try:
        hit = rec.find({"id": "_probe", "kind": "news", "title": name, "text": name,
                        "source": "", "url": ""})
    except Exception:
        hit = set()
    return sorted(e for e in hit if e != entities.slug(name))


def load_meta(con, days):
    """{story_id -> story meta} keyed by BOTH the article url (RSS cache keys) and 'hn:<objectID>'
    (Hacker News cache keys, recovered from the discussion link)."""
    meta = {}
    for url, title, source, pub, eng, fs, disc in con.execute(
            "SELECT url,title,source,published,engagement,first_seen,discussion FROM articles"):
        m = dict(title=title or "", source=source or "", published=pub or 0,
                 engagement=eng or 0, first_seen=fs or 0)
        meta[url] = m
        if disc and "item?id=" in disc:
            meta["hn:" + disc.split("item?id=")[-1].strip()] = m
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14, help="only stories first seen in the last N days")
    ap.add_argument("--min-stories", type=int, default=2, help="minimum distinct stories to list a name")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--json", default=None, help="also write the ranked rows to this JSON file")
    a = ap.parse_args()

    if not os.path.exists(LLM_CACHE):
        print("no llm-cache.json yet (LLM extraction has not run)"); return
    cache = json.load(open(LLM_CACHE))
    rec, seed_ids = curated()
    con = sqlite3.connect(DB)
    meta = load_meta(con, a.days)
    cutoff = int(time.time()) - a.days * 86400

    agg = {}
    curated_hit = Counter()  # slug -> count, for a one-line "already covered" sanity figure
    for story_id, ents in cache.items():
        m = meta.get(story_id)
        if not m:
            continue  # can't date/attribute it (aged out of the 90-day DB, or stale cache entry)
        when = m["first_seen"] or m["published"]
        if not when or when < cutoff:
            continue
        for pair in ents or []:
            if not pair:
                continue
            name = (pair[0] or "").strip()
            typ = (pair[1] if len(pair) > 1 else "") or "?"
            if len(name) < 2 or name.lower() in STOP:
                continue
            if is_curated(rec, seed_ids, name):
                curated_hit[entities.slug(name)] += 1
                continue
            key = entities.slug(name)
            if not key:
                continue
            e = agg.setdefault(key, dict(forms=Counter(), types=Counter(), stories=set(),
                                         titles=[], sources=Counter(), eng=0, last=0))
            e["types"][typ] += 1
            e["forms"][name] += 1
            if story_id not in e["stories"]:
                e["stories"].add(story_id)
                e["eng"] += m["engagement"]
                e["last"] = max(e["last"], when)
                e["sources"][m["source"]] += 1
                if len(e["titles"]) < 3 and m["title"]:
                    e["titles"].append(m["title"][:72])

    rows = []
    for key, e in agg.items():
        n = len(e["stories"])
        if n < a.min_stories:
            continue
        nm = e["forms"].most_common(1)[0][0]
        near = near_entities(rec, nm)
        rows.append(dict(
            name=nm, slug=key, type=e["types"].most_common(1)[0][0], stories=n, engagement=e["eng"],
            top_source=e["sources"].most_common(1)[0][0],
            maps_to=near, status=("alias of " + near[0] if near else "NEW"),
            last_seen=datetime.fromtimestamp(e["last"], timezone.utc).strftime("%Y-%m-%d"),
            examples=e["titles"]))
    # brand-new entities first (highest curation value), then by story count
    rows.sort(key=lambda r: (bool(r["maps_to"]), -r["stories"], -r["engagement"]))
    rows = rows[: a.top]

    print(f"\nEmerging entities — extracted by the LLM, not yet in the dictionary "
          f"(last {a.days} days, >= {a.min_stories} stories)\n")
    if not rows:
        print("  (none — the dictionary is covering everything the LLM finds)\n");
    else:
        print(f"  {'#st':>3}  {'type':<11} {'name':<24} {'last':<10} {'curate as':<18} top source")
        print(f"  {'-'*3}  {'-'*11} {'-'*24} {'-'*10} {'-'*18} {'-'*16}")
        for r in rows:
            print(f"  {r['stories']:>3}  {r['type']:<11} {r['name'][:24]:<24} {r['last_seen']:<10} "
                  f"{r['status'][:18]:<18} {r['top_source'][:16]}")
        new_n = sum(1 for r in rows if not r["maps_to"])
        print(f"\n  {len(rows)} candidates ({new_n} brand-new, {len(rows)-new_n} variants of an "
              f"existing node). Example headlines for the top brand-new ones:")
        for r in [x for x in rows if not x["maps_to"]][:5]:
            print(f"   • {r['name']} [{r['type']}] — {r['examples'][0] if r['examples'] else ''}")
    print(f"\n  (for comparison, {sum(curated_hit.values())} LLM mentions in the window already map "
          f"to {len(curated_hit)} dictionary entities)\n")

    if a.json:
        json.dump(rows, open(a.json, "w"), indent=2)
        print(f"  wrote {a.json}\n")


if __name__ == "__main__":
    main()
