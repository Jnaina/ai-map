#!/usr/bin/env python3
"""Tag existing D1 visit rows with a bot reason (probe / ua / dc), using the same rules as
functions/_middleware.js. Rows that look like a person keep bot = NULL.

noscript (2026-11-24): a visitor (IP) who loaded the main page on a Singapore day but whose browser never sent
the page's /api/ping that day (index.html's app.js sends it on load) did not run the page script, so all its untagged
page views that day are tagged 'noscript'. Only the main page sends the ping, so visitors who opened only other pages
were not judged before 2026-11-24 11:46 SGT; since then the articles and topics pages ping too (web/ping.js), so
they count like the main page (weekly issues still do not). Rows younger than NOSCRIPT_GRACE are left for a later run. Runs hourly (bot-retag.sh, cron).

Usage (from /opt/ai-map/pages):  python3 backfill_bots.py [--all]
  default: only rows whose bot column is still NULL;  --all: re-tag every row.
Needs CLOUDFLARE_API_TOKEN (read from .cf_deploy_token if not set) and npx wrangler.
"""
import json
import os
import re
import subprocess
import sys
import time

DB = "ai-map-visits"
ACCOUNT = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "4d92d2ca307430c0f6706269f613fb47")

# --- keep in sync with functions/_middleware.js ---------------------------------------
PAGES = {"/", "/index.html", "/articles", "/articles.html", "/topics", "/topics.html", "/agents", "/agents.html"}  # /topics added 2026-11-22
WEEKLY = re.compile(r"^/weekly(/(index\.html|\d{4}-w\d{2}(\.html)?)?)?/?$")  # AI Weekly pages, added 2026-11-24 (were "probe")
BROWSER_UA = re.compile(r"^Mozilla/5\.0 \(")
# A "+http(s)://..." contact URL inside the UA is a near-universal crawler convention
# (e.g. "Mozilla/5.0 (compatible; DataAIHub/1.0; +https://...)"); real browsers never carry one.
BOT_UA = re.compile(
    r"bot|crawl|spider|slurp|scan|preview|fetch|curl|wget|python|go-http|java/|okhttp|axios|undici|"
    r"libwww|httpclient|scrapy|headless|phantom|puppeteer|playwright|selenium|monitor|uptime|pingdom|"
    r"lighthouse|facebookexternalhit|whatsapp|telegram|discord|slack|leakix|censys|zgrab|masscan|nmap|"
    r"nuclei|expanse|internet-measurement|semrush|ahrefs|mj12|yandex|baidu|bytespider|petal|gptbot|"
    r"claude|perplexity|ccbot|amazonbot|applebot|\+https?://", re.I)
DC_ASN = re.compile(
    r"google|amazon|aws|microsoft|azure|digital ?ocean|linode|akamai|ovh|hetzner|vultr|choopa|"
    r"constant company|oracle|alibaba|aliyun|aceville|collyer quay|tencent|huawei|contabo|m247|leaseweb|techoff|palo alto|virtualine|ghosty|neroso|anthropic|online sas|"
    r"cloudflare|dedik|egihosting|hostpapa|custodiandc|green floid|web2objects|aventice|virtual systems|"
    r"scaleway|netcup|ionos|hostinger|godaddy|datacamp|code200|hostroyale|mevspace|blatant|subnet digital|"
    r"flux telecom|b2 net solutions|servermania|content delivery network|railway|fly\.io|render|vercel|netlify|heroku|data ?cent(er|re)|hosting|server|vps|cloud", re.I)
# WordPress / CMS vulnerability-scan fingerprints in the referer: a real browser never arrives with
# one of these, so a hit on a real page carrying it is a scanner that guessed a WordPress URL.
# Real Chrome always sends "Chrome/130.0.0.0"; hand-written scraper UAs often send "Chrome/130 " (2026-11-20).
FAKE_CHROME = re.compile(r"\bChrome/\d+(?:\.\d+){0,2}(?:\s|$)")
PROBE_IP_MIN, PROBE_IP_WINDOW = 3, 7 * 86400
PROBE_DAY_WINDOW = 86400  # any probe from the same IP or IPv6 /64 within 24 h (2026-11-22)


def v6_block(ip):
    """IPv6 /64 prefix (first four groups) or None; mirrors v6Block() in _middleware.js."""
    if not ip or ":" not in ip:
        return None
    head = ip.split("::")[0].split(":")
    return ":".join(head[:4]).lower() if len(head) >= 4 else None
# Exact UAs of known look-alike bots (keep in sync with KNOWN_BOT_UAS in _middleware.js), 2026-11-20.
KNOWN_BOT_UAS = {
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",  # 2026-11-24 proxy crawler (5+ countries, no page script)
}
PROBE_REF = re.compile(
    r"rest_route=|wp-json|wp-login|wp-admin|xmlrpc\.php|wlwmanifest|/wp-(?:content|includes|config)|[?&]author=\d", re.I)


def bot_reason(path, ua, asn, ref):
    if path not in PAGES and not WEEKLY.match(path):
        return "probe"
    if ref and PROBE_REF.search(ref):
        return "probe"
    if ua in KNOWN_BOT_UAS:
        return "ua"
    if not ua or not BROWSER_UA.search(ua) or BOT_UA.search(ua) or FAKE_CHROME.search(ua):
        return "ua"
    if asn and DC_ASN.search(asn):
        return "dc"
    return None
# ---------------------------------------------------------------------------------------


# articles/topics pages send the ping too since ping.js went live (2026-11-24 11:43 SGT); count them from then on.
PING_PAGES_SINCE = 1795491960   # 2026-11-24 11:46 SGT
NOSCRIPT_GRACE = 900   # seconds a fresh page view waits for its ping before it can be judged

# Untagged page views (rows with an IP: recorded since 2026-11-18; pings since 2026-11-17) of IP-days that include
# the main page but no /api/ping row that day and none within the grace period after the view.
NOSCRIPT_SQL = f"""UPDATE visits SET bot='noscript' WHERE id IN (
  SELECT v.id FROM visits v
  WHERE v.bot IS NULL AND v.ip IS NOT NULL AND v.path<>'/api/ping'
    AND v.ts < CAST(strftime('%s','now') AS INTEGER) - {NOSCRIPT_GRACE}
    AND EXISTS (SELECT 1 FROM visits m WHERE m.ip=v.ip AND (m.path IN ('/','/index.html')
                  OR (m.path IN ('/articles','/articles.html','/topics','/topics.html') AND m.ts >= {PING_PAGES_SINCE}))
                AND (m.ts+28800)/86400=(v.ts+28800)/86400)
    AND NOT EXISTS (SELECT 1 FROM visits p WHERE p.ip=v.ip AND p.path='/api/ping'
                    AND ((p.ts+28800)/86400=(v.ts+28800)/86400 OR p.ts BETWEEN v.ts AND v.ts+{NOSCRIPT_GRACE})))"""


def d1(sql):
    for attempt in range(4):  # 2026-11-22: retry transient "fetch failed" from the D1 API
        out = subprocess.run(
            ["npx", "--yes", "wrangler", "d1", "execute", DB, "--remote", "--json", "--command", sql],
            capture_output=True, text=True, env={**os.environ, "CLOUDFLARE_ACCOUNT_ID": ACCOUNT})
        if out.returncode == 0:
            break
        time.sleep(5 * (attempt + 1))
    if out.returncode != 0:
        sys.exit(f"wrangler failed: {out.stderr[-1500:]} {out.stdout[-1500:]}")
    return json.loads(out.stdout)[0]["results"]


def main():
    if not os.environ.get("CLOUDFLARE_API_TOKEN"):
        os.environ["CLOUDFLARE_API_TOKEN"] = open(os.path.join(os.path.dirname(__file__) or ".", ".cf_deploy_token")).read().strip()
    # Never touch rows the live middleware tagged bot='self' (owner self-exclusion): that tag
    # depends on a cookie / live IP we don't store, so it can't be recomputed here. Default run
    # only fills untagged rows; --all re-tags everything EXCEPT the self rows.
    # Heartbeat rows (/api/ping) are never page views: re-tagging them here would mark every one 'probe'
    # and wipe out the open-tab metric. api/ping.js tags them itself (heartbeat / heartbeat-ua / -dc).
    where = " WHERE bot IS NULL" if "--all" not in sys.argv else " WHERE bot IS NULL OR (bot <> 'self' AND bot NOT LIKE '%heartbeat%')"
    rows = d1(f"SELECT id, path, ua, asn, referer FROM visits{where}")
    groups = {}
    for r in rows:
        reason = bot_reason(r["path"] or "", r["ua"], r["asn"], r.get("referer"))
        groups.setdefault(reason, []).append(r["id"])
    for reason, ids in groups.items():
        if reason is None:
            continue
        for i in range(0, len(ids), 400):
            chunk = ",".join(str(x) for x in ids[i:i + 400])
            d1(f"UPDATE visits SET bot='{reason}' WHERE id IN ({chunk})")
    if "--all" in sys.argv and groups.get(None):
        for i in range(0, len(groups[None]), 400):
            chunk = ",".join(str(x) for x in groups[None][i:i + 400])
            d1(f"UPDATE visits SET bot=NULL WHERE id IN ({chunk})")
    # probe-ip: real-page rows from an IP with 3+ distinct probe paths within 7 days either side.
    before = d1("SELECT COUNT(*) n FROM visits WHERE bot='probe-ip'")[0]["n"]
    d1("UPDATE visits SET bot='probe-ip' WHERE bot IS NULL AND ip IS NOT NULL AND "
       "(SELECT COUNT(DISTINCT p.path) FROM visits p WHERE p.ip=visits.ip AND p.bot='probe' "
       f"AND p.ts BETWEEN visits.ts-{PROBE_IP_WINDOW} AND visits.ts+{PROBE_IP_WINDOW}) >= {PROBE_IP_MIN}")
    # probe-ip, 24 h rule: any probe within a day either side from the same IP or IPv6 /64.
    probes = {}
    for r in d1("SELECT ip, ts FROM visits WHERE bot='probe' AND ip IS NOT NULL"):
        for k in {r["ip"], v6_block(r["ip"])} - {None}:
            probes.setdefault(k, []).append(r["ts"])
    hit = [r["id"] for r in d1("SELECT id, ip, ts FROM visits WHERE bot IS NULL AND ip IS NOT NULL")
           if any(abs(t - r["ts"]) <= PROBE_DAY_WINDOW
                  for k in {r["ip"], v6_block(r["ip"])} - {None} for t in probes.get(k, ()))]
    for i in range(0, len(hit), 400):
        d1(f"UPDATE visits SET bot='probe-ip' WHERE id IN ({','.join(str(x) for x in hit[i:i + 400])})")
    after = d1("SELECT COUNT(*) n FROM visits WHERE bot='probe-ip'")[0]["n"]
    ns_before = d1("SELECT COUNT(*) n FROM visits WHERE bot='noscript'")[0]["n"]
    d1(NOSCRIPT_SQL)
    ns_after = d1("SELECT COUNT(*) n FROM visits WHERE bot='noscript'")[0]["n"]
    print({(k or "human"): len(v) for k, v in groups.items()}, f"of {len(rows)} rows checked;",
          f"probe-ip rows {before} -> {after}; noscript rows {ns_before} -> {ns_after}")


if __name__ == "__main__":
    main()
