// Presence heartbeat. The map page live-updates by polling .json files (new_counts.json /
// data.json), which the page-view middleware deliberately ignores — so a tab left open for
// days or weeks is counted only once, at its initial HTML load, and never again. This endpoint
// lets such long-lived tabs announce "still here" without a reload.
//
// It writes AT MOST ONE row per unique visitor per day into the same `visits` table, tagged
// bot='heartbeat' (owner -> 'self-heartbeat'). Because ip_hash is salted with the calendar day,
// "one row per ip_hash among heartbeat rows" is automatically one per visitor per day, so repeat
// pings in a day are no-ops. The heartbeat tag keeps these rows out of both the page-view metrics
// (which select bot IS NULL) and the bot/probe accounting (see api/visits.js), so they surface
// only as the dedicated "open-tab visitors" metric. Never blocks; every error is swallowed.
//
// The page pings on load and periodically while open; the on-load ping overlaps that day's page
// view, but pings on later days (tab still open, no reload) are the whole point.

async function ipHash(ip, salt) {
  try {
    const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(salt + "|" + ip));
    return [...new Uint8Array(buf)].slice(0, 8).map((b) => b.toString(16).padStart(2, "0")).join("");
  } catch (e) {
    return null;
  }
}

function ipInList(ip, list) {
  if (!ip || !list) return false;
  return list.split(/[,\s]+/).some((x) => x && x === ip);
}

// 204 No Content, never cached — the client ignores the body.
const noContent = () => new Response(null, { status: 204, headers: { "cache-control": "no-store" } });

export async function onRequest(context) {
  const { request, env } = context;
  try {
    if (request.method !== "POST" && request.method !== "GET") return noContent();
    if (!env.DB) return noContent();

    const ip = request.headers.get("cf-connecting-ip") || "";
    if (!ip) return noContent();
    const cf = request.cf || {};
    const day = new Date().toISOString().slice(0, 10);
    const salt = (env.IP_SALT || "ai-map") + day;
    const iph = await ipHash(ip, salt);
    if (!iph) return noContent();

    // Owner self-exclusion mirrors the page-view middleware: a browser that opted out (aimap_self=1
    // cookie, set by visiting ?self=1) or a known owner IP is tagged 'self-heartbeat' so the
    // dashboard hides it by default, while the raw log stays complete.
    const cookie = request.headers.get("cookie") || "";
    const isSelf = /(?:^|;\s*)aimap_self=1(?:\s*;|$)/.test(cookie) || ipInList(ip, env.OWNER_IPS);
    const tag = isSelf ? "self-heartbeat" : "heartbeat";

    // One row per visitor per day: ip_hash is day-salted, so dedupe on (heartbeat tags, ip_hash).
    await env.DB.prepare(
      "INSERT INTO visits (ts,path,country,city,region,colo,asn,ua,referer,ip_hash,bot) " +
        "SELECT ?,?,?,?,?,?,?,?,?,?,? " +
        "WHERE NOT EXISTS (SELECT 1 FROM visits WHERE ip_hash=? AND bot IN ('heartbeat','self-heartbeat'))"
    )
      .bind(
        Math.floor(Date.now() / 1000),
        "/api/ping",
        cf.country || null,
        cf.city || null,
        cf.region || null,
        cf.colo || null,
        cf.asOrganization || null,
        request.headers.get("user-agent") || null,
        request.headers.get("referer") || null,
        iph,
        tag,
        iph
      )
      .run();
  } catch (e) {
    /* never let the heartbeat break anything */
  }
  return noContent();
}
