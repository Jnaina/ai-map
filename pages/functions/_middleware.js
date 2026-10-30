// Runs on every request to the Pages site. Records one row per *page view* (not assets,
// data files or API calls) into the D1 database `DB`, using the geo fields Cloudflare
// attaches to each request (request.cf). Never blocks or breaks serving: logging happens
// in waitUntil() and every error is swallowed.
//
// Privacy: we store coarse origin (country/city/region), the CF edge PoP (colo), the
// network (asOrganization), path, UA and referer — plus a per-day salted hash of the IP
// so unique visitors can be counted without ever storing a raw IP address.
//
// Bot tagging (2026-10-20): every row is still logged, but rows that are not a person get a
// reason in the `bot` column, so the dashboard can hide them while the raw log stays complete:
//   probe — a path that is not one of the site's pages (vulnerability scanners: /.env, /.git/…)
//   ua    — the user agent is not a normal browser, or names a bot/tool
//   dc    — the request came from a cloud / hosting / data-centre network
// The same rules live in pages/backfill_bots.py (used to tag older rows); keep them in sync.

const ASSET_RX = /\.(?:js|css|json|png|jpe?g|gif|svg|ico|webp|woff2?|ttf|map|txt|xml|wasm)$/i;

const PAGES = new Set(["/", "/index.html", "/articles", "/articles.html"]);
const BROWSER_UA_RX = /^Mozilla\/5\.0 \(/;
// A "+http(s)://..." contact URL inside the UA is a near-universal crawler convention
// (e.g. "Mozilla/5.0 (compatible; DataAIHub/1.0; +https://...)"); real browsers never carry one.
const BOT_UA_RX = /bot|crawl|spider|slurp|scan|preview|fetch|curl|wget|python|go-http|java\/|okhttp|axios|undici|libwww|httpclient|scrapy|headless|phantom|puppeteer|playwright|selenium|monitor|uptime|pingdom|lighthouse|facebookexternalhit|whatsapp|telegram|discord|slack|leakix|censys|zgrab|masscan|nmap|nuclei|expanse|internet-measurement|semrush|ahrefs|mj12|yandex|baidu|bytespider|petal|gptbot|claude|perplexity|ccbot|amazonbot|applebot|\+https?:\/\//i;
const DC_ASN_RX = /google|amazon|aws|microsoft|azure|digital ?ocean|linode|akamai|ovh|hetzner|vultr|choopa|constant company|oracle|alibaba|tencent|huawei|contabo|m247|leaseweb|techoff|palo alto|virtualine|ghosty|cloudflare|dedik|egihosting|hostpapa|custodiandc|green floid|web2objects|aventice|virtual systems|scaleway|netcup|ionos|hostinger|godaddy|datacamp|code200|hostroyale|mevspace|blatant|subnet digital|railway|fly\.io|render|vercel|netlify|heroku|data ?cent(er|re)|hosting|server|vps|cloud/i;
// WordPress / CMS vulnerability-scan fingerprints in the *referer*: a real browser never arrives
// with one of these in its Referer header, so a hit on a real page (/) carrying it is a scanner
// that guessed a WordPress URL. Catches probes that use a believable browser UA from an ASN we
// don't yet know as a data-centre (e.g. the "?rest_route=/" sprays from TECHOFF SRV LIMITED).
const PROBE_REF_RX = /rest_route=|wp-json|wp-login|wp-admin|xmlrpc\.php|wlwmanifest|\/wp-(?:content|includes|config)|[?&]author=\d/i;

function botReason(path, ua, asn, ref) {
  if (!PAGES.has(path)) return "probe";
  if (ref && PROBE_REF_RX.test(ref)) return "probe";
  if (!ua || !BROWSER_UA_RX.test(ua) || BOT_UA_RX.test(ua)) return "ua";
  if (asn && DC_ASN_RX.test(asn)) return "dc";
  return null;
}

// Owner self-exclusion: is this request the site owner debugging the app? Two signals,
// either one is enough — a per-browser opt-out cookie (set by visiting ?self=1), or a
// known owner IP listed in the OWNER_IPS env var (comma/space separated). Such rows are
// still logged, but tagged bot='self' so the dashboard hides them by default (bot IS NULL)
// while the raw log stays complete. The `self` tag is runtime-only (it depends on the
// cookie / live IP, neither of which is stored), so backfill_bots.py never sets or clears it.
function ipInList(ip, list) {
  if (!ip || !list) return false;
  return list.split(/[,\s]+/).some((x) => x && x === ip);
}

async function ipHash(ip, salt) {
  try {
    const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(salt + "|" + ip));
    return [...new Uint8Array(buf)].slice(0, 8).map((b) => b.toString(16).padStart(2, "0")).join("");
  } catch (e) {
    return null;
  }
}

export async function onRequest(context) {
  const { request, env, next, waitUntil } = context;
  // Google Search Console HTML-file verification: serve the token verbatim with a 200 at the exact
  // .html URL. Pages otherwise 308-redirects *.html to its extensionless path, and GSC's file check
  // can reject a redirect. Short-circuit before next() so no redirect is ever produced.
  if (request.method === "GET" && new URL(request.url).pathname === "/google7b26fcff0836be29.html") {
    return new Response("google-site-verification: google7b26fcff0836be29.html",
      { headers: { "content-type": "text/html; charset=utf-8" } });
  }
  const res = await next();
  try {
    if (!env.DB || request.method !== "GET") return res;
    const url = new URL(request.url);
    const p = url.pathname;
    // only page views (probes for missing paths are served the HTML shell, so they land here too)
    if (ASSET_RX.test(p) || p.startsWith("/api/") || p === "/stats.html" || p === "/stats") return res;
    const ct = res.headers.get("content-type") || "";
    const isHtml = p === "/" || p.endsWith("/") || p.endsWith(".html") || ct.includes("text/html");
    if (!isHtml) return res;

    const cf = request.cf || {};
    const ip = request.headers.get("cf-connecting-ip") || "";
    const day = new Date().toISOString().slice(0, 10);
    const salt = (env.IP_SALT || "ai-map") + day;
    const ua = request.headers.get("user-agent") || null;
    const asn = cf.asOrganization || null;
    const ref = request.headers.get("referer") || null;

    // Owner self-exclusion. ?self=1 opts this browser out (sets a 1-year cookie); ?self=0 opts
    // back in (clears it). Otherwise fall back to the cookie or the OWNER_IPS allowlist.
    const toggle = url.searchParams.get("self");
    const cookie = request.headers.get("cookie") || "";
    let isSelf = /(?:^|;\s*)aimap_self=1(?:\s*;|$)/.test(cookie) || ipInList(ip, env.OWNER_IPS);
    let setCookie = null;
    if (toggle === "1") {
      isSelf = true;
      setCookie = "aimap_self=1; Path=/; Max-Age=31536000; SameSite=Lax";
    } else if (toggle === "0") {
      isSelf = false;
      setCookie = "aimap_self=; Path=/; Max-Age=0; SameSite=Lax";
    }
    const reason = isSelf ? "self" : botReason(p, ua, asn, ref);

    const task = (async () => {
      const iph = await ipHash(ip, salt);
      await env.DB.prepare(
        "INSERT INTO visits (ts,path,country,city,region,colo,asn,ua,referer,ip_hash,bot) " +
          "VALUES (?,?,?,?,?,?,?,?,?,?,?)"
      )
        .bind(
          Math.floor(Date.now() / 1000),
          p,
          cf.country || null,
          cf.city || null,
          cf.region || null,
          cf.colo || null,
          asn,
          ua,
          ref,
          iph,
          reason
        )
        .run();
    })();
    waitUntil(task);

    // If the opt-out was just toggled, set/clear the cookie on a mutable copy of the response.
    if (setCookie) {
      const r = new Response(res.body, res);
      r.headers.append("Set-Cookie", setCookie);
      return r;
    }
  } catch (e) {
    /* never let analytics break the site */
  }
  return res;
}
