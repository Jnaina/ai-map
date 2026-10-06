"""Build the AI Map graphs: fetch -> recognise entities -> score heat -> write web/data/*.json.

Two views are written from the same fetch:
  week   (web/data/data.json)   7-day window, 24 h half-life, trend vs 24 h ago
  today  (web/data/today.json)  24 h window,   6 h half-life, trend vs 6 h ago

Heat model
----------
Each story contributes  weight(source) x engagement_boost x 0.5 ** (age_hours / HALF_LIFE)
to every entity it mentions, so attention "cools" with the view's half-life. The entity's heat
one trend-lag ago is computed the same way from the stories that existed then.

Status:   new      = not in the curated dictionary, first seen within the last 7 days
          rising   = heat at least 1.5x what it was one trend-lag ago
          steady   = everything else
Breaking: a separate flag: in the last 6 h the entity got >= 2 stories and at least 2.5x the
          (weighted) attention it got in the 6 h before. "Currently trending" lists are ignored.
Links:    entities that appear in the same stories (decayed weight), plus parent links.
"""
import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from entities import AI_RX, Recogniser, llm_extract  # noqa: E402
from sources import fetch_all  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "web", "data")
HISTORY = os.path.join(ROOT, "data", "history.json")
CACHE = os.path.join(ROOT, "data", "stories-cache.json")

VIEWS = [
    dict(name="week", out="data.json", window_h=7 * 24, half_life=24.0, trend_lag=24.0, max_nodes=140, min_stories=2),
    dict(name="today", out="today.json", window_h=24, half_life=6.0, trend_lag=6.0, max_nodes=110, min_stories=2),
]
BREAK_WINDOW_H = 6
BREAK_RATIO = 2.5
BREAK_MIN_STORIES = 2
BREAK_MIN_WEIGHT = 2.0

# Big companies whose mentions alone don't make a story about AI
NOT_AI_SPECIFIC = {"google", "apple", "amazon", "microsoft", "meta", "tesla", "samsung", "intel", "ibm", "oracle",
                   "salesforce", "broadcom", "qualcomm", "huawei", "tencent", "baidu", "bytedance", "tsmc", "arm",
                   "snowflake", "palantir", "sk-hynix", "amd", "waymo", "tim-cook", "andy-jassy", "elon-musk",
                   "mark-zuckerberg", "sundar-pichai", "satya-nadella"}
AI_CONCEPTS = {"ai-agents", "reasoning-models", "open-weight-models", "rag", "agi-superintelligence",
               "ai-safety-alignment", "coding-agents", "video-generation", "on-device-ai"}


def boost(s):
    e = s.get("engagement") or 0
    if e <= 0:
        return 1.0
    scale = {"discussion": 2.0, "paper": 2.0, "model": 3.0, "repo": 3.0}.get(s["kind"], 3.0)
    return 1.0 + math.log10(1 + e) / scale


def decay(age_h, half_life):
    return 0.5 ** (max(age_h, 0) / half_life)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="minutes")


def breaking_stats(stories, mentions, now):
    """Weighted attention and story counts per entity in the last 6 h and the 6 h before."""
    recent_w, prev_w, recent_n = defaultdict(float), defaultdict(float), defaultdict(int)
    for s in stories:
        if s["kind"] in ("model", "repo"):  # trending lists are stamped "now": no real timing
            continue
        age = (now - s["published"]) / 3600
        if age >= 2 * BREAK_WINDOW_H:
            continue
        w = s["weight"] * boost(s)
        for e in mentions.get(s["id"]) or ():
            if age < BREAK_WINDOW_H:
                recent_w[e] += w
                recent_n[e] += 1
            else:
                prev_w[e] += w
    flags = {}
    for e, rw in recent_w.items():
        if recent_n[e] >= BREAK_MIN_STORIES and rw >= BREAK_MIN_WEIGHT and rw >= BREAK_RATIO * max(prev_w[e], 0.4):
            flags[e] = dict(last6h=recent_n[e], ratio=round(rw / max(prev_w[e], 0.4), 1))
    return flags


def build_view(v, stories, mentions, rec, now, hist, breaking, stamp):
    heat, heat_prev = defaultdict(float), defaultdict(float)
    count, by_source, ent_stories = defaultdict(int), defaultdict(lambda: defaultdict(int)), defaultdict(list)
    first_mention, pair = {}, defaultdict(float)
    used = 0
    for s in stories:
        age_h = (now - s["published"]) / 3600
        if age_h > v["window_h"]:
            continue
        ents = mentions.get(s["id"]) or set()
        if not ents:
            continue
        used += 1
        w = s["weight"] * boost(s)
        contrib = w * decay(age_h, v["half_life"])
        if s["kind"] in ("model", "repo"):
            contrib_prev = contrib  # "currently trending" lists have no history: treat as a steady baseline
        else:
            lag = v["trend_lag"]
            contrib_prev = w * decay(age_h - lag, v["half_life"]) if age_h >= lag else 0.0
        for e in ents:
            heat[e] += contrib
            heat_prev[e] += contrib_prev
            count[e] += 1
            by_source[e][s["source"]] += 1
            ent_stories[e].append((contrib, s))
            first_mention[e] = min(first_mention.get(e, s["published"]), s["published"])
        el = sorted(ents)
        for i in range(len(el)):
            for j in range(i + 1, len(el)):
                pair[(el[i], el[j])] += contrib

    cands = [e for e in heat if count[e] >= v["min_stories"]
             or rec.ents[e]["type"] == "Open source" and heat[e] > 1.5 or e in breaking]
    cands.sort(key=lambda e: -heat[e])
    nodes_set = set(cands[: v["max_nodes"]])

    for e in nodes_set:
        h = hist["entities"].setdefault(e, {"label": rec.ents[e]["label"], "first_seen": iso(first_mention[e]), "series": []})
        if v["name"] == "week":
            h["series"] = (h["series"] + [[stamp, round(heat[e], 2)]])[-48:]  # ~2 days hourly

    top = max((heat[e] for e in nodes_set), default=1)
    nodes = []
    for e in sorted(nodes_set, key=lambda x: -heat[x]):
        info = rec.ents[e]
        fs = datetime.fromisoformat(hist["entities"][e]["first_seen"]).timestamp()
        is_new = (not info["seed"]) and (now - fs) < 7 * 86400
        prev = heat_prev[e]
        trend = (heat[e] / prev) if prev > 0.05 else (9.9 if heat[e] > 0.5 else 1.0)
        status = "new" if is_new else ("rising" if trend >= 1.5 and heat[e] >= 1.0 else "steady")
        best = sorted(ent_stories[e], key=lambda x: -x[0])[:8]
        nodes.append(dict(
            id=e, label=info["label"], type=info["type"], parent=info["parent"],
            heat=round(heat[e], 3), heat_norm=round(heat[e] / top, 4), trend=round(min(trend, 9.9), 2),
            status=status, breaking=breaking.get(e), stories=count[e], first_seen=hist["entities"][e]["first_seen"],
            sources=dict(sorted(by_source[e].items(), key=lambda x: -x[1])),
            series=hist["entities"][e]["series"][-24:],
            top_stories=[dict(title=s["title"], url=s["url"], source=s["source"], published=iso(s["published"]),
                              discussion=s.get("discussion")) for _, s in best]))

    per = defaultdict(list)
    for (x, y), w in pair.items():
        if x in nodes_set and y in nodes_set and w > 0.15 * (v["half_life"] / 24):
            per[x].append((w, y))
            per[y].append((w, x))
    keep = {}
    for x, lst in per.items():
        for w, y in sorted(lst, reverse=True)[:6]:
            k = tuple(sorted((x, y)))
            keep[k] = max(keep.get(k, 0), w)
    for e in nodes_set:
        p = rec.ents[e]["parent"]
        if p and p in nodes_set:
            k = tuple(sorted((e, p)))
            keep[k] = max(keep.get(k, 0), 0.5 * max(keep.values(), default=1))
    wmax = max(keep.values(), default=1)
    links = [dict(source=x, target=y, weight=round(w / wmax, 4)) for (x, y), w in keep.items()]

    srcs = defaultdict(int)
    for s in stories:
        if (now - s["published"]) / 3600 <= v["window_h"]:
            srcs[s["source"]] += 1
    data = dict(view=v["name"], generated_at=stamp, window_hours=v["window_h"], half_life_hours=v["half_life"],
                trend_lag_hours=v["trend_lag"], breaking_window_hours=BREAK_WINDOW_H,
                story_count=sum(srcs.values()), sources=dict(sorted(srcs.items(), key=lambda x: -x[1])),
                nodes=nodes, links=links)
    out = os.path.join(DATA_DIR, v["out"])
    json.dump(data, open(out, "w"), separators=(",", ":"))
    st = defaultdict(int)
    for n in nodes:
        st[n["status"]] += 1
    nb = [n["label"] for n in nodes if n["breaking"]]
    print(f"[{v['name']}] {out}: {len(nodes)} nodes, {len(links)} links, {used} stories, status {dict(st)}, "
          f"breaking {len(nb)}{': ' + ', '.join(nb[:8]) if nb else ''}")
    print(f"[{v['name']}] top: " + ", ".join(f"{n['label']}({n['heat']:.1f})" for n in nodes[:12]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--use-cache", action="store_true", help="reuse the last fetch (for UI work)")
    a = ap.parse_args()
    now = time.time()

    if a.use_cache and os.path.exists(CACHE):
        stories = json.load(open(CACHE))
        print(f"Using cached stories: {len(stories)}")
    else:
        stories = fetch_all(7)
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        json.dump(stories, open(CACHE, "w"))

    rec = Recogniser()
    mentions = {s["id"]: rec.find(s) for s in stories}

    # Hacker News covers everything; keep a discussion only if it is about AI: an AI keyword,
    # or an AI-specific entity (not just "Apple"/"Amazon" or a generic topic like "jobs")
    def ai_specific(e):
        return e not in NOT_AI_SPECIFIC and rec.ents[e]["type"] != "Concept" or e in AI_CONCEPTS
    before = len(stories)
    stories = [s for s in stories if s["kind"] != "discussion" or AI_RX.search(s["title"])
               or any(ai_specific(e) for e in mentions[s["id"]])]
    print(f"Kept {len(stories)} of {before} stories after dropping off-topic Hacker News items")
    for sid, extra in llm_extract(stories, rec).items():
        mentions[sid] |= extra

    breaking = breaking_stats(stories, mentions, now)
    hist = json.load(open(HISTORY)) if os.path.exists(HISTORY) else {"runs": [], "entities": {}}
    stamp = iso(now)
    os.makedirs(DATA_DIR, exist_ok=True)
    for v in VIEWS:
        build_view(v, stories, mentions, rec, now, hist, breaking, stamp)
    hist["runs"] = (hist["runs"] + [stamp])[-200:]
    json.dump(hist, open(HISTORY, "w"))


if __name__ == "__main__":
    main()
