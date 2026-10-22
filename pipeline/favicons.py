"""Fetch and cache a favicon per news source, so the Articles sidebar can show site icons.

Sources are grouped by feed *name* ("TechCrunch AI", "Hacker News", ...), so we map each
known name to its canonical domain; unknown sources fall back to their most common article
domain. Icons are downloaded once (via Google's favicon service, which returns a clean PNG)
into web/assets/favicons/<domain>.png and served locally thereafter — no per-view external
dependency. Best-effort: a failed download just means the page shows its text fallback.
"""
import glob
import os
import time
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 ai-map/0.1 (+personal project)"}
TIMEOUT = 8
GOOGLE = "https://www.google.com/s2/favicons?sz=64&domain={}"
MAX_AGE = 30 * 86400  # re-fetch an icon at most monthly

# Explicit per-domain icon overrides: some sites (usually Substack/custom-domain) have no
# icon in Google's favicon service (it 301s to an error page), so fetch those from icon.horse,
# which resolves them. Keyed by the same domain domain_for() returns.
ICON_URLS = {
    "interconnects.ai": "https://icon.horse/icon/interconnects.ai",
    "thealgorithmicbridge.com": "https://icon.horse/icon/thealgorithmicbridge.com",
}

# Canonical domain per known source name (needed where the feed name isn't the link domain,
# e.g. Hacker News and GitHub point at many external sites).
SOURCE_DOMAINS = {
    "Hacker News": "news.ycombinator.com",
    "GitHub (new repos)": "github.com",
    "Hugging Face Papers": "huggingface.co",
    "Hugging Face Trending": "huggingface.co",
    "Hugging Face Blog": "huggingface.co",
    "TechCrunch AI": "techcrunch.com",
    "The Verge AI": "theverge.com",
    "VentureBeat AI": "venturebeat.com",
    "MIT Technology Review": "technologyreview.com",
    "Ars Technica AI": "arstechnica.com",
    "Wired AI": "wired.com",
    "The Decoder": "the-decoder.com",
    "Simon Willison": "simonwillison.net",
    "OpenAI News": "openai.com",
    "Google AI Blog": "blog.google",
    "Google DeepMind": "deepmind.google",
    "NVIDIA Blog": "blogs.nvidia.com",
    "AWS Machine Learning": "aws.amazon.com",
    "Google Research": "research.google",
    "Microsoft Research": "microsoft.com",
    "Meta Engineering": "engineering.fb.com",
    "The Information": "theinformation.com",
    "Semafor": "semafor.com",
    "Platformer": "platformer.news",
    "Anthropic": "anthropic.com",
    "Anthropic Engineering": "anthropic.com",
    "Anthropic Red Teaming": "anthropic.com",
    "Thinking Machines": "thinkingmachines.ai",
    "Slashdot IT": "slashdot.org",
    "Microsoft AI Blog": "microsoft.com",
    "Apple Machine Learning": "machinelearning.apple.com",
    "PyTorch": "pytorch.org",
    "LangChain": "langchain.com",
    "Together AI": "together.ai",
    "Last Week in AI": "lastweekin.ai",
    "Berkeley AI Research": "bair.berkeley.edu",
    "Lilian Weng": "lilianweng.github.io",
    "arXiv cs.AI": "arxiv.org",
    "Interconnects": "interconnects.ai",
    "The Algorithmic Bridge": "thealgorithmicbridge.com",
}


def domain_for(source, articles):
    """Canonical domain for a source: the hardcoded map, else the commonest article domain."""
    if source in SOURCE_DOMAINS:
        return SOURCE_DOMAINS[source]
    counts = {}
    for a in articles:
        d = a.get("domain")
        if d:
            counts[d] = counts.get(d, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def _ext(data):
    """Pick a file extension from the image's magic bytes, so the on-disk name matches the
    real format (Google's favicon service returns PNG, but sometimes JPEG/ICO/GIF/WebP)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] in (b"GIF8",):
        return "gif"
    if data[:4] == b"\x00\x00\x01\x00":
        return "ico"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if b"<svg" in data[:256].lower() or data[:5] == b"<?xml":
        return "svg"
    return "png"


def _cached(favdir, safe):
    """Newest existing cached icon for this domain (any extension), or None."""
    hits = sorted(glob.glob(os.path.join(favdir, safe + ".*")), key=os.path.getmtime, reverse=True)
    for p in hits:
        if os.path.getsize(p) > 0:
            return p
    return None


def ensure(domain, favdir, log=print):
    """Return a web-root-relative path to the cached favicon for `domain`, downloading it
    if missing/stale. The file is saved with the extension matching its true format.
    Returns None on failure."""
    if not domain:
        return None
    safe = domain.replace("/", "_")
    existing = _cached(favdir, safe)
    if existing and (time.time() - os.path.getmtime(existing)) < MAX_AGE:
        return "assets/favicons/" + os.path.basename(existing)
    try:
        os.makedirs(favdir, exist_ok=True)
        req = urllib.request.Request(ICON_URLS.get(domain, GOOGLE.format(domain)), headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = r.read()
        if len(data) < 100:  # Google returns a tiny default when it has nothing useful
            return "assets/favicons/" + os.path.basename(existing) if existing else None
        name = safe + "." + _ext(data)
        # Drop any stale copy saved under a different extension.
        for p in glob.glob(os.path.join(favdir, safe + ".*")):
            if os.path.basename(p) != name:
                try:
                    os.remove(p)
                except OSError:
                    pass
        with open(os.path.join(favdir, name), "wb") as f:
            f.write(data)
        return "assets/favicons/" + name
    except Exception as e:
        log(f"  ! favicon {domain}: {str(e)[:60]}")
        return "assets/favicons/" + os.path.basename(existing) if existing else None
