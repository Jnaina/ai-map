// Private analytics API, backing the /stats.html dashboard. Returns aggregated visit
// stats from D1 as JSON. Gated by a shared secret (env STATS_TOKEN): pass it as
// ?key=... or an "Authorization: Bearer ..." header. Without the right token -> 401.
// Nothing here is cached (see _headers).
//
// 2026-10-20:
// - Bots are excluded by default (rows with a `bot` reason, see _middleware.js);
//   pass ?bots=1 to include them. `botViews` reports what was hidden.
// - Days are Singapore days (UTC+8).
// - Unique visitors: the visitor hash changes every day by design, so uniques are only
//   meaningful per day. Totals report the average and the sum of daily uniques.
// - `daily90` always covers the last 90 Singapore days (views + unique visitors per day).

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

  const days = Math.min(Math.max(parseInt(url.searchParams.get("days") || "30", 10) || 30, 1), 365);
  const withBots = url.searchParams.get("bots") === "1";
  const now = Math.floor(Date.now() / 1000);
  const since = now - days * 86400;
  // start of the Singapore day 89 days ago, for the fixed 90-day chart
  const todaySg = Math.floor((now + TZ_OFFSET) / 86400);
  const since90 = (todaySg - 89) * 86400 - TZ_OFFSET;

  const human = withBots ? "" : " AND bot IS NULL";
  const day = `date(ts + ${TZ_OFFSET}, 'unixepoch')`;
  const q = (sql, start = since) => env.DB.prepare(sql).bind(start).all();

  try {
    // "Open-tab visitors": people who sent a presence heartbeat on a day but never loaded the HTML
    // that day (no bot-IS-NULL page view) — i.e. they kept the map open from an earlier day and let
    // it live-update without reloading. ip_hash is day-salted, so matching ip_hash means same day.
    const hbSql =
      `SELECT ${day} d, COUNT(DISTINCT ip_hash) u FROM visits hb ` +
      `WHERE ts>=? AND bot='heartbeat' ` +
      `AND NOT EXISTS (SELECT 1 FROM visits v WHERE v.ip_hash=hb.ip_hash AND v.bot IS NULL) ` +
      `GROUP BY d ORDER BY d`;

    const [total, daily, byCountry, byCity, byDay, byPath, byRef, byAsn, byBot, recent, daily90, hb] =
      await Promise.all([
        q(`SELECT COUNT(*) views FROM visits WHERE ts>=?${human}`),
        q(`SELECT ${day} d, COUNT(DISTINCT ip_hash) u FROM visits WHERE ts>=?${human} GROUP BY d`),
        q(`SELECT COALESCE(country,'??') k, COUNT(*) c FROM visits WHERE ts>=?${human} GROUP BY k ORDER BY c DESC LIMIT 60`),
        q(`SELECT city k, country, COUNT(*) c FROM visits WHERE ts>=?${human} AND city IS NOT NULL GROUP BY city,country ORDER BY c DESC, k ASC LIMIT 250`),
        q(`SELECT ${day} d, COUNT(*) c, COUNT(DISTINCT ip_hash) u FROM visits WHERE ts>=?${human} GROUP BY d ORDER BY d`),
        q(`SELECT path k, COUNT(*) c FROM visits WHERE ts>=?${human} GROUP BY k ORDER BY c DESC LIMIT 25`),
        q(`SELECT referer k, COUNT(*) c FROM visits WHERE ts>=?${human} AND referer IS NOT NULL AND referer<>'' GROUP BY k ORDER BY c DESC LIMIT 25`),
        q(`SELECT COALESCE(asn,'(unknown)') k, COUNT(*) c FROM visits WHERE ts>=?${human} GROUP BY k ORDER BY c DESC LIMIT 25`),
        // heartbeat tags are humans, not bots/probes: exclude them from the bot accounting
        q(`SELECT bot k, COUNT(*) c FROM visits WHERE ts>=? AND bot IS NOT NULL AND bot NOT IN ('heartbeat','self-heartbeat') GROUP BY k ORDER BY c DESC`),
        q(`SELECT ts,path,country,city,region,colo,asn,referer,bot FROM visits WHERE ts>=?${human} ORDER BY ts DESC LIMIT 150`),
        q(`SELECT ${day} d, COUNT(*) c, COUNT(DISTINCT ip_hash) u FROM visits WHERE ts>=?${human} GROUP BY d ORDER BY d`, since90),
        q(hbSql),
      ]);

    const dailyU = daily.results.map((r) => r.u);
    const sumU = dailyU.reduce((a, b) => a + b, 0);
    const botViews = byBot.results.reduce((a, r) => a + r.c, 0);
    const hbU = hb.results.map((r) => r.u);
    const hbSum = hbU.reduce((a, b) => a + b, 0);

    // fill the 90-day series so every day appears, even with no visits
    const have = Object.fromEntries(daily90.results.map((r) => [r.d, r]));
    const series90 = [];
    for (let i = 89; i >= 0; i--) {
      const d = new Date((todaySg - i) * 86400 * 1000).toISOString().slice(0, 10);
      const r = have[d];
      series90.push({ d, c: r ? r.c : 0, u: r ? r.u : 0 });
    }

    return json({
      days,
      bots: withBots,
      timezone: "Asia/Singapore",
      generated_at: new Date().toISOString(),
      totals: {
        views: total.results[0].views,
        uniques: sumU, // sum of daily unique visitors over the range ("visitor-days")
        uniquesPerDay: dailyU.length ? Math.round((sumU / dailyU.length) * 10) / 10 : 0, // average over days with visits
        activeDays: dailyU.length,
      },
      botViews,
      byBot: byBot.results,
      // open-tab presence: visitors active via a still-open, self-updating tab on a day they did
      // not (re)load the page. `perDay` is the average over days that had any; `daily` is the series.
      openTabs: {
        daily: hb.results,
        total: hbSum, // visitor-days
        perDay: hbU.length ? Math.round((hbSum / hbU.length) * 10) / 10 : 0,
      },
      byCountry: byCountry.results,
      byCity: byCity.results,
      byDay: byDay.results,
      byPath: byPath.results,
      byReferer: byRef.results,
      byNetwork: byAsn.results,
      recent: recent.results,
      daily90: series90,
    });
  } catch (e) {
    return json({ error: String((e && e.message) || e) }, 500);
  }
}
