"""Build the AI Map graph: fetch -> recognise entities -> score heat -> write web/data/data.json.

Heat model
----------
Each story contributes  weight(source) x engagement_boost x 0.5 ** (age_hours / HALF_LIFE)
to every entity it mentions, so attention "cools" with a configurable half-life (default 24 h,
over a 7-day window). An entity's heat 24 h ago is computed the same way from the stories that
existed then, which gives a trend without needing a previous run.

Status:  new     = first seen within the last 7 days (tracked in data/history.json)
         rising  = heat at least 1.5x what it was 24 h ago
         steady  = everything else
Links:   two entities are linked when they appear in the same stories (decayed weight),
         plus static parent links (e.g. ChatGPT -> OpenAI).
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
OUT = os.path.join(ROOT, "web", "data", "data.json")
HISTORY = os.path.join(ROOT, "data", "history.json")
CACHE = os.path.join(ROOT, "data", "stories-cache.json")

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=float, default=7)
    ap.add_argument("--half-life", type=float, default=float(os.environ.get("AIMAP_HALF_LIFE_HOURS", 24)))
    ap.add_argument("--max-nodes", type=int, default=140)
    ap.add_argument("--min-stories", type=int, default=2)
    ap.add_argument("--use-cache", action="store_true", help="reuse the last fetch (for UI work)")
    a = ap.parse_args()
    now = time.time()

    if a.use_cache and os.path.exists(CACHE):
        stories = json.load(open(CACHE))
        print(f"Using cached stories: {len(stories)}")
    else:
        stories = fetch_all(a.window_days)
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

    heat, heat_prev = defaultdict(float), defaultdict(float)
    count, by_source, ent_stories = defaultdict(int), defaultdict(lambda: defaultdict(int)), defaultdict(list)
    first_mention = {}
    pair = defaultdict(float)
    for s in stories:
        ents = mentions.get(s["id"]) or set()
        if not ents:
            continue
        age_h = (now - s["published"]) / 3600
        w = s["weight"] * boost(s)
        contrib = w * decay(age_h, a.half_life)
        if s["kind"] in ("model", "repo"):
            contrib_prev = contrib  # "currently trending" lists have no history: treat as a steady baseline
        else:
            contrib_prev = w * decay(age_h - 24, a.half_life) if age_h >= 24 else 0.0
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

    # choose nodes: enough distinct stories, highest heat
    cands = [e for e in heat if count[e] >= a.min_stories or rec.ents[e]["type"] == "Open source" and heat[e] > 1.5]
    cands.sort(key=lambda e: -heat[e])
    nodes_set = set(cands[: a.max_nodes])

    # history: first-seen dates + rolling heat series
    hist = json.load(open(HISTORY)) if os.path.exists(HISTORY) else {"runs": [], "entities": {}}
    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="minutes")
    for e in nodes_set:
        h = hist["entities"].setdefault(e, {"label": rec.ents[e]["label"],
                                            "first_seen": datetime.fromtimestamp(first_mention[e], timezone.utc).isoformat(timespec="minutes"),
                                            "series": []})
        h["series"] = (h["series"] + [[stamp, round(heat[e], 2)]])[-28:]  # ~2 weeks at 2 runs/day
    hist["runs"] = (hist["runs"] + [stamp])[-60:]

    top = max((heat[e] for e in nodes_set), default=1)
    nodes = []
    for e in sorted(nodes_set, key=lambda x: -heat[x]):
        info = rec.ents[e]
        fs = datetime.fromisoformat(hist["entities"][e]["first_seen"]).timestamp()
        # "new" = not in the curated dictionary and first seen in the last 7 days
        is_new = (not info["seed"]) and (now - fs) < 7 * 86400
        prev = heat_prev[e]
        trend = (heat[e] / prev) if prev > 0.05 else (9.9 if heat[e] > 0.5 else 1.0)
        status = "new" if is_new else ("rising" if trend >= 1.5 and heat[e] >= 1.0 else "steady")
        best = sorted(ent_stories[e], key=lambda x: -x[0])[:8]
        nodes.append(dict(
            id=e, label=info["label"], type=info["type"], parent=info["parent"],
            heat=round(heat[e], 3), heat_norm=round(heat[e] / top, 4), trend=round(min(trend, 9.9), 2),
            status=status, stories=count[e], first_seen=hist["entities"][e]["first_seen"],
            sources=dict(sorted(by_source[e].items(), key=lambda x: -x[1])),
            series=hist["entities"][e]["series"][-14:],
            top_stories=[dict(title=s["title"], url=s["url"], source=s["source"],
                              published=datetime.fromtimestamp(s["published"], timezone.utc).isoformat(timespec="minutes"),
                              discussion=s.get("discussion")) for _, s in best]))

    # links: strongest co-mentions per node + parent links
    per = defaultdict(list)
    for (x, y), w in pair.items():
        if x in nodes_set and y in nodes_set and w > 0.15:
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
            keep[k] = max(keep.get(k, 0), 0.5)
    wmax = max(keep.values(), default=1)
    links = [dict(source=x, target=y, weight=round(w / wmax, 4)) for (x, y), w in keep.items()]

    srcs = defaultdict(int)
    for s in stories:
        srcs[s["source"]] += 1
    data = dict(generated_at=stamp, window_days=a.window_days, half_life_hours=a.half_life,
                story_count=len(stories), sources=dict(sorted(srcs.items(), key=lambda x: -x[1])),
                nodes=nodes, links=links)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(data, open(OUT, "w"), separators=(",", ":"))
    json.dump(hist, open(HISTORY, "w"))
    st = defaultdict(int)
    for n in nodes:
        st[n["status"]] += 1
    print(f"Wrote {OUT}: {len(nodes)} nodes, {len(links)} links, status {dict(st)}")
    print("Top 15:", ", ".join(f"{n['label']}({n['heat']:.1f})" for n in nodes[:15]))


if __name__ == "__main__":
    main()
