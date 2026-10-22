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

# Optional: curl_cffi impersonates a real browser's TLS fingerprint, which gets past TLS/JS
# bot-challenges (e.g. Vercel's "Security Checkpoint" on VentureBeat and O'Reilly) that a plain
# urllib request can't. Only used for feeds in IMPERSONATE_FEEDS; if it's not installed those
# feeds simply fail and are skipped — the rest of the (stdlib-only) pipeline is unaffected.
try:
    from curl_cffi import requests as _cffi
except Exception:
    _cffi = None

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) ai-map/0.1 (+personal project)"}
TIMEOUT = 20

# name, url, weight. Official company blogs get a little extra weight.
RSS_FEEDS = [
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/", 1.0),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", 1.0),
    # VentureBeat is on Vercel behind a TLS/JS bot-challenge; use its MAIN feed (the /category/ai one
    # is stale) via curl_cffi impersonation (see IMPERSONATE_FEEDS) and AI-gate it (all-categories).
    ("VentureBeat AI", "https://venturebeat.com/feed", 1.0),
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
    # AI labs, frameworks and researchers (AI-only feeds, trusted as-is / not gated).
    ("Anthropic", "https://rss.libertas.systems/feeds/anthropic-news.xml", 1.2),  # libertas mirror (plain request; migrated off the bestblogs RSSHub mirror whose /engineering route broke upstream)
    ("Anthropic Research", "https://rss.libertas.systems/feeds/anthropic-research.xml", 1.2),  # anthropic.com/research/ (libertas mirror; plain request)
    ("Anthropic Engineering", "https://rss.libertas.systems/feeds/anthropic-engineering.xml", 1.2),  # the RSSHub /anthropic/engineering route broke upstream (503 across instances) ~2026-10-19; this mirror is fresh (loads on plain request) and the conoro GH-Pages feed is stale since late 2025
    ("Anthropic Red Teaming", "https://raw.githubusercontent.com/0xSMW/rss-feeds/main/feeds/feed_anthropic_red.xml", 1.1),  # Anthropic frontier-safety/red-team + cyber-capability posts; no official RSS, so a generated feed from the 0xSMW/rss-feeds GH repo (personal mirror -> fragile; feed-health will flag if it goes stale). Pipeline dedups by URL so overlap with the news/research feeds is harmless
    ("Thinking Machines", "https://thinkingmachines.ai/index.xml", 1.1),  # Thinking Machines Lab (Mira Murati) official feed; low-frequency (often 0 in the 7-day window until they post)
    ("Microsoft AI Blog", "https://www.microsoft.com/en-us/ai/blog/feed/", 1.0),
    ("Apple Machine Learning", "https://machinelearning.apple.com/rss.xml", 1.0),
    ("PyTorch", "https://pytorch.org/blog/feed.xml", 1.0),
    ("LangChain", "https://blog.langchain.com/rss.xml", 0.9),
    ("Together AI", "https://www.together.ai/blog/rss.xml", 0.8),
    ("Last Week in AI", "https://lastweekin.ai/feed/", 0.9),
    ("Berkeley AI Research", "https://bair.berkeley.edu/blog/feed.xml", 0.9),
    ("Lilian Weng", "https://lilianweng.github.io/posts/index.xml", 0.9),
    ("arXiv cs.AI", "https://export.arxiv.org/rss/cs.AI", 0.4),  # AI-only category; export.* host (rss.* returns empty); firehose -> low weight
    # Broader tech/research sources: not AI-only feeds, so they are AI-relevance gated below.
    ("Google Research", "https://research.google/blog/rss/", 1.1),
    ("Microsoft Research", "https://www.microsoft.com/en-us/research/feed/", 1.0),
    ("Meta Engineering", "https://engineering.fb.com/feed/", 1.0),
    ("The Information", "https://www.theinformation.com/feed", 1.0),
    ("Semafor", "https://www.semafor.com/rss.xml", 0.9),
    ("Platformer", "https://www.platformer.news/rss/", 0.9),
    ("SemiAnalysis", "https://semianalysis.com/feed/", 1.0),  # public RSS = free posts only (mostly paywalled); low volume
    # AI-scoped feeds from a curated public list (danbarbarito gist), validated live 2026-10-20.
    # These are AI-topic feeds (or AI-only blogs), so trusted as-is like the other AI-category feeds.
    ("The Register AI", "https://www.theregister.com/software/ai_ml/headlines.atom", 1.0),
    ("IEEE Spectrum AI", "https://spectrum.ieee.org/feeds/topic/artificial-intelligence.rss", 1.0),
    ("The Guardian AI", "https://www.theguardian.com/technology/artificialintelligenceai/rss", 1.0),
    ("ZDNET AI", "https://www.zdnet.com/topic/artificial-intelligence/rss.xml", 0.9),
    ("TechXplore AI", "https://techxplore.com/rss-feed/machine-learning-ai-news/", 0.9),
    ("SiliconANGLE AI", "https://siliconangle.com/category/ai/feed", 0.8),
    ("InfoQ AI/ML", "https://feed.infoq.com/ai-ml-data-eng/", 0.9),
    ("KDnuggets", "https://www.kdnuggets.com/feed", 0.8),
    ("MarkTechPost", "https://www.marktechpost.com/feed", 0.6),  # high-volume AI-news site -> capped + low weight
    ("MIT News ML", "https://news.mit.edu/topic/mitmachine-learning-rss.xml", 1.0),
    ("Nature ML", "https://www.nature.com/subjects/machine-learning.rss", 0.9),
    ("NVIDIA Technical Blog", "https://developer.nvidia.com/blog/feed", 1.0),
    ("Latent Space", "https://www.latent.space/feed", 1.0),
    ("Interconnects", "https://www.interconnects.ai/feed", 1.0),
    ("Ahead of AI", "https://magazine.sebastianraschka.com/feed", 1.0),
    ("AI Snake Oil", "https://aisnakeoil.substack.com/feed", 0.9),
    ("The Algorithmic Bridge", "https://thealgorithmicbridge.substack.com/feed", 0.8),
    ("TheSequence", "https://thesequence.substack.com/feed", 0.8),
    ("One Useful Thing", "https://www.oneusefulthing.org/feed", 0.9),
    # Second pass over the danbarbarito gist, validated live 2026-10-21 (fresh + parses).
    ("DeepMind Blog", "https://deepmind.com/blog/feed/basic/", 1.2),  # old URL still redirects to live deepmind.google/blog; major lab, AI-only
    ("AI Business", "https://aibusiness.com/rss.xml", 0.8),  # AI enterprise/trade news; AI-only site
    ("AI Now Institute", "https://ainowinstitute.org/category/news/feed", 0.8),  # AI policy/ethics/governance
    ("Max Woolf", "https://minimaxir.com/post/index.xml", 0.9),  # quality individual ML blog, low volume
    # AI in biotech/health/pharma -- a major AI application area. Validated live 2026-10-21.
    ("NEJM AI", "https://ai.nejm.org/action/showFeed?type=etoc&feed=rss&jc=ai", 1.2),  # NEJM's AI journal (RSS 1.0/RDF); AI-specific, trusted
    ("Lancet Digital Health", "https://www.thelancet.com/rssfeed/landig_online.xml", 1.1),  # Lancet journal (RSS 1.0/RDF); digital health -> AI-gated
    ("JMIR AI", "https://ai.jmir.org/feed", 1.0),  # peer-reviewed AI-in-medicine journal; AI-only
    ("Endpoints News", "https://endpoints.news/feed/", 0.9),  # biopharma/drug-R&D news (needs curl_cffi) -> AI-gated
    ("STAT Health Tech", "https://www.statnews.com/category/health-tech/feed", 0.9),  # authoritative health/biotech journalism -> AI-gated
    # AI in banking/insurance/fintech -- a top enterprise-AI spend vertical. Validated live
    # 2026-10-21. All general fintech/insurance outlets -> AI-gated (only AI stories pass).
    ("Finextra", "https://www.finextra.com/rss/headlines.aspx", 0.9),  # authoritative fintech news, high volume
    ("Bank Automation News", "https://bankautomationnews.com/feed/", 0.8),  # AI/automation in banking (needs curl_cffi)
    ("Tearsheet", "https://tearsheet.co/feed/", 0.7),  # fintech/banking
    ("Finovate", "https://finovate.com/feed/", 0.7),  # fintech
    ("Coverager", "https://coverager.com/feed/", 0.7),  # insurtech/insurance news
    ("Insurance Innovation Reporter", "https://iireporter.com/feed/", 0.7),  # insurance technology
    # Reputable but NOT AI-only (general science/tech/vendor) -> AI-relevance gated below.
    ("Quanta Magazine", "https://api.quantamagazine.org/feed", 0.9),
    ("404 Media", "https://www.404media.co/rss", 0.9),
    ("Databricks", "https://www.databricks.com/feed", 0.7),
    # AI labs with no native RSS, via the Alan Turing Institute's scraped-feed repo
    # (alan-turing-institute/ai-rss-feeds). Validated live 2026-10-20; links are absolute.
    # Fills real gaps (we had no Mistral/xAI/Cohere/Ai2/Mila feed). Trusted as-is (lab feeds).
    ("Mistral News", "https://raw.githubusercontent.com/alan-turing-institute/ai-rss-feeds/refs/heads/main/feeds/mistral-news.xml", 1.2),
    ("xAI", "https://raw.githubusercontent.com/alan-turing-institute/ai-rss-feeds/refs/heads/main/feeds/spacex-ai-news.xml", 1.2),  # repo mislabels x.ai as "SpaceX"
    ("Cohere Blog", "https://raw.githubusercontent.com/alan-turing-institute/ai-rss-feeds/refs/heads/main/feeds/cohere-blog.xml", 1.0),
    ("Ai2 (Allen Institute)", "https://raw.githubusercontent.com/alan-turing-institute/ai-rss-feeds/refs/heads/main/feeds/allenai-news.xml", 1.0),
    ("Mila", "https://raw.githubusercontent.com/alan-turing-institute/ai-rss-feeds/refs/heads/main/feeds/mila-news.xml", 0.9),
    ("AISI Blog", "https://raw.githubusercontent.com/alan-turing-institute/ai-rss-feeds/refs/heads/main/feeds/aisi-blog.xml", 0.9),
    # Curated from Feedspot's "Top 100 AI RSS Feeds", validated live 2026-10-20. Mostly fills an
    # AI policy / safety / governance gap, plus a few reputable AI-scoped news feeds. Trusted as-is
    # (AI-only orgs or AI-topic-scoped URLs) except CSET, which is gated below.
    ("MIRI", "https://intelligence.org/feed/", 0.7),  # AI alignment research (Machine Intelligence Research Institute)
    ("Redwood Research", "https://blog.redwoodresearch.org/feed", 0.8),  # AI alignment lab
    ("Transformer News", "https://www.transformernews.ai/feed", 0.9),  # AI news + policy
    ("AIhub", "https://aihub.org/feed/?cat=-473", 0.9),  # non-profit AI research community
    ("The Conversation AI", "https://theconversation.com/topics/artificial-intelligence-ai-90/articles.atom", 0.8),
    ("Crunchbase News AI", "https://news.crunchbase.com/sections/ai/feed/", 0.8),  # AI startup/funding
    ("GeekWire AI", "https://www.geekwire.com/tag/ai/feed/", 0.8),
    ("Fast Company AI", "https://www.fastcompany.com/section/artificial-intelligence/rss", 0.8),
    ("NYT AI", "https://www.nytimes.com/svc/collections/v1/publish/https://www.nytimes.com/spotlight/artificial-intelligence/rss.xml", 0.9),
    ("CSET", "https://cset.georgetown.edu/feed/", 0.8),  # Georgetown AI/emerging-tech policy; org-wide feed -> AI-gated
    # China AI — a real coverage gap. Dedicated China-AI analysis is trusted as-is; broader
    # China tech/policy outlets are AI-relevance gated below. Validated live 2026-10-20.
    ("ChinAI", "https://chinai.substack.com/feed", 1.0),  # Jeff Ding: weekly translations/analysis of China's AI
    ("Recode China AI", "https://recodechinaai.substack.com/feed", 0.9),  # dedicated China-AI newsletter
    ("ChinaTalk", "https://www.chinatalk.media/feed", 0.9),  # US-China tech/policy
    ("Interconnected", "https://interconnected.blog/rss/", 0.8),  # Kevin Xu: bilingual US-China tech
    ("SCMP Tech", "https://www.scmp.com/rss/36/feed", 0.9),  # South China Morning Post tech section (AI-topic feed is 404)
    ("TechNode", "https://technode.com/feed/", 0.7),  # China startup/tech news (high volume -> capped)
    ("Pandaily", "https://pandaily.com/feed/", 0.7),  # Beijing tech news
    ("Import AI", "https://importai.substack.com/feed", 1.0),  # Jack Clark (Anthropic co-founder): weekly AI research + policy
    ("Data Center Knowledge", "https://www.datacenterknowledge.com/rss.xml", 0.8),  # authoritative DC trade press; AI-gated (hyperscale/GPU/power-for-AI)
    # AI-chip / semiconductor trade press (AI-gated): feeds the "AI chips" concept (FPGA, ASIC, NPU,
    # wafer-scale, chiplets, accelerators) and the "Export controls" concept.
    ("The Next Platform", "https://www.nextplatform.com/feed/", 0.9),  # AI/HPC datacenter silicon (high volume -> capped)
    ("IEEE Spectrum Semiconductors", "https://spectrum.ieee.org/feeds/topic/semiconductors.rss", 0.9),
    ("EE Times", "https://www.eetimes.com/feed/", 0.8),
    ("Semiconductor Engineering", "https://semiengineering.com/feed/", 0.8),  # chip design / EDA / ASIC / chiplet depth
    ("ServeTheHome", "https://www.servethehome.com/feed/", 0.8),  # server / AI accelerator hardware
    ("Tom's Hardware", "https://www.tomshardware.com/feeds/all", 0.7),  # consumer/AI hardware (high volume -> capped)
    ("SemiWiki", "https://semiwiki.com/feed/", 0.7),  # semiconductor industry / IP / design
    # O'Reilly Radar: oreilly.com put the radar feeds behind Akamai bot management (403s even via
    # curl_cffi impersonation) on 2026-10-21. The FeedBurner mirror serves the same content, is
    # fresh, and loads on a plain request (no challenge). This is the full radar feed (not ai-ml
    # only), so it's AI-gated below.
    ("O'Reilly Radar", "https://feeds.feedburner.com/oreilly/radar/atom", 1.0),
    # Slashdot has no AI-only feed; the IT section (RSS 1.0/RDF) has the best AI density, so take it
    # AI-gated. Expect heavy overlap with HN + the tech feeds (pipeline dedups by URL).
    ("Slashdot IT", "https://rss.slashdot.org/Slashdot/slashdotIt", 0.7),
    # Local-LLM inference/offloading projects — GitHub releases.atom. Trusted (AI-only), not gated.
    # fetch_rss prefixes the project name to each item (see GITHUB_RELEASE_FEEDS above).
    ("vLLM (releases)", "https://github.com/vllm-project/vllm/releases.atom", 0.7),
    ("KTransformers (releases)", "https://github.com/kvcache-ai/ktransformers/releases.atom", 0.8),
    ("AirLLM (releases)", "https://github.com/lyogavin/airllm/releases.atom", 0.8),
    ("MLC-LLM (releases)", "https://github.com/mlc-ai/mlc-llm/releases.atom", 0.7),
    ("llamafile (releases)", "https://github.com/Mozilla-Ocho/llamafile/releases.atom", 0.7),
    ("LMDeploy (releases)", "https://github.com/InternLM/lmdeploy/releases.atom", 0.7),
    ("TensorRT-LLM (releases)", "https://github.com/NVIDIA/TensorRT-LLM/releases.atom", 0.7),
    ("Strata (releases)", "https://github.com/niko1221/strata/releases.atom", 0.8),
    ("Project Maya (releases)", "https://github.com/mw00/project-maya/releases.atom", 0.8),
]

# Feeds that are NOT AI-only (general tech/research/news). Stories from these are kept only
# if they look AI-relevant (AI keyword or an AI-specific entity), the same gate used for
# Hacker News. The AI-category feeds above are trusted as-is and never filtered.
AI_FILTER_SOURCES = {
    "Google Research", "Microsoft Research", "Meta Engineering",
    "The Information", "Semafor", "Platformer", "SemiAnalysis",
    "Quanta Magazine", "404 Media", "Databricks", "CSET",
    "ChinaTalk", "Interconnected", "SCMP Tech", "TechNode", "Pandaily",
    "Data Center Knowledge", "VentureBeat AI", "O'Reilly Radar",
    "Lancet Digital Health", "Endpoints News", "STAT Health Tech",
    "Finextra", "Bank Automation News", "Tearsheet", "Finovate",
    "Coverager", "Insurance Innovation Reporter", "Slashdot IT",
    "The Next Platform", "IEEE Spectrum Semiconductors", "EE Times",
    "Semiconductor Engineering", "ServeTheHome", "Tom's Hardware", "SemiWiki",
    "NVIDIA Blog",  # whole-blog feed: gate out GeForce/automotive/Omniverse non-AI posts
}

# Per-feed caps: keep only the N most recent items from a firehose source so it can't
# flood the map/archive. arXiv cs.AI announces ~300 papers/day.
FEED_CAPS = {
    "arXiv cs.AI": 50,
    "MarkTechPost": 20,  # publishes many AI posts/day; cap so it can't flood the window
    "TechNode": 40,  # ~2000-item firehose of general China tech; cap after the AI-relevance gate
    "The Next Platform": 30,  # high-volume AI/HPC hardware feed; cap after the AI-relevance gate
    "Tom's Hardware": 25,  # high-volume consumer hardware; cap after the AI-relevance gate
}

# Feeds behind a TLS/JS bot-challenge (e.g. Vercel's "Security Checkpoint") that a plain urllib
# request can't pass; fetched via curl_cffi browser impersonation instead. Needs curl_cffi
# installed (optional); without it these feeds are simply skipped.
IMPERSONATE_FEEDS = {"VentureBeat AI", "Endpoints News", "Bank Automation News"}

# The pipeline runs on a fast cadence (every ~2 min) so Hacker News — the high-churn, high-AI
# source, fetched unconditionally below — stays near real-time. Everything else is throttled so it
# is only actually hit about once an hour: between live fetches the last good result is reused (and
# served as a fallback on failure). DEFAULT_FEED_INTERVAL_HOURS applies to every RSS feed and to
# the HF/GitHub APIs; FEED_INTERVAL_HOURS overrides it per feed (longer for sources that rate-limit
# or block us — see the feed-health history).
DEFAULT_FEED_INTERVAL_HOURS = 1
FEED_INTERVAL_HOURS = {
    "VentureBeat AI": 2,   # hard 429 rate-limiter (per-IP), even with browser impersonation
    "KDnuggets": 2,        # intermittent 403 WAF block
    "SemiAnalysis": 3,     # mostly-paywalled, intermittent 403; low value
    "TechNode": 2,         # ~2000-item firehose of general China tech
}
FEED_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "feed-cache")

# GitHub releases.atom feeds for key local-LLM inference/offloading projects. Release items have bare
# version titles ("v0.32.1"), so fetch_rss prefixes the project name (the value here) to each item —
# otherwise the story carries no recognisable entity. The project name matches an alias in the entity
# dictionary (entities.py SEED), so these releases add heat to the project's node + the offloading concept.
GITHUB_RELEASE_FEEDS = {
    "vLLM (releases)": "vLLM",
    "KTransformers (releases)": "KTransformers",
    "AirLLM (releases)": "AirLLM",
    "MLC-LLM (releases)": "MLC-LLM",
    "llamafile (releases)": "llamafile",
    "LMDeploy (releases)": "LMDeploy",
    "TensorRT-LLM (releases)": "TensorRT-LLM",
    "Strata (releases)": "Strata",
    "Project Maya (releases)": "Project Maya",
}

GITHUB_TOPICS = ["llm", "ai-agents", "agents", "generative-ai", "mcp", "large-language-models", "rag", "ai",
                 # inference-optimisation / local-LLM discovery: surface the next ktransformers/PowerInfer
                 "llm-inference", "inference", "llm-serving", "quantization", "local-llm", "on-device-ai", "gguf"]


def _get(url, headers=None, impersonate=False):
    if impersonate and _cffi is not None:
        r = _cffi.get(url, impersonate="chrome", timeout=TIMEOUT, headers=headers or None)
        r.raise_for_status()  # a challenge/error (e.g. 429/403) raises -> caller logs & skips
        return r.content
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
    now_ts = time.time()
    try:
        root = ET.fromstring(_get(url, impersonate=name in IMPERSONATE_FEEDS))
    except Exception as e:
        log(f"  ! {name}: {str(e)[:80]}")
        return out
    ns = {"a": "http://www.w3.org/2005/Atom"}
    rss1 = "{http://purl.org/rss/1.0/}"  # RSS 1.0 / RDF (e.g. NEJM AI, Lancet Digital Health)
    dc = "{http://purl.org/dc/elements/1.1/}"
    # Pick the item flavour: RSS 2.0 <item>, then RSS 1.0/RDF namespaced <item>, then Atom <entry>.
    items = root.findall(".//item")
    fmt = "rss2"
    if not items:
        items = root.findall(f".//{rss1}item")
        if items:
            fmt = "rss1"
        else:
            items = root.findall(".//a:entry", ns)
            fmt = "atom"
    for it in items:
        if fmt == "atom":
            title = (it.findtext("a:title", "", ns) or "").strip()
            link_el = it.find("a:link[@rel='alternate']", ns)
            if link_el is None:  # (Element truthiness means "has children", so no `or` here)
                link_el = it.find("a:link", ns)
            link = link_el.get("href") if link_el is not None else ""
            date = _parse_date(it.findtext("a:published", "", ns) or it.findtext("a:updated", "", ns))
            summary = it.findtext("a:summary", "", ns) or it.findtext("a:content", "", ns)
        elif fmt == "rss1":
            title = (it.findtext(f"{rss1}title") or "").strip()
            link = (it.findtext(f"{rss1}link") or "").strip()
            date = _parse_date(it.findtext(f"{dc}date") or it.findtext(f"{rss1}pubDate"))
            summary = it.findtext(f"{rss1}description") or it.findtext(f"{dc}description") or ""
        else:
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            date = _parse_date(it.findtext("pubDate") or it.findtext(f"{dc}date"))
            summary = it.findtext("description") or ""
        if not title or not date or date < since:
            continue
        # A future pubDate (e.g. a scraper that only knows the month and stamps it to the last day
        # of the month) must not amplify heat or read as "just now". Clamp it and flag it so the
        # build can substitute the real first-seen time from the archive DB.
        imprecise = date > now_ts
        date = min(date, now_ts)
        disp = _clean(title)
        proj = GITHUB_RELEASE_FEEDS.get(name)
        if proj:
            # Release items are bare version tags ("v0.32.1"); prefix the project so the story is
            # attributable (heat + entity extraction read the text, not the source name).
            if proj.lower() not in disp.lower():
                disp = f"{proj} {disp}".strip()
            text = f"{proj} release. {disp}. {_clean(summary)[:400]}"
        else:
            text = disp + ". " + _clean(summary)[:400]
        out.append(dict(id=link or title, title=disp, url=link, source=name, kind="news",
                        published=date, engagement=0, weight=weight, date_imprecise=imprecise,
                        text=text))
    cap = FEED_CAPS.get(name)
    if cap and len(out) > cap:
        out.sort(key=lambda s: s["published"], reverse=True)  # keep the most recent
        out = out[:cap]
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


def _fetch_rss_throttled(name, url, weight, since, interval_h, log):
    """Fetch at most once per `interval_h`, reusing the cached result in between (and as a
    fallback if a live fetch fails/returns nothing). Keeps a rate-limited feed in the map
    without hitting it every hourly run."""
    os.makedirs(FEED_CACHE_DIR, exist_ok=True)
    path = os.path.join(FEED_CACHE_DIR, re.sub(r"[^A-Za-z0-9]+", "_", name) + ".json")
    cache = None
    if os.path.exists(path):
        try:
            cache = json.load(open(path))
        except Exception:
            cache = None
    # Reuse the cache (no network) until `interval_h` has passed since the last *attempt*, so a
    # feed that keeps failing (e.g. a persistent 429) is still only hit on that slower cadence.
    if cache and (time.time() - cache.get("ts", 0)) < interval_h * 3600:
        fresh = [s for s in cache.get("stories", []) if s.get("published", 0) >= since]
        due_in = interval_h - (time.time() - cache["ts"]) / 3600
        log(f"  {name}: {len(fresh)} stories (cached; next fetch in {due_in:.1f} h)")
        return fresh
    out = fetch_rss(name, url, weight, since, log)
    stories = out if out else (cache.get("stories", []) if cache else [])
    try:  # stamp the attempt time (success or not) so the interval backoff holds
        json.dump({"ts": time.time(), "stories": stories}, open(path, "w"))
    except Exception as e:
        log(f"  ! {name}: cache write failed: {str(e)[:60]}")
    fresh = [s for s in stories if s.get("published", 0) >= since]
    if not out and cache:  # live fetch failed/empty: fell back to the last good result
        log(f"  {name}: {len(fresh)} stories (stale cache; live fetch returned nothing)")
    return fresh


def _throttled(key, interval_h, since, fn, log):
    """Throttle any fetcher (HF/GitHub) the same way _fetch_rss_throttled does for RSS: call `fn`
    at most once per `interval_h`, reusing the cached result (filtered to the window) in between
    and as a fallback on failure. Keeps HN fast while these APIs are only hit ~hourly."""
    os.makedirs(FEED_CACHE_DIR, exist_ok=True)
    path = os.path.join(FEED_CACHE_DIR, "api_" + re.sub(r"[^A-Za-z0-9]+", "_", key) + ".json")
    cache = None
    if os.path.exists(path):
        try:
            cache = json.load(open(path))
        except Exception:
            cache = None
    in_window = lambda ss: [s for s in ss if s.get("published") is None or s.get("published", 0) >= since]
    if cache and (time.time() - cache.get("ts", 0)) < interval_h * 3600:
        fresh = in_window(cache.get("stories", []))
        due_in = interval_h - (time.time() - cache["ts"]) / 3600
        log(f"  {key}: {len(fresh)} (cached; next fetch in {due_in:.1f} h)")
        return fresh
    out = fn()
    stories = out if out else (cache.get("stories", []) if cache else [])
    try:  # stamp the attempt time (success or not) so the interval backoff holds
        json.dump({"ts": time.time(), "stories": stories}, open(path, "w"))
    except Exception as e:
        log(f"  ! {key}: cache write failed: {str(e)[:60]}")
    return in_window(stories)


def fetch_all(window_days=7, log=print):
    now = time.time()
    since = now - window_days * 86400
    stories = []
    log("Fetching sources...")
    stories += fetch_hn(since, log)  # fast lane: fetched every run (near real-time)
    for name, url, w in RSS_FEEDS:
        iv = FEED_INTERVAL_HOURS.get(name, DEFAULT_FEED_INTERVAL_HOURS)
        if iv:
            stories += _fetch_rss_throttled(name, url, w, since, iv, log)
        else:
            stories += fetch_rss(name, url, w, since, log)
    stories += _throttled("HF papers", DEFAULT_FEED_INTERVAL_HOURS, since, lambda: fetch_hf_papers(since, log), log)
    stories += _throttled("HF models", DEFAULT_FEED_INTERVAL_HOURS, since, lambda: fetch_hf_models(since, log, now), log)
    stories += _throttled("GitHub", DEFAULT_FEED_INTERVAL_HOURS, since, lambda: fetch_github(since, log), log)
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
