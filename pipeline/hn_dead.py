"""Rescue useful AI articles from DEAD Hacker News posts.

HN hides dead posts from everyone except logged-in users with "showdead" on: the official API returns
them without title/url and Algolia does not index them. So this module reads https://news.ycombinator.com/newest
with the session cookie of a SEPARATE HN account (showdead = yes) and admits a dead post only if:
  1. it is not a "Show HN",
  2. it does not link to GitHub, and
  3. the linked page is a genuine AI article (judged by the local LLM on the page text, strictly).

Cookie: /opt/ai-map/.hn_cookie (chmod 600), one line, the value of HN's "user" cookie (or "user=<value>").
No cookie file -> the feature is off (one quiet log line, no error). Expired cookie -> logged as an error
("  ! HN dead posts: ...") so feed_health / the health portal flag it; normal HN intake is unaffected.

State: data/hn-dead-state.json (verdict per post + admitted stories), decisions: data/hn-dead-log.jsonl.
"""
import calendar
import html as htmlmod
import json
import os
import re
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COOKIE_FILE = os.path.join(ROOT, ".hn_cookie")
STATE_FILE = os.path.join(ROOT, "data", "hn-dead-state.json")
LOG_FILE = os.path.join(ROOT, "data", "hn-dead-log.jsonl")
POLL_EVERY = 230          # seconds; the refresh runs every 2 min, so this polls every other run (~4 min)
MAX_JUDGE_PER_RUN = 6     # cap page fetches + LLM calls per run so a burst can't stall the refresh
NEWEST_URL = "https://news.ycombinator.com/newest"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


FETCH_TRIES = 6   # polls an admitted-looking dead post's article may fail to load before it is given up


def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"last_poll": 0, "verdicts": {}, "admitted": {}}


def _save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


def _log_decision(rec):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _cookie():
    try:
        v = open(COOKIE_FILE).read().strip()
    except Exception:
        return None
    if not v:
        return None
    # a row pasted from the browser's cookie table: "user<TAB>value<TAB>domain<TAB>..." -> take the value
    if "\t" in v:
        f = v.split("\t")
        v = f[1].strip() if f[0].strip() == "user" and len(f) > 1 else f[0].strip()
    v = v.split(";")[0].strip()
    if v.startswith("user="):
        v = v[5:]
    if not v or not v.isascii() or any(c.isspace() for c in v):
        return None
    return "user=" + v


def parse_newest(page):
    """Return (logged_in, [post]) from an HN /newest page. post: id, title, url (None when HN hides it, as it
    does for dead posts), site (the domain HN shows), dead, score, ts."""
    logged_in = 'id="me"' in page or 'href="logout' in page
    rows = re.split(r'<tr class="athing', page)[1:]
    posts = []
    for r in rows:
        m = re.search(r'id="(\d+)"', r)
        if not m:
            continue
        pid = m.group(1)
        tl = re.search(r'<span class="titleline">[^<]*<a([^>]*)>(.*?)</a>', r, re.S)
        if not tl:
            continue
        href = re.search(r'href="([^"]*)"', tl.group(1))
        url = htmlmod.unescape(href.group(1)) if href else None   # HN removes the link on dead posts
        title = htmlmod.unescape(re.sub(r"<[^>]+>", "", tl.group(2))).strip()
        if url and url.startswith("item?id="):
            url = "https://news.ycombinator.com/" + url
        site = re.search(r'class="sitestr">([^<]+)<', r)
        site = htmlmod.unescape(site.group(1)).strip() if site else None
        # "[dead]" / "[flagged]" markers sit next to the title link, outside the anchor text
        outside = re.sub(r"<a [^>]*>.*?</a>", " ", r.split('class="subtext"')[0], flags=re.S)
        dead = "[dead]" in outside
        ts = re.search(r'class="age" title="[^"]*?\s(\d{9,11})"', r)
        if not ts:
            iso = re.search(r'class="age" title="(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)', r)
            ts_v = calendar.timegm(time.strptime(iso.group(1), "%Y-%m-%dT%H:%M:%S")) if iso else int(time.time())
        else:
            ts_v = int(ts.group(1))
        sc = re.search(r'class="score"[^>]*>(\d+) point', r)
        posts.append(dict(id=pid, title=title, url=url, site=site, dead=dead, score=int(sc.group(1)) if sc else 1,
                          ts=ts_v))
    return logged_in, posts


def rule_skip(p):
    """Rules 1 and 2. Returns a reason string if the post is excluded, else None."""
    if re.match(r"\s*show\s+hn\b", p["title"], re.I):
        return "Show HN"
    host = (urllib.parse.urlparse(p["url"]).netloc if p.get("url") else (p.get("site") or "").split("/")[0]).lower()
    if host.endswith("github.com") or host.endswith("github.io") or host.endswith("githubusercontent.com"):
        return "GitHub"
    if host in ("news.ycombinator.com", ""):
        return "no external article"
    return None


# ---- finding the article: HN hides the link of a dead post and shows only its site (e.g. "pupaos.com" or
#      "medium.com/@someone"). Look for a page on that site whose title matches the HN title: the site's home
#      page itself, a link on it, or an item in the site's RSS/Atom feed.
def _norm(t):
    return re.sub(r"[^a-z0-9 ]+", " ", (t or "").lower()).split()


def title_match(a, b):
    """0..1 similarity of two titles (sequence ratio, or how much of the HN title the candidate contains)."""
    import difflib
    x, y = _norm(a), _norm(b)
    if not x or not y:
        return 0.0
    ratio = difflib.SequenceMatcher(None, " ".join(x), " ".join(y)).ratio()
    sx = set(w for w in x if len(w) > 2)
    cover = len(sx & set(y)) / len(sx) if sx else 0.0
    return max(ratio, cover if len(sx) >= 3 else 0.0)


def recent_feed_item(item, hn_ts, days=3):
    """True if a feed item's date is within `days` before (or just after) the HN submission."""
    import email.utils
    from datetime import datetime
    m = re.search(r"<(?:pubDate|published|updated|dc:date)[^>]*>([^<]+)<", item, re.I)
    if not m or not hn_ts:
        return False
    v = m.group(1).strip()
    try:
        ts = email.utils.parsedate_to_datetime(v).timestamp()
    except Exception:
        try:
            ts = datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except Exception:
            return False
    return hn_ts - days * 86400 <= ts <= hn_ts + 6 * 3600


def find_article_url(p, get, log, threshold=0.7):
    """Return (url, how) for a dead post's article, or (None, reason)."""
    site = p.get("site")
    if not site:
        return None, "HN shows no site"
    base = "https://" + site.rstrip("/") + "/"
    hdr = {"User-Agent": UA}

    def fetch(u, timeout=12):
        try:   # browser impersonation (curl_cffi) when sources._get supports it: Substack etc. 403 plain clients
            raw = get(u, headers=hdr, timeout=timeout, impersonate=True)
        except Exception:   # unsupported, or the site rejects the impersonated client: plain request
            raw = get(u, headers=hdr, timeout=timeout)
        return raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
    try:
        home = fetch(base)
    except Exception as e:
        return None, f"site unreachable ({str(e)[:40]})"
    best = (0.0, None, None)

    def consider(score, url, how):
        nonlocal best
        if score > best[0]:
            best = (score, url, how)

    # the home page itself (one-page sites / reports, e.g. pupaos.com)
    for rx in (r"<title[^>]*>(.*?)</title>", r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', r"<h1[^>]*>(.*?)</h1>"):
        m = re.search(rx, home, re.S | re.I)
        if m:
            consider(title_match(p["title"], htmlmod.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))), base, "site home page")
    # links on the home page, then on its blog/posts listing pages
    def scan_links(page, page_url, how):
        lists = []
        for href, text in re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S | re.I):
            u = urllib.parse.urljoin(page_url, htmlmod.unescape(href))
            t = htmlmod.unescape(re.sub(r"<[^>]+>", " ", text))
            if len(t.strip()) >= 12:
                consider(title_match(p["title"], t), u, how)
            if re.search(r"/(blog|posts?|articles?|news|writing|essays|insights)/?$", urllib.parse.urlparse(u).path, re.I):
                lists.append(u)
        return lists
    listing = scan_links(home, base, "link on site home page")
    # the site's feed
    feeds = [urllib.parse.urljoin(base, htmlmod.unescape(h)) for h in re.findall(
        r'<link(?=[^>]+application/(?:rss|atom)\+xml)[^>]+href="([^"]+)"', home, re.I)]
    if best[0] < threshold:
        for f in (feeds[:2] or [base + "feed", base + "rss.xml", base + "feed.xml", base + "atom.xml", base + "index.xml"]):
            try:
                x = fetch(f, timeout=10)
            except Exception:
                continue
            for n, item in enumerate(re.findall(r"<(?:item|entry)\b.*?</(?:item|entry)>", x, re.S | re.I)):
                t = re.search(r"<title[^>]*>(.*?)</title>", item, re.S | re.I)
                lk = re.search(r"<link[^>]*>\s*([^<\s]+)\s*</link>", item, re.I) or \
                    re.search(r'<link[^>]+href="([^"]+)"', item, re.I)
                if t and lk:
                    tt = htmlmod.unescape(re.sub(r"<!\[CDATA\[|\]\]>|<[^>]+>", "", t.group(1)))
                    sc = title_match(p["title"], tt)
                    # links can be relative (Jekyll Atom feeds: href="/post-slug/"); resolve against the feed
                    # URL (2026-11-24: ostro.ws gave "/covert-channels-without-coordination/" -> fetch failed forever)
                    link = urllib.parse.urljoin(f, htmlmod.unescape(lk.group(1)))
                    consider(sc, link, "site feed")
                    # posters often reword the title: accept the site's NEWEST post if it came out shortly
                    # before the HN submission and shares a key word with the HN title
                    if n == 0 and sc >= 0.25 and recent_feed_item(item, p.get("ts")):
                        consider(threshold, link, f"site's newest post (reworded title, match {sc:.2f})")
            if best[0] >= threshold:
                break
    for lu in list(dict.fromkeys(listing))[:2]:
        if best[0] >= threshold:
            break
        try:
            scan_links(fetch(lu, timeout=10), lu, "link on site blog page")
        except Exception:
            pass
    # the sitemap: match the URL slug against the title (sites that don't link the post from the home page)
    if best[0] < threshold:
        host_root = "https://" + site.split("/")[0] + "/"
        maps, seen_maps = [host_root + "sitemap.xml"], set()
        try:
            maps += re.findall(r"(?im)^sitemap:\s*(\S+)", fetch(host_root + "robots.txt", timeout=8))
        except Exception:
            pass
        while maps and len(seen_maps) < 4:
            m = maps.pop(0)
            if m in seen_maps:
                continue
            seen_maps.add(m)
            try:
                x = fetch(m, timeout=10)
            except Exception:
                continue
            locs = [urllib.parse.urljoin(m, l) for l in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", x)]
            if "<sitemapindex" in x:
                maps += [l for l in locs if re.search(r"post|blog|article|news|page", l, re.I)][:3] or locs[:3]
                continue
            for lu in [l for l in locs if re.search(r"/(blog|posts?|articles?|news|writing)/?$", l, re.I)][:2]:
                if lu not in listing:
                    try:
                        scan_links(fetch(lu, timeout=10), lu, "link on site blog page")
                    except Exception:
                        pass
            for loc in locs[:5000]:
                slug = urllib.parse.urlparse(loc).path.rstrip("/").split("/")[-1]
                if len(slug) > 8:
                    consider(title_match(p["title"], slug.replace("-", " ").replace("_", " ")), loc, "site sitemap")
    if best[0] >= threshold:
        return best[1], f"{best[2]}, title match {best[0]:.2f}"
    return None, f"article not found on {site} (best title match {best[0]:.2f})"


JUDGE_PROMPT = """You screen Hacker News posts that HN's spam filter killed, to rescue only GENUINE AI articles.
Decide from the page text below. Answer YES only if ALL hold:
- Artificial intelligence / machine learning is a CENTRAL subject or the core of the article's argument: models,
  research, AI products and how they work, AI industry/business/funding, AI policy/safety, or analysis of applying
  AI to a field or industry (e.g. a market report whose thesis is what AI changes there). A one-line or buzzword
  mention of AI does not count.
- It is a substantive article: news, analysis, research, a report, an essay or a real tutorial, with real content.
  A company may publish it: a long, sourced research report or analysis counts as substantive even if it also
  promotes the company's product (judge the report, not the sales section).
Answer NO for: pure landing pages or sales pitches with no substantial content, ads, SEO filler, listicles of
tools, crypto/token promotion, link farms, sign-up walls, error/blank pages, personal job posts, non-AI topics,
or anything that looks like spam.
Reply ONLY with JSON: {"ai_article": true|false, "reason": "<max 15 words>"}

HN title: %s
URL: %s
Page size: about %d words, %d AI-related sentences, cites sources: %s
Page text (opening, then AI-related passages):
%s"""


AI_SENT_RX = re.compile(r"\b(AI|A\.I\.|artificial intelligence|machine learning|ML|LLMs?|language models?|neural|"
                        r"GPT|Claude|Gemini|transformer|generative|agents?|inference|training|model weights)\b", re.I)


def excerpt(text, head=3000, budget=4000):
    """Opening of the page plus the AI-related sentences from the rest, so a long report whose AI thesis
    sits further down is judged on it (not on its intro alone)."""
    out, used = [], 0
    for sent in re.split(r"(?<=[.!?])\s+", text[head:]):
        if AI_SENT_RX.search(sent) and used + len(sent) <= budget:
            out.append(sent.strip())
            used += len(sent)
    rest = ("\n[AI-related passages from the rest of the page:]\n" + " ... ".join(out)) if out else \
        "\n[No AI-related passages in the rest of the page.]"
    return text[:head] + rest


def judge(p, page_text, log):
    base, model = os.environ.get("AIMAP_LLM_BASE_URL"), os.environ.get("AIMAP_LLM_MODEL")
    if not base or not model:
        return None, "LLM not configured"
    key = os.environ.get("AIMAP_LLM_API_KEY", "none")
    words = len(page_text.split())
    ai_sents = sum(1 for x in re.split(r"(?<=[.!?])\s+", page_text) if AI_SENT_RX.search(x))
    cites = "yes" if re.search(r"\b(Sources|References|Bibliography|Citations)\b", page_text) else "no"
    prompt = JUDGE_PROMPT % (p["title"], p["url"], words, ai_sents, cites, excerpt(page_text))
    body = json.dumps({"model": model, "temperature": 0, "messages": [{"role": "user", "content": prompt}],
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    try:
        req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body,
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        out = json.loads(urllib.request.urlopen(req, timeout=120).read())
        txt = out["choices"][0]["message"]["content"]
        d = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
        return bool(d.get("ai_article")), str(d.get("reason", ""))[:120]
    except Exception as e:
        log(f"  (HN dead posts: LLM judge failed for {p['id']}: {str(e)[:60]})")
        return None, "LLM error"


def fetch_dead(since, log, get, article_text, force=False):
    """Return admitted dead-post stories (fetch_hn story dicts) whose post time is inside the window.

    get(url, headers=..., timeout=...) -> bytes and article_text(url, log) -> str|None|"" come from sources.py.
    """
    st = _load_state()
    now = time.time()
    st["admitted"] = {k: v for k, v in st["admitted"].items() if (v.get("published") or 0) >= now - 8 * 86400}
    st["verdicts"] = {k: v for k, v in st["verdicts"].items() if v.get("at", 0) >= now - 8 * 86400}
    st["fetch_fails"] = {k: v for k, v in st.get("fetch_fails", {}).items() if v.get("at", 0) >= now - 2 * 86400}

    def window():
        return [s for s in st["admitted"].values() if (s.get("published") or 0) >= since]

    cookie = _cookie()
    if not cookie:
        log("  (HN dead posts: off - no cookie file)")
        return []
    if not force and now - st.get("last_poll", 0) < POLL_EVERY:
        return window()
    st["last_poll"] = now
    try:
        page = get(NEWEST_URL, headers={"Cookie": cookie, "User-Agent": UA}, timeout=15)
        page = page.decode("utf-8", "ignore") if isinstance(page, (bytes, bytearray)) else str(page)
    except Exception as e:
        log(f"  ! HN dead posts: {str(e)[:80]}")
        _save_state(st)
        return window()
    logged_in, posts = parse_newest(page)
    if not logged_in:
        log("  ! HN dead posts: not logged in (cookie expired or wrong?) - check /opt/ai-map/.hn_cookie")
        _save_state(st)
        return window()
    dead = [p for p in posts if p["dead"]]
    judged = admitted = 0
    for p in dead:
        if p["id"] in st["verdicts"]:
            continue
        why = rule_skip(p)
        rec = dict(at=int(now), id=p["id"], title=p["title"], url=p.get("url"), site=p.get("site"))
        if why:
            st["verdicts"][p["id"]] = dict(at=int(now), ok=False, reason=why)
            _log_decision({**rec, "ok": False, "reason": why})
            continue
        if judged >= MAX_JUDGE_PER_RUN:
            continue  # judged on a later run
        judged += 1
        found = ""
        if not p.get("url"):
            url, how = find_article_url(p, get, log)
            if not url:
                st["verdicts"][p["id"]] = dict(at=int(now), ok=False, reason=how)
                _log_decision({**rec, "ok": False, "reason": how})
                continue
            p["url"], found = url, how
            rec["url"] = url
            rec["found_via"] = how
        text = article_text(p["url"], log)
        if text is None:
            # fetch failed: retry next poll, but not forever (a bad link failed on every poll, 24 Nov 2026)
            ff = st.setdefault("fetch_fails", {})
            ff[p["id"]] = dict(n=ff.get(p["id"], {}).get("n", 0) + 1, at=int(now))
            if ff[p["id"]]["n"] >= FETCH_TRIES:
                reason = f"article fetch failed {FETCH_TRIES} times"
                st["verdicts"][p["id"]] = dict(at=int(now), ok=False, reason=reason)
                _log_decision({**rec, "ok": False, "reason": reason})
                ff.pop(p["id"], None)
            continue
        if len(text) < 400:
            ok, reason = False, "page has too little text"
        else:
            ok, reason = judge(p, text, log)
            if ok is None:
                continue  # LLM unavailable: retry next poll
        st["verdicts"][p["id"]] = dict(at=int(now), ok=ok, reason=reason)
        _log_decision({**rec, "ok": ok, "reason": reason})
        if ok:
            admitted += 1
            st["admitted"][p["id"]] = dict(
                id=f"hn:{p['id']}", title=p["title"], url=p["url"], source="Hacker News", kind="discussion",
                published=p["ts"], engagement=p["score"], weight=1.0, text=p["title"], ai_verified=True,
                hn_dead=True, discussion=f"https://news.ycombinator.com/item?id={p['id']}")
            log(f"  (HN dead posts: admitted {p['id']} \"{p['title'][:60]}\" - {reason})")
    _save_state(st)
    win = window()
    log(f"  HN dead posts: {len(win)} stories ({len(dead)} dead on /newest, {judged} judged, {admitted} newly admitted)")
    return win
