// Private "today" API for the AI Map Stats iPhone app. Same shared secret as /api/visits
// (env STATS_TOKEN, ?key=... or "Authorization: Bearer ..."). Returns, for the current
// Singapore day: page views, unique visitors, and the last 20 unique visitors (one row per
// visitor hash, showing their most recent view). The hash itself is never returned.
// Bots are excluded unless ?bots=1.

const TZ_OFFSET = 8 * 3600; // Asia/Singapore, no DST

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

export async function onRequestGet(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  const bearer = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
  const token = url.searchParams.get("key") || bearer;
  if (!env.STATS_TOKEN || token !== env.STATS_TOKEN) return json({ error: "unauthorized" }, 401);
  if (!env.DB) return json({ error: "no database bound" }, 500);

  const withBots = url.searchParams.get("bots") === "1";
  const human = withBots ? "" : " AND bot IS NULL";
  const now = Math.floor(Date.now() / 1000);
  const dayStart = Math.floor((now + TZ_OFFSET) / 86400) * 86400 - TZ_OFFSET; // midnight SGT

  try {
    const q = (sql) => env.DB.prepare(sql).bind(dayStart).all();
    const [totals, visitors] = await Promise.all([
      q(`SELECT COUNT(*) views, COUNT(DISTINCT ip_hash) uniques FROM visits WHERE ts>=?${human}`),
      // SQLite: with MAX(ts), the bare columns come from the row holding that max (latest view)
      q(`SELECT MAX(ts) ts, MIN(ts) first_ts, COUNT(*) views, country, region, city, colo, asn, path, referer, bot
         FROM visits WHERE ts>=?${human} GROUP BY ip_hash ORDER BY ts DESC LIMIT 20`),
    ]);
    return json({
      date: new Date((dayStart + TZ_OFFSET) * 1000).toISOString().slice(0, 10),
      timezone: "Asia/Singapore",
      bots: withBots,
      generated_at: new Date().toISOString(),
      views: totals.results[0].views,
      uniques: totals.results[0].uniques,
      visitors: visitors.results,
    });
  } catch (e) {
    return json({ error: String((e && e.message) || e) }, 500);
  }
}
