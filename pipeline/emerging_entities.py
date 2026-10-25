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
NTFY_URL = os.environ.get("AIMAP_NTFY_URL", "https://ntfy.sh/jag_alerts")


def ntfy(title, body, priority="default", tags="bulb"):
    """POST a notification to the ntfy topic (same channel as feed_alert.py). Best-effort."""
    import urllib.request
    if os.environ.get("AIMAP_ALERTS") == "off":
        print(f"[emerging] AIMAP_ALERTS=off; would send: {title!r}")
        return False
    headers = {"Title": title, "Priority": priority, "Tags": tags}
    try:
        req = urllib.request.Request(NTFY_URL, data=body.encode("utf-8"), headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:
        print(f"[emerging] ntfy send failed: {e}")
        return False

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


def compute(days=14, min_stories=2):
    """Core report, reusable by the CLI and the curator web app. Returns (rows, curated_hit) where
    rows is the full sorted candidate list (brand-new first) and curated_hit is a Counter of names the
    dictionary already covers. Honours the curation overlay's `dismissed` set (hides those names)."""
    if not os.path.exists(LLM_CACHE):
        return [], Counter()
    cache = json.load(open(LLM_CACHE))
    rec, seed_ids = curated()
    dismissed = entities.load_curation().get("dismissed", set())
    con = sqlite3.connect(DB)
    meta = load_meta(con, days)
    cutoff = int(time.time()) - days * 86400

    agg = {}
    curated_hit = Counter()
    for story_id, ents in cache.items():
        m = meta.get(story_id)
        if not m:
            continue
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
            if not key or key in dismissed:
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
        if n < min_stories:
            continue
        nm = e["forms"].most_common(1)[0][0]
        near = near_entities(rec, nm)
        rows.append(dict(
            name=nm, slug=key, type=e["types"].most_common(1)[0][0], stories=n, engagement=e["eng"],
            top_source=e["sources"].most_common(1)[0][0], aliases=sorted(e["forms"]),
            maps_to=near, status=("alias of " + near[0] if near else "NEW"),
            last_seen=datetime.fromtimestamp(e["last"], timezone.utc).strftime("%Y-%m-%d"),
            examples=e["titles"]))
    rows.sort(key=lambda r: (bool(r["maps_to"]), -r["stories"], -r["engagement"]))
    return rows, curated_hit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14, help="only stories first seen in the last N days")
    ap.add_argument("--min-stories", type=int, default=2, help="minimum distinct stories to list a name")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--json", default=None, help="also write the ranked rows to this JSON file")
    ap.add_argument("--ntfy", action="store_true", help="also push a short digest to ntfy (weekly use)")
    a = ap.parse_args()

    rows, curated_hit = compute(a.days, a.min_stories)
    shown = rows[: a.top]

    print(f"\nEmerging entities — extracted by the LLM, not yet in the dictionary "
          f"(last {a.days} days, >= {a.min_stories} stories)\n")
    if not shown:
        print("  (none — the dictionary is covering everything the LLM finds)\n")
    else:
        print(f"  {'#st':>3}  {'type':<11} {'name':<24} {'last':<10} {'curate as':<18} top source")
        print(f"  {'-'*3}  {'-'*11} {'-'*24} {'-'*10} {'-'*18} {'-'*16}")
        for r in shown:
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
        json.dump(shown, open(a.json, "w"), indent=2)
        print(f"  wrote {a.json}\n")

    if a.ntfy and rows:
        new = [r for r in rows if not r["maps_to"]]
        var = [r for r in rows if r["maps_to"]]
        lines = [f"Last {a.days}d · extracted by the LLM, not in the dictionary", ""]
        if new:
            lines.append("NEW — add as entities:")
            lines += [f"• {r['name']} ({r['type']}, {r['stories']})" for r in new[:12]]
        if var:
            lines.append("")
            lines.append("Variants — fold in as alias:")
            lines += [f"• {r['name']} → {r['maps_to'][0]}" for r in var[:8]]
        title = f"AI Map: {len(new)} new + {len(var)} variant entities to curate"
        ok = ntfy(title, "\n".join(lines), tags="bulb")
        print(f"  ntfy digest {'sent' if ok else 'NOT sent'} ({len(new)} new, {len(var)} variants)\n")
    elif a.ntfy:
        print("  ntfy: nothing to report; no push sent\n")


if __name__ == "__main__":
    main()
