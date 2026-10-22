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
const BOT_UA_RX = /bot|crawl|spider|slurp|scan|preview|fetch|curl|wget|python|go-http|java\/|okhttp|axios|undici|libwww|httpclient|scrapy|headless|phantom|puppeteer|playwright|selenium|monitor|uptime|pingdom|lighthouse|facebookexternalhit|whatsapp|telegram|discord|slack|leakix|censys|zgrab|masscan|nmap|nuclei|expanse|internet-measurement|semrush|ahrefs|mj12|yandex|baidu|bytespider|petal|gptbot|claude|perplexity|ccbot|amazonbot|applebot/i;
const DC_ASN_RX = /google|amazon|aws|microsoft|azure|digital ?ocean|linode|akamai|ovh|hetzner|vultr|choopa|constant company|oracle|alibaba|tencent|huawei|contabo|m247|leaseweb|palo alto|virtualine|ghosty|cloudflare|dedik|egihosting|hostpapa|custodiandc|green floid|web2objects|aventice|virtual systems|scaleway|netcup|ionos|hostinger|godaddy|datacamp|code200|hostroyale|mevspace|blatant|subnet digital|data ?cent(er|re)|hosting|server|vps|cloud/i;

function botReason(path, ua, asn) {
  if (!PAGES.has(path)) return "probe";
  if (!ua || !BROWSER_UA_RX.test(ua) || BOT_UA_RX.test(ua)) return "ua";
  if (asn && DC_ASN_RX.test(asn)) return "dc";
  return null;
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
  const res = await next();
  try {
    if (!env.DB || request.method !== "GET") return res;
    const p = new URL(request.url).pathname;
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
          request.headers.get("referer") || null,
          iph,
          botReason(p, ua, asn)
        )
        .run();
    })();
    waitUntil(task);
  } catch (e) {
    /* never let analytics break the site */
  }
  return res;
}
