#!/usr/bin/env python3
"""Tag existing D1 visit rows with a bot reason (probe / ua / dc), using the same rules as
functions/_middleware.js. Rows that look like a person keep bot = NULL.

Usage (from /opt/ai-map/pages):  python3 backfill_bots.py [--all]
  default: only rows whose bot column is still NULL;  --all: re-tag every row.
Needs CLOUDFLARE_API_TOKEN (read from .cf_deploy_token if not set) and npx wrangler.
"""
import json
import os
import re
import subprocess
import sys

DB = "ai-map-visits"
ACCOUNT = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "4d92d2ca307430c0f6706269f613fb47")

# --- keep in sync with functions/_middleware.js ---------------------------------------
PAGES = {"/", "/index.html", "/articles", "/articles.html"}
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
    r"constant company|oracle|alibaba|tencent|huawei|contabo|m247|leaseweb|palo alto|virtualine|ghosty|"
    r"cloudflare|dedik|egihosting|hostpapa|custodiandc|green floid|web2objects|aventice|virtual systems|"
    r"scaleway|netcup|ionos|hostinger|godaddy|datacamp|code200|hostroyale|mevspace|blatant|subnet digital|"
    r"railway|fly\.io|render|vercel|netlify|heroku|data ?cent(er|re)|hosting|server|vps|cloud", re.I)


def bot_reason(path, ua, asn):
    if path not in PAGES:
        return "probe"
    if not ua or not BROWSER_UA.search(ua) or BOT_UA.search(ua):
        return "ua"
    if asn and DC_ASN.search(asn):
        return "dc"
    return None
# ---------------------------------------------------------------------------------------


def d1(sql):
    out = subprocess.run(
        ["npx", "--yes", "wrangler", "d1", "execute", DB, "--remote", "--json", "--command", sql],
        capture_output=True, text=True, env={**os.environ, "CLOUDFLARE_ACCOUNT_ID": ACCOUNT})
    if out.returncode != 0:
        sys.exit(f"wrangler failed: {out.stderr[-500:]}")
    return json.loads(out.stdout)[0]["results"]


def main():
    if not os.environ.get("CLOUDFLARE_API_TOKEN"):
        os.environ["CLOUDFLARE_API_TOKEN"] = open(os.path.join(os.path.dirname(__file__) or ".", ".cf_deploy_token")).read().strip()
    # Never touch rows the live middleware tagged bot='self' (owner self-exclusion): that tag
    # depends on a cookie / live IP we don't store, so it can't be recomputed here. Default run
    # only fills untagged rows; --all re-tags everything EXCEPT the self rows.
    where = " WHERE bot IS NULL" if "--all" not in sys.argv else " WHERE bot IS NULL OR bot <> 'self'"
    rows = d1(f"SELECT id, path, ua, asn FROM visits{where}")
    groups = {}
    for r in rows:
        reason = bot_reason(r["path"] or "", r["ua"], r["asn"])
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
    print({(k or "human"): len(v) for k, v in groups.items()}, f"of {len(rows)} rows checked")


if __name__ == "__main__":
    main()
