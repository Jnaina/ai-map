// Presence ping for the pages that do not load app.js (articles, topics), added 2026-11-24.
// Same behaviour as the heartbeat in app.js: /api/ping on load, every 6 h while the tab stays open, and when the
// tab regains focus (client throttle 30 min; the server keeps one row per visitor per day). The visit stats use it
// to tell browsers that run the page script from crawlers that only fetch the HTML (the "noscript" tag).
(function () {
  let last = 0;
  function ping() {
    const now = Date.now();
    if (now - last < 30 * 60 * 1000) return;
    last = now;
    try { fetch("/api/ping", { method: "POST", keepalive: true, cache: "no-store" }); } catch (e) {}
  }
  ping();
  setInterval(ping, 6 * 3600 * 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) ping(); });
})();
