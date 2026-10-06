"""Fetch English AI news and trending projects from public sources.

Every fetcher returns a list of story dicts:
    {id, title, url, source, kind, published (unix seconds), engagement, text}
`engagement` is a raw popularity number (points, upvotes, stars...) or 0.
Fetchers never raise: a failing source is logged and skipped.
"""
import email.utils
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) ai-map/0.1 (+personal project)"}
TIMEOUT = 20

# name, url, weight. Official company blogs get a little extra weight.
RSS_FEEDS = [
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/", 1.0),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", 1.0),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/", 1.0),
    ("MIT Technology Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed", 1.0),
    ("Ars Technica AI", "https://arstechnica.com/ai/feed/", 1.0),
    ("Wired AI", "https://www.wired.com/feed/tag/ai/latest/rss", 1.0),
    ("The Decoder", "https://the-decoder.com/feed/", 1.0),
    ("Simon Willison", "https://simonwillison.net/atom/everything/", 0.9),
    ("OpenAI News", "https://openai.com/news/rss.xml", 1.2),
    ("Google AI Blog", "https://blog.google/technology/ai/rss/", 1.2),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml", 1.2),
    ("NVIDIA Blog", "https://blogs.nvidia.com/feed/", 1.1),
    ("Hugging Face Blog", "https://huggingface.co/blog/feed.xml", 1.1),
    ("AWS Machine Learning", "https://aws.amazon.com/blogs/machine-learning/feed/", 0.9),
]

GITHUB_TOPICS = ["llm", "ai-agents", "agents", "generative-ai", "mcp", "large-language-models", "rag", "ai"]


def _get(url, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
            import gzip
            data = gzip.decompress(data)
        return data


def _clean(html):
    txt = re.sub(r"<[^>]+>", " ", html or "")
    txt = re.sub(r"&[#a-z0-9]+;", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def _parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        return email.utils.parsedate_to_datetime(s).timestamp()
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def fetch_rss(name, url, weight, since, log):
    out = []
    try:
        root = ET.fromstring(_get(url))
    except Exception as e:
        log(f"  ! {name}: {str(e)[:80]}")
        return out
    ns = {"a": "http://www.w3.org/2005/Atom"}
    items = root.findall(".//item")
    atom = not items
    if atom:
        items = root.findall(".//a:entry", ns)
    for it in items:
        if atom:
            title = (it.findtext("a:title", "", ns) or "").strip()
            link_el = it.find("a:link[@rel='alternate']", ns)
            if link_el is None:  # (Element truthiness means "has children", so no `or` here)
                link_el = it.find("a:link", ns)
            link = link_el.get("href") if link_el is not None else ""
            date = _parse_date(it.findtext("a:published", "", ns) or it.findtext("a:updated", "", ns))
            summary = it.findtext("a:summary", "", ns) or it.findtext("a:content", "", ns)
        else:
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            date = _parse_date(it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date"))
            summary = it.findtext("description") or ""
        if not title or not date or date < since:
            continue
        out.append(dict(id=link or title, title=_clean(title), url=link, source=name, kind="news",
                        published=date, engagement=0, weight=weight,
                        text=_clean(title) + ". " + _clean(summary)[:400]))
    log(f"  {name}: {len(out)} stories")
    return out


def fetch_hn(since, log, min_points=30, early_points=10, early_hours=3):
    """Stories above `min_points`, plus fresh ones (< early_hours old) above `early_points`,
    so a breakout is caught while it's still climbing."""
    out, page = [], 0
    now = time.time()
    while page < 5:
        q = urllib.parse.urlencode({"tags": "story", "hitsPerPage": 1000, "page": page,
                                    "numericFilters": f"created_at_i>{int(since)},points>{early_points}"})
        try:
            data = json.loads(_get("https://hn.algolia.com/api/v1/search_by_date?" + q))
        except Exception as e:
            log(f"  ! Hacker News: {str(e)[:80]}")
            break
        for h in data.get("hits", []):
            pts, created = h.get("points") or 0, h.get("created_at_i", 0)
            if pts <= min_points and now - created > early_hours * 3600:
                continue
            title = h.get("title") or ""
            url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
            out.append(dict(id=f"hn:{h.get('objectID')}", title=title, url=url, source="Hacker News",
                            kind="discussion", published=h.get("created_at_i", 0),
                            engagement=h.get("points") or 0, weight=1.0, text=title,
                            discussion=f"https://news.ycombinator.com/item?id={h.get('objectID')}"))
        page += 1
        if page >= data.get("nbPages", 0):
            break
    log(f"  Hacker News: {len(out)} stories (>{min_points} points, or >{early_points} if under {early_hours} h old)")
    return out


def fetch_hf_papers(since, log):
    out = []
    try:
        data = json.loads(_get("https://huggingface.co/api/daily_papers?limit=100"))
    except Exception as e:
        log(f"  ! HF papers: {str(e)[:80]}")
        return out
    for d in data:
        p = d.get("paper", {})
        date = _parse_date(d.get("publishedAt") or p.get("publishedAt"))
        if not date or date < since:
            continue
        title = p.get("title") or d.get("title") or ""
        out.append(dict(id=f"hfp:{p.get('id')}", title=title, url=f"https://huggingface.co/papers/{p.get('id')}",
                        source="Hugging Face Papers", kind="paper", published=date,
                        engagement=p.get("upvotes") or 0, weight=0.6,
                        text=title + ". " + (p.get("summary") or "")[:400]))
    log(f"  Hugging Face Papers: {len(out)} papers")
    return out


def fetch_hf_models(since, log, now):
    out = []
    try:
        data = json.loads(_get("https://huggingface.co/api/models?sort=trendingScore&direction=-1&limit=60"))
    except Exception as e:
        log(f"  ! HF models: {str(e)[:80]}")
        return out
    for m in data:
        mid = m.get("id") or m.get("modelId") or ""
        if "/" not in mid:
            continue
        # trending models are "current", so date them now
        out.append(dict(id=f"hfm:{mid}", title=f"Trending on Hugging Face: {mid}", url=f"https://huggingface.co/{mid}",
                        source="Hugging Face Trending", kind="model", published=now,
                        engagement=m.get("trendingScore") or m.get("likes") or 0, weight=0.35,
                        text=mid.replace("/", " ").replace("-", " "), hf_model=mid))
    log(f"  Hugging Face trending models: {len(out)}")
    return out


def fetch_github(since, log):
    out, seen = [], set()
    created = datetime.fromtimestamp(since - 23 * 86400, timezone.utc).strftime("%Y-%m-%d")  # repos < 30 days old
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    hdr = {"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {tok}"} if tok else {})}
    for topic in GITHUB_TOPICS:
        q = urllib.parse.urlencode({"q": f"topic:{topic} created:>{created}", "sort": "stars", "order": "desc", "per_page": 30})
        try:
            data = json.loads(_get("https://api.github.com/search/repositories?" + q, hdr))
        except Exception as e:
            log(f"  ! GitHub topic:{topic}: {str(e)[:80]}")
            continue
        for r in data.get("items", []):
            if r["full_name"] in seen or r.get("stargazers_count", 0) < 100:
                continue
            seen.add(r["full_name"])
            date = _parse_date(r.get("pushed_at")) or time.time()
            if date < since:  # not active inside the window
                continue
            desc = r.get("description") or ""
            out.append(dict(id=f"gh:{r['full_name']}", title=f"{r['full_name']}: {desc}"[:200], url=r["html_url"],
                            source="GitHub (new repos)", kind="repo", published=min(date, time.time()),
                            engagement=r.get("stargazers_count", 0), weight=0.5,
                            text=f"{r['name']} {desc}", gh_repo=r["full_name"]))
        time.sleep(1.5 if tok else 7)  # unauthenticated search allows 10 requests/minute
    log(f"  GitHub new AI repos: {len(out)}")
    return out


def fetch_all(window_days=7, log=print):
    now = time.time()
    since = now - window_days * 86400
    stories = []
    log("Fetching sources...")
    stories += fetch_hn(since, log)
    for name, url, w in RSS_FEEDS:
        stories += fetch_rss(name, url, w, since, log)
    stories += fetch_hf_papers(since, log)
    stories += fetch_hf_models(since, log, now)
    stories += fetch_github(since, log)
    # de-duplicate by URL (same article posted to HN and a feed counts once per source)
    seen, uniq = set(), []
    for s in stories:
        key = (s["source"], s["url"] or s["title"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(s)
    log(f"Total stories in window: {len(uniq)}")
    return uniq
