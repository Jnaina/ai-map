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
    ("METR", "https://metr.org/feed.xml", 1.0),  # AI evaluations / dangerous-capability + model-autonomy research (AI-only org, trusted); validated live 2026-10-27
    ("Air Street Press", "https://press.airstreet.com/feed", 0.8),  # Nathan Benaich; author of the annual State of AI Report (AI-only, low volume, trusted); validated live 2026-10-27
    ("arXiv cs.AI", "https://export.arxiv.org/rss/cs.AI", 0.4),  # AI-only category; export.* host (rss.* returns empty); firehose -> low weight
    # Broader tech/research sources: not AI-only feeds, so they are AI-relevance gated below.
    ("Google Research", "https://research.google/blog/rss/", 1.1),
    ("Microsoft Research", "https://www.microsoft.com/en-us/research/feed/", 1.0),
    ("Meta Engineering", "https://engineering.fb.com/feed/", 1.0),
    ("Amazon Science", "https://www.amazon.science/index.rss", 0.9),  # Amazon's research output; AI-dense but not AI-only (some non-AI CS) -> gated. Validated live 2026-10-27
    # AI commentary / roundups: AI-focused but occasionally off-topic -> gated; opinion, so modest weight.
    ("Zvi Mowshowitz", "https://thezvi.substack.com/feed", 0.7),  # "Don't Worry About the Vase": dense weekly AI roundups (many entities per post, hence the low weight). Validated live 2026-10-27
    ("Gary Marcus", "https://garymarcus.substack.com/feed", 0.8),  # prominent AI skeptic/commentator; single-topic opinion. Validated live 2026-10-27
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
    # AI stocks / public markets / IPO (all-tech feeds -> AI-gated below to drop non-AI market noise)
    ("CNBC Technology", "https://www.cnbc.com/id/19854910/device/rss/rss.html", 0.8),  # AI stocks/earnings/markets
    ("Seeking Alpha Tech", "https://seekingalpha.com/sector/technology.xml", 0.7),  # AI stock analysis
    ("Sifted", "https://sifted.eu/feed", 0.7),  # European AI startups (AI-gated)
    ("Axios", "https://api.axios.com/feed/", 0.6),  # all-news firehose, AI-gated (Pro Rata has no RSS)
    ("GeekWire AI", "https://www.geekwire.com/tag/ai/feed/", 0.8),
    ("TechRadar AI", "https://www.techradar.com/feeds/tag/ai", 0.6),  # consumer-tech AI tag -> AI-gated (some non-AI noise)
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
    ("OpenShell (releases)", "https://github.com/NVIDIA/OpenShell/releases.atom", 0.8),  # NVIDIA autonomous-agent runtime
    # AI agent frameworks / runtimes — releases.atom (trusted, AI-only; name prefixed per GITHUB_RELEASE_FEEDS).
    ("AutoGPT (releases)", "https://github.com/Significant-Gravitas/AutoGPT/releases.atom", 0.8),
    ("AutoGen (releases)", "https://github.com/microsoft/autogen/releases.atom", 0.8),
    ("CrewAI (releases)", "https://github.com/crewAIInc/crewAI/releases.atom", 0.8),
    ("MetaGPT (releases)", "https://github.com/FoundationAgents/MetaGPT/releases.atom", 0.8),
    ("OpenHands (releases)", "https://github.com/OpenHands/OpenHands/releases.atom", 0.8),
    ("Dify (releases)", "https://github.com/langgenius/dify/releases.atom", 0.8),
    ("browser-use (releases)", "https://github.com/browser-use/browser-use/releases.atom", 0.8),
    ("mem0 (releases)", "https://github.com/mem0ai/mem0/releases.atom", 0.8),
]

# Feeds that are NOT AI-only (general tech/research/news). Stories from these are kept only
# if they look AI-relevant (AI keyword or an AI-specific entity), the same gate used for
# Hacker News. The AI-category feeds above are trusted as-is and never filtered.
AI_FILTER_SOURCES = {
    "Google Research", "Microsoft Research", "Meta Engineering", "Amazon Science",
    "Zvi Mowshowitz", "Gary Marcus",
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
    "CNBC Technology", "Seeking Alpha Tech",  # all-tech/markets feeds: keep only AI-relevant items
    "Sifted", "Axios",  # startups/deals + general-news feeds: keep only AI-relevant items
    "TechRadar AI",  # consumer-tech AI tag feed carries non-AI reviews/deals: gate them out
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
    "OpenShell (releases)": "OpenShell",
    "AutoGPT (releases)": "AutoGPT",
    "AutoGen (releases)": "AutoGen",
    "CrewAI (releases)": "CrewAI",
    "MetaGPT (releases)": "MetaGPT",
    "OpenHands (releases)": "OpenHands",
    "Dify (releases)": "Dify",
    "browser-use (releases)": "browser-use",
    "mem0 (releases)": "mem0",
}

# A story whose link points at a company's own site is that company's content, so attribute it to
# that company's node even when the headline never names the company (e.g. an Anthropic research
# post titled "LLMs are making formal verification feasible" matches no entity by text). This keys
# off the LINK domain, so it also catches a Hacker News submission of the same URL — not just the
# company's RSS feed. build.py adds the owner to each story's mentions after dictionary extraction;
# matching is suffix-based (covers subdomains), only slugs in the entity dictionary take effect, and
# the AI-relevance gate still applies. Keep domains unambiguous to a single entity (so not google.com).
DOMAIN_ENTITY = {
    "anthropic.com": "anthropic",
    "openai.com": "openai",
    "deepmind.google": "google-deepmind",
    "blog.google": "google", "ai.google": "google", "research.google": "google",
    "developers.googleblog.com": "google", "research.googleblog.com": "google",
    "blogs.nvidia.com": "nvidia", "developer.nvidia.com": "nvidia",
    "huggingface.co": "hugging-face",
    "pytorch.org": "pytorch",
    "ai.meta.com": "meta", "engineering.fb.com": "meta",
    "aws.amazon.com": "amazon",
    "machinelearning.apple.com": "apple",
    "research.microsoft.com": "microsoft",
}


# Query params that identify *how you arrived* at a page, not *which* page it is. Newsletter and
# analytics feeds bolt these onto the same article over and over (Fast Company's email feed re-sends
# a story for days, each time with a fresh position=/campaign_date=, so the URL — our dedup key —
# changes and the archive fills with dupes). Strip them so every variant collapses to one canonical
# URL. Denylist, not allowlist: many sites carry a real article id in the query (?p=, ?id=, ?v=),
# so we only drop params we're sure are tracking. Anything starting "utm_" goes regardless.
TRACKING_PARAMS = {
    "position", "partner", "campaign", "campaign_date", "newsletter", "ref", "ref_src", "ref_url",
    "referrer", "source", "feed", "cmpid", "ncid", "fbclid", "gclid", "dclid", "gbraid", "wbraid",
    "msclkid", "yclid", "igshid", "igsh", "mc_cid", "mc_eid", "_hsenc", "_hsmi", "hsctatracking",
    "spm", "scm", "vero_id", "vero_conv", "oly_anon_id", "oly_enc_id", "guccounter", "guce_referrer",
    "guce_referrer_sig", "sr_share", "cmp", "at_medium", "at_campaign",
}


def canonical_url(url):
    """Drop tracking query params (and the fragment) so the same article has one stable URL.
    Keeps non-tracking params and ordering of what remains; returns the input unchanged on error."""
    if not url:
        return url
    try:
        parts = urllib.parse.urlsplit(url)
        if not parts.query:
            kept_q = ""
        else:
            kept = [(k, v) for (k, v) in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                    if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
            kept_q = urllib.parse.urlencode(kept)
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, kept_q, ""))
    except Exception:
        return url


def domain_entity(url):
    """Owning-company entity slug for a URL on a company's own site, else None (suffix match,
    so a subdomain like www.anthropic.com still resolves). See DOMAIN_ENTITY."""
    net = urllib.parse.urlparse(url or "").netloc.lower()
    if net.startswith("www."):
        net = net[4:]
    for dom, ent in DOMAIN_ENTITY.items():
        if net == dom or net.endswith("." + dom):
            return ent
    return None

GITHUB_TOPICS = ["llm", "ai-agents", "agents", "generative-ai", "mcp", "large-language-models", "rag", "ai",
                 # inference-optimisation / local-LLM discovery: surface the next ktransformers/PowerInfer
                 "llm-inference", "inference", "llm-serving", "quantization", "local-llm", "on-device-ai", "gguf"]
# The bare "ai" tag is heavily abused by SEO / piracy repos that stuff it to ride the AI wave, so a repo
# that qualified ONLY via a generic tag (no specific AI topic below) must also show an AI signal in its
# text, or it's dropped. See _new_repo_ok.
GH_GENERIC_TOPICS = {"ai", "agents", "inference", "quantization"}
# Piracy / warez SEO repos (crack/keygen/activator/... for commercial software) farm stars and stuff
# AI tags for visibility -- a hard reject on the owner/repo name regardless of topics or stars.
GH_SPAM_RX = re.compile(
    r"(?:^|[-_/])(?:cracks?|cracked|keygens?|nulled|warez|activators?|activation|repack|"
    r"pre-?activated|serial(?:-?keys?)?|license-?keys?|product-?keys?|free-?downloads?|"
    r"full-?version|torrents?|pirat(?:e|ed|ing)|patcher)(?:$|[-_/])", re.I)

# --- "Trending AI Repos" lane --------------------------------------------------------------------
# Star *velocity* across the top AI-topic repos, computed from a daily snapshot store. Unlike
# fetch_github (which only finds repos born in the last 30 days with an AI topic tag), this catches
# repos that are *accelerating* now, including older ones that suddenly go viral (the OpenShell case).
STARS_SNAPSHOT = os.path.join(os.path.dirname(FEED_CACHE_DIR), "github-stars.json")  # data/github-stars.json
TREND_MIN_STARS = 300       # pool floor: only track repos with at least this many stars
TREND_D1_FLOOR = 50         # include a repo if it gained >= this many stars in ~1 day,
TREND_D7_FLOOR = 150        #   ...or this many in ~7 days (steady climbers that don't spike in a day),
TREND_PCT7 = 0.25           #   ...or grew by >= this fraction over 7 days (relative surge)
TREND_PCT7_MIN_ABS = 100    #      but only when the absolute 7-day gain clears this (skip tiny bases).
TREND_MAX = 15              # hard cap on how many trending repos reach the map
TREND_MAX_AVG_PER_DAY = 700 # star-farm guard: drop repos (older than 45 d) averaging more than this
TREND_SNAPSHOT_DAYS = 35    # retention of the daily snapshot store (headroom over the 30-day series)
# Auto-seeding from HN/news: a repo that's hot in the stories we already collect (a high-engagement
# HN post, or one mentioned across several stories) is seeded into the velocity pool even with NO AI
# topic tag -- closing the "topicless blind spot" (strata/OpenShell) without hand-curation. Gated on
# AI relevance so unrelated repos don't leak in; capped per run to bound GitHub REST calls.
TREND_SEED_MIN_ENGAGEMENT = 75  # a single story with >= this many points (HN) seeds its repo
TREND_SEED_MIN_STORIES = 2      # ...or a repo mentioned across >= this many distinct stories
TREND_SEED_MAX_PER_RUN = 20     # cap new /repos lookups per run (REST core rate limit)
GH_REPO_RX = re.compile(r"github\.com/([A-Za-z0-9][\w.-]*)/([A-Za-z0-9][\w.-]*)")
GH_NON_REPO_OWNERS = {"orgs", "sponsors", "topics", "collections", "features", "about", "pricing",
                      "marketplace", "settings", "notifications", "login", "join", "search", "trending",
                      "apps", "site", "contact", "security", "readme", "explore", "new", "account"}
# Repos behind our curated release feeds (Strata, Project Maya, vLLM, agent frameworks...). They are
# seeded into the velocity pool regardless of topic tags / star floor, because some carry NO GitHub
# topics (e.g. niko1221/strata: 31k* but topics=[]) so topic-search can't find them -- the OpenShell
# blind spot. Seeding lets them get star-velocity tracking and surface in Trending repos when surging.
RELEASE_FEED_REPOS = [re.search(r"github\.com/([^/]+/[^/]+?)/releases", u).group(1)
                      for n, u, _w in RSS_FEEDS
                      if n in GITHUB_RELEASE_FEEDS and re.search(r"github\.com/[^/]+/[^/]+?/releases", u)]


def _load_star_snaps():
    try:
        snaps = json.load(open(STARS_SNAPSHOT))
        return snaps if isinstance(snaps, list) else []
    except Exception:
        return []


def _nearest_snap(snaps, target_ts, tol):
    """Star map of the snapshot whose timestamp is closest to target_ts within tol seconds, else None."""
    best, bestd = None, tol
    for sn in snaps:
        d = abs(sn.get("ts", 0) - target_ts)
        if d <= bestd:
            best, bestd = sn, d
    return best.get("stars", {}) if best else None


def _get(url, headers=None, impersonate=False, timeout=TIMEOUT):
    if impersonate and _cffi is not None:
        r = _cffi.get(url, impersonate="chrome", timeout=timeout, headers=headers or None)
        r.raise_for_status()  # a challenge/error (e.g. 429/403) raises -> caller logs & skips
        return r.content
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
            import gzip
            data = gzip.decompress(data)
        return data


def _clean(html):
    txt = re.sub(r"<[^>]+>", " ", html or "")
    txt = re.sub(r"&[#a-z0-9]+;", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def fetch_article_text(url, log=print, max_chars=8000, timeout=10):
    """Fetch an article page and return its cleaned body text (truncated). Returns None on fetch
    failure (retryable) and "" when the page yields no text. Used to rescue high-engagement HN
    stories whose headline names no entity but whose body does (see build.py body enrichment).
    Uses a short timeout: enrichment runs on the 2-minute refresh, so a slow page must not stall it."""
    try:
        raw = _get(url, timeout=timeout)
        html = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
    except Exception as e:
        log(f"  ! body {url[:50]}: {str(e)[:50]}")
        return None
    # Isolate the real article prose from page chrome before entity matching. News pages embed
    # recirculation modules ("related"/"more news" teasers), share widgets and nav whose text names
    # unrelated entities — e.g. an "OpenAI agents..." teaser on a crime story — which would falsely
    # attach the story to those nodes AND slip it past the AI-relevance gate. Three steps:
    #  1) drop scripts and structural chrome (nav/header/footer/aside/form);
    #  2) keep only the <article> element when the page has one;
    #  3) drop hyperlink text (teaser headlines, share/widget/related links are all <a>), which is
    #     where the cross-contamination lives — article prose is overwhelmingly non-link text.
    html = re.sub(r"(?is)<(script|style|noscript|nav|header|footer|aside|form)\b[^>]*>.*?</\1>", " ", html)
    m = re.search(r"(?is)<article\b[^>]*>(.*?)</article>", html)
    if m:
        html = m.group(1)
    html = re.sub(r"(?is)<a\b[^>]*>.*?</a>", " ", html)
    txt = _clean(html)
    # Social-share toolbars (icon buttons rendered as text inside the article) leak brand names —
    # e.g. CNN's "Facebook Tweet Link Threads Link Copied!" attaches the story to Meta. A run of 2+
    # consecutive share-UI labels is a toolbar; a lone "Facebook" in prose ("Facebook announced...")
    # is left untouched.
    txt = re.sub(r"(?i)(?:\b(?:facebook|tweet|threads|whatsapp|reddit|linkedin|telegram|messenger|"
                 r"pinterest|bluesky|flipboard|mastodon|share|email|link copied!?|copy link)\b[\s.,!|·•\-]*){2,}",
                 " ", txt)
    return txt[:max_chars]


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
        link = canonical_url(link)   # strip newsletter/tracking params so re-sends dedup to one URL
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


def fetch_hn(since, log, max_pages=8):
    """Every HN submission in the window (the /newest firehose, ANY score) + the current front page.

    The firehose catches even a low-point AI story (e.g. an Atlantic AI essay sitting at 6 points);
    the front-page query guarantees the current main page is always included. build.py then AI-gates
    every HN story through the LLM, so only the AI-related ones reach the map and the archive. Polled
    every run, so new submissions are picked up within ~2 min of posting; the 90-day archive keeps
    them, so forward coverage is complete. (Algolia returns newest-first, up to max_pages*1000, which
    covers several days of the window; older low-point stories already live in the accumulated archive.)"""
    out, seen = [], set()

    def add(h):
        oid = h.get("objectID")
        if not oid or oid in seen:
            return
        seen.add(oid)
        title = h.get("title") or ""
        url = canonical_url(h.get("url")) or f"https://news.ycombinator.com/item?id={oid}"
        out.append(dict(id=f"hn:{oid}", title=title, url=url, source="Hacker News",
                        kind="discussion", published=h.get("created_at_i", 0),
                        engagement=h.get("points") or 0, weight=1.0, text=title,
                        discussion=f"https://news.ycombinator.com/item?id={oid}"))

    # 1) the whole current front page, regardless of score (these have "hit the main page")
    fp_ok = False
    try:
        fp = json.loads(_get("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=100"))
        for h in fp.get("hits", []):
            if (h.get("created_at_i") or 0) >= since:
                add(h)
        fp_ok = True
    except Exception as e:
        log(f"  ! Hacker News front page: {str(e)[:80]}")
    fp_n = len(out)
    # Log the sub-fetch's SUCCESS as its own parseable line, not only its failures. feed_health.py
    # tracks a feed by the log lines bearing its name; "Hacker News front page" used to appear only
    # in the "  ! ..." failure form, so its consecutive-failure streak could never reset -- a few
    # transient timeouts (hours apart) wedged it permanently "down", paging every 2 min via
    # feed_alert, while every real fetch since had succeeded. A success line lets the streak clear.
    if fp_ok:
        log(f"  Hacker News front page: {fp_n} stories")

    # 2) the /newest firehose: every story submitted in the window, no points floor
    page = 0
    fh_failed = False
    while page < max_pages:
        q = urllib.parse.urlencode({"tags": "story", "hitsPerPage": 1000, "page": page,
                                    "numericFilters": f"created_at_i>{int(since)}"})
        try:
            data = json.loads(_get("https://hn.algolia.com/api/v1/search_by_date?" + q))
        except Exception as e:
            log(f"  ! Hacker News firehose: {str(e)[:80]}")
            fh_failed = True
            break
        for h in data.get("hits", []):
            add(h)
        page += 1
        if page >= data.get("nbPages", 0):
            break
    # Success line for the firehose sub-fetch too (same reasoning as the front page above). Counts
    # as failed only if the very first page errored (no stories at all); a later-page error still
    # yielded stories, so treat that as a success.
    if not (fh_failed and page == 0):
        log(f"  Hacker News firehose: {len(out) - fp_n} stories")
    log(f"  Hacker News: {len(out)} stories ({fp_n} front page + {len(out) - fp_n} new in window; "
        f"AI-gated downstream)")
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


def _new_repo_ok(r):
    """Admission gate for the 'GitHub (new repos)' lane, on top of the star floor. Rejects obvious
    piracy/warez SEO repos (free-cracks/Adobe-Acrobat-Pro and friends), and repos that only matched a
    generic tag (e.g. bare 'ai') with no AI signal in their name/description."""
    full = (r.get("full_name") or "").lower()
    if GH_SPAM_RX.search(full):
        return False
    topics = {t.lower() for t in (r.get("topics") or [])}
    if not (topics - GH_GENERIC_TOPICS) & {t.lower() for t in GITHUB_TOPICS}:
        from entities import AI_RX  # lazy import: avoid a module-load cycle
        if not AI_RX.search(f"{r.get('name') or ''} {r.get('description') or ''}"):
            return False
    return True


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
            if not _new_repo_ok(r):
                continue
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


def _trend_repo_ok(r):
    """Shared admission gate for the velocity pool: enough stars, recently pushed, not an obvious
    star-farm. Star-farm guard: no legit repo sustains a huge lifetime-average star rate for months
    (a 300k-star repo < 1 yr old is fake); real top AI repos sit well under 200/day."""
    st = r.get("stargazers_count", 0)
    if st < TREND_MIN_STARS or r.get("archived"):
        return False
    pushed = _parse_date(r.get("pushed_at")) or 0
    if pushed and pushed < time.time() - 180 * 86400:          # drop abandoned repos
        return False
    created = _parse_date(r.get("created_at")) or 0
    age_d = (time.time() - created) / 86400 if created else 0
    if age_d > 45 and st / age_d > TREND_MAX_AVG_PER_DAY:
        return False
    return True


def _harvest_repo_mentions(stories):
    """Find github.com/<owner>/<repo> mentions in the stories we already fetched that look AI-relevant,
    and aggregate per repo. Returns {full_name: (max_engagement, distinct_story_count)} -- the signal
    for auto-seeding hot-but-topicless repos into the velocity pool."""
    from entities import AI_RX  # lazy import: avoid any module-load cycle
    agg = {}
    for s in stories or []:
        # Only HN discussions and RSS news -- the blind-spot signal. The GitHub lanes (kind="repo")
        # are already the discovery path we're complementing, and their engagement is stargazers_count
        # (not discussion heat), which would make the engagement threshold meaningless and let every
        # github-sourced repo-story seed itself.
        if s.get("kind") not in ("discussion", "news"):
            continue
        url = s.get("url") or ""
        text = s.get("text") or ""
        if not AI_RX.search(f"{s.get('title','')} {text} {url}"):
            continue
        fns = set()
        for owner, repo in GH_REPO_RX.findall(f"{url} {text}"):
            if owner.lower() in GH_NON_REPO_OWNERS:
                continue
            fns.add(f"{owner}/{repo[:-4] if repo.endswith('.git') else repo}")
        eng = s.get("engagement") or 0
        for fn in fns:
            e, n = agg.get(fn, (0, 0))
            agg[fn] = (max(e, eng), n + 1)
    return agg


def fetch_trending_repos(since, log, stories=None):
    """Rank the top AI repos by star *velocity* and emit the top TREND_MAX as repo stories, each
    carrying a 30-day star series for a sparkline. The pool is the top AI-*topic* repos by stars, plus
    curated release-feed repos, plus repos auto-seeded from hot HN/news stories (`stories`) so a repo
    with no AI topic tag still gets tracked when it's trending. Stores one star snapshot per day in
    github-stars.json; velocity is current stars minus the snapshot ~1 day / ~7 days ago. During the
    first week (no history yet) it falls back to ranking by total stars. Best-effort; never raises."""
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    hdr = {"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {tok}"} if tok else {})}
    pool = {}  # full_name -> current repo dict (max stars seen across topics)
    for topic in GITHUB_TOPICS:
        q = urllib.parse.urlencode({"q": f"topic:{topic}", "sort": "stars", "order": "desc", "per_page": 50})
        try:
            data = json.loads(_get("https://api.github.com/search/repositories?" + q, hdr))
        except Exception as e:
            log(f"  ! trending topic:{topic}: {str(e)[:80]}")
            continue
        for r in data.get("items", []):
            if not _trend_repo_ok(r):
                continue
            fn = r["full_name"]
            if fn not in pool or r.get("stargazers_count", 0) > pool[fn].get("stargazers_count", 0):
                pool[fn] = r
        time.sleep(1.5 if tok else 7)  # unauthenticated search allows 10 requests/minute
    # Seed the curated release-feed repos (bypassing topic/star gates) so topicless-but-popular repos
    # like niko1221/strata still get velocity tracking. Uses the REST repos endpoint (core rate limit,
    # separate from search). The star-farm guard is intentionally skipped here -- these are vetted.
    seeded = 0
    for fn in RELEASE_FEED_REPOS:
        if fn in pool:
            continue
        try:
            r = json.loads(_get(f"https://api.github.com/repos/{fn}", hdr))
        except Exception as e:
            log(f"  ! trending seed {fn}: {str(e)[:60]}")
            continue
        if r.get("full_name") and not r.get("archived"):
            pool[r["full_name"]] = r
            seeded += 1
        time.sleep(0.3 if tok else 2)
    if seeded:
        log(f"  Trending AI repos: seeded {seeded} release-feed repo(s) into the pool")

    # Auto-seed hot-but-topicless repos surfaced by the HN/news stories we already fetched. These
    # bypass the topic/star-floor discovery (they may carry NO AI topic tag) but still face the shared
    # _trend_repo_ok admission gate, an AI-relevance check, and an engagement/mention-count threshold.
    autoseeded = 0
    if stories:
        ranked = sorted(_harvest_repo_mentions(stories).items(),
                        key=lambda kv: -(1000 * kv[1][1] + kv[1][0]))  # distinct stories first, then engagement
        for fn, (eng, n) in ranked:
            if autoseeded >= TREND_SEED_MAX_PER_RUN:
                break
            if fn in pool or fn in RELEASE_FEED_REPOS:
                continue
            if eng < TREND_SEED_MIN_ENGAGEMENT and n < TREND_SEED_MIN_STORIES:
                continue
            try:
                r = json.loads(_get(f"https://api.github.com/repos/{fn}", hdr))
            except Exception as e:
                log(f"  ! trending autoseed {fn}: {str(e)[:50]}")
                continue
            if r.get("full_name") and _trend_repo_ok(r):
                pool[r["full_name"]] = r
                autoseeded += 1
            time.sleep(0.3 if tok else 2)
    if autoseeded:
        log(f"  Trending AI repos: auto-seeded {autoseeded} hot repo(s) from HN/news")

    if not pool:
        log("  Trending AI repos: pool empty (API unreachable)")
        return []

    now = time.time()
    cur = {fn: r.get("stargazers_count", 0) for fn, r in pool.items()}
    snaps = _load_star_snaps()
    # Append today's snapshot at most once per ~day; within a day, refresh today's counts in place.
    if not snaps or (now - snaps[-1].get("ts", 0)) >= 20 * 3600:
        snaps.append({"ts": now, "stars": cur})
    else:
        # Keep the day's ORIGINAL ts on refresh. Bumping it to `now` every 2-min run would keep
        # (now - ts) under the 20 h gate forever, so a new daily snapshot would never be appended
        # and velocity would be stuck in permanent cold start (d1/d7 always None).
        snaps[-1] = {"ts": snaps[-1].get("ts", now), "stars": {**snaps[-1].get("stars", {}), **cur}}
    cutoff = now - TREND_SNAPSHOT_DAYS * 86400
    snaps = [s for s in snaps if s.get("ts", 0) >= cutoff]
    try:
        json.dump(snaps, open(STARS_SNAPSHOT, "w"))
    except Exception as e:
        log(f"  ! star snapshot write: {str(e)[:60]}")

    d1map = _nearest_snap(snaps, now - 86400, 12 * 3600)       # ~1 day ago (±12 h)
    d7map = _nearest_snap(snaps, now - 7 * 86400, 2 * 86400)   # ~7 days ago (±2 d)

    scored = []
    for fn, r in pool.items():
        st = cur[fn]
        d1 = (st - d1map[fn]) if d1map and fn in d1map else None
        base7 = d7map.get(fn) if d7map else None
        d7 = (st - base7) if base7 is not None else None
        pct7 = (d7 / base7) if (d7 is not None and base7) else None
        series = [{"d": datetime.fromtimestamp(sn["ts"], timezone.utc).strftime("%Y-%m-%d"), "s": sn["stars"][fn]}
                  for sn in snaps if fn in sn.get("stars", {})][-30:]
        surging = pct7 is not None and pct7 >= TREND_PCT7 and (d7 or 0) >= TREND_PCT7_MIN_ABS
        cold = d1 is None and d7 is None                       # no history yet (first week)
        include = cold or (d1 is not None and d1 >= TREND_D1_FLOOR) \
            or (d7 is not None and d7 >= TREND_D7_FLOOR) or surging
        if not include:
            continue
        # rank by 1-day velocity; fall back to 7-day/day, then (cold start) total stars
        rank = d1 if d1 is not None else (d7 / 7.0 if d7 is not None else st / 1000.0)
        scored.append((rank, fn, r, d1, d7, pct7, series, surging))

    scored.sort(key=lambda x: -(x[0] or 0))
    out = []
    for rank, fn, r, d1, d7, pct7, series, surging in scored[:TREND_MAX]:
        desc = r.get("description") or ""
        name = fn.split("/", 1)[1]
        out.append(dict(
            id=f"ghtrend:{fn}", title=f"{fn}: {desc}"[:200], url=r["html_url"],
            source="Trending AI Repos", kind="repo", published=now,
            engagement=r.get("stargazers_count", 0), weight=0.8,
            text=f"{name} {desc}", gh_repo=fn,
            star_delta_1d=d1, star_delta_7d=d7, star_pct_7d=(round(pct7, 3) if pct7 is not None else None),
            star_series=series, surging=surging))
    phase = "cold start: by total stars" if not (d1map or d7map) else "by star velocity"
    log(f"  Trending AI repos: {len(out)} of {len(pool)} tracked ({phase}); {len(snaps)} daily snapshots")
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
    stories += _throttled("GitHub trending", DEFAULT_FEED_INTERVAL_HOURS, since, lambda: fetch_trending_repos(since, log, stories), log)
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
