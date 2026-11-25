// Agent Leaderboard (agents.html): renders /data/agents/leaderboard.json (daily copy of Agent Duel's public
// leaderboard, written by pipeline/agentduel.py) + /data/agents/history.json (our own daily snapshots).
(function () {
  "use strict";
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const PALETTE = ["#d1495b", "#e07a1f", "#2e86de", "#16a085", "#8e44ad", "#c0392b", "#27ae60", "#34495e",
    "#f39c12", "#1abc9c", "#7f8c8d", "#6c5ce7", "#b8860b", "#0e7490", "#be185d", "#4d7c0f"];
  const AD = "https://agentduel.com";

  let D = null, H = null;
  const S = { votes: "all", metric: "cost", stat: "median", h2h: "actual", labsOff: new Set(), q: "",
    sort: { k: "rank", dir: 1 }, model: null, a: null, b: null, budget: null, hidden: new Set() };
  const color = {};

  // ---------- state <-> URL ----------
  function readHash() {
    const p = new URLSearchParams(location.hash.slice(1));
    if (p.get("votes") === "noEarly") S.votes = "noEarly";
    if (p.get("metric") === "time") S.metric = "time";
    if (p.get("stat") === "mean") S.stat = "mean";
    if (["expected", "surprise", "battles"].includes(p.get("h2h"))) S.h2h = p.get("h2h");
    if (p.get("labs")) p.get("labs").split(",").forEach((l) => S.labsOff.add(l));
    if (p.get("m")) S.model = p.get("m");
    if (p.get("a")) S.a = p.get("a");
    if (p.get("b")) S.b = p.get("b");
  }
  function writeHash() {
    const p = new URLSearchParams();
    if (S.votes !== "all") p.set("votes", S.votes);
    if (S.metric !== "cost") p.set("metric", S.metric);
    if (S.stat !== "median") p.set("stat", S.stat);
    if (S.h2h !== "actual") p.set("h2h", S.h2h);
    if (S.labsOff.size) p.set("labs", [...S.labsOff].join(","));
    if (S.model) p.set("m", S.model);
    if (S.a) p.set("a", S.a);
    if (S.b) p.set("b", S.b);
    const h = p.toString();
    history.replaceState(null, "", h ? "#" + h : location.pathname + location.search);
  }

  // ---------- data helpers ----------
  const ds = () => D.datasets[S.votes];
  const allRows = () => ds().models;
  const visible = (m) => !S.labsOff.has(m.lab) && (!S.q || (m.name + " " + m.lab).toLowerCase().includes(S.q));
  const rows = () => allRows().filter(visible);
  const key = () => S.metric + (S.stat === "median" ? "Median" : "Mean");
  const val = (m) => m[key()];
  const det = (slug) => (D.models[slug] || {}).details || {};
  const fmtV = (v, metric) => v == null ? "—" : (metric || S.metric) === "cost" ? "$" + (v < 0.1 ? v.toFixed(3) : v.toFixed(2)) : v.toFixed(1) + " min";
  const pct = (p, d) => p == null ? "—" : (p > 0 && p < 0.005 ? "<1%" : (p * 100).toFixed(d || 0) + "%");
  const pWin = (ea, eb) => 1 / (1 + Math.pow(10, (eb - ea) / 400));
  const bySlug = (slug) => allRows().find((m) => m.slug === slug);
  const dot = (slug) => `<span class="dot" style="background:${color[slug] || "#999"}"></span>`;
  const sgt = (iso) => iso ? new Date(iso).toLocaleString("en-SG", { timeZone: "Asia/Singapore", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }) + " SGT" : "—";

  function tiers(list) {   // list sorted by rank; tier = models whose hi >= tier leader's lo
    const out = {}; let t = 0, i = 0;
    const arr = list.slice().sort((a, b) => a.rank - b.rank);
    while (i < arr.length) {
      t++; const lead = arr[i];
      while (i < arr.length && arr[i].hi >= lead.lo) { out[arr[i].slug] = t; i++; }
    }
    return out;
  }
  function frontier(list, k) {      // lower value + higher Elo = better
    const pts = list.filter((m) => m[k] != null).sort((a, b) => a[k] - b[k] || b.elo - a.elo);
    const f = []; let best = -Infinity;
    for (const m of pts) if (m.elo > best) { f.push(m); best = m.elo; }
    return f;
  }
  function prevDay() {      // last history snapshot before today's (for rank deltas)
    if (!H || !H.days) return null;
    const days = Object.keys(H.days).sort();
    const today = new Date(D.fetchedAt).toLocaleDateString("en-CA", { timeZone: "Asia/Singapore" });
    const prev = days.filter((d) => d < today);
    return prev.length ? H.days[prev[prev.length - 1]] : null;
  }

  // ---------- tooltip ----------
  const tip = $("#tip");
  function showTip(html, e) {
    tip.innerHTML = html; tip.hidden = false;
    const x = e.clientX + 14, y = e.clientY + 14, w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(x, innerWidth - w - 8) + "px";
    tip.style.top = (y + h > innerHeight - 8 ? e.clientY - h - 10 : y) + "px";
  }
  const hideTip = () => { tip.hidden = true; };
  document.addEventListener("scroll", hideTip, { passive: true });

  // ---------- header bits ----------
  function renderStamp() {
    const n = allRows().length;
    const battles = Object.values(D.models).reduce((s, m) => s + ((m.details || {}).battles || 0), 0) / 2;
    $("#stamp").innerHTML = `Agent Duel computed ${esc(sgt(ds().generatedAt))} · copied here ${esc(sgt(D.fetchedAt))} · ${n} ranked agents · ~${Math.round(battles).toLocaleString()} eligible battles`;
    const a = D.datasets.all.models[0], b = D.datasets.noEarly.models[0];
    const f = $("#flip");
    if (a && b && a.slug !== b.slug) {
      f.hidden = false;
      f.innerHTML = S.votes === "all"
        ? `Leaving out early votes changes the leader: <b>${esc(b.name)}</b> goes to #1 (${pct(b.pFirst)} chance of being best) and <b>${esc(a.name)}</b> drops to #${bySlugIn("noEarly", a.slug)}. <a href="#" data-flip>Show that view</a>`
        : `With all votes counted, <b>${esc(a.name)}</b> leads instead (${pct(a.pFirst)} chance of being best). <a href="#" data-flip>Show all votes</a>`;
    } else f.hidden = true;
  }
  function bySlugIn(dsKey, slug) { const m = D.datasets[dsKey].models.find((x) => x.slug === slug); return m ? m.rank : "—"; }

  function renderControls() {
    $$(".seg button[data-k]").forEach((b) => b.classList.toggle("on", S[b.dataset.k] === b.dataset.v));
    const labs = [...new Set(D.datasets.all.models.concat(D.datasets.noEarly.models).map((m) => m.lab))];
    $("#labs").innerHTML = labs.map((l) => `<button type="button" class="chip${S.labsOff.has(l) ? " off" : ""}" data-lab="${esc(l)}">${esc(l)}</button>`).join("");
  }

  // ---------- ranking table ----------
  const COLS = [
    { k: "rank", t: "Rank", l: false, v: (m) => m.rank },
    { k: "name", t: "Agent", l: true, v: (m) => m.name.toLowerCase() },
    { k: "elo", t: "Elo", v: (m) => m.elo },
    { k: "ci", t: "95% range", v: (m) => m.hi - m.lo, nosort: false },
    { k: "pFirst", t: "Chance #1", v: (m) => m.pFirst || 0 },
    { k: "rr", t: "Rank range", v: (m) => m.rankRange95 ? m.rankRange95[1] : 99 },
    { k: "win", t: "Win rate", v: (m) => det(m.slug).rawWinRate || 0 },
    { k: "battles", t: "Battles", v: (m) => det(m.slug).battles || 0 },
    { k: "cost", t: "Cost / task", v: (m) => m["cost" + (S.stat === "median" ? "Median" : "Mean")] ?? 1e9 },
    { k: "time", t: "Time / task", v: (m) => m["time" + (S.stat === "median" ? "Median" : "Mean")] ?? 1e9 },
    { k: "tps", t: "Tok/s", v: (m) => det(m.slug).tps || 0 },
    { k: "trend", t: "Weekly Elo", v: (m) => { const w = (D.models[m.slug] || {}).weekly || []; return w.length > 1 ? w[w.length - 1].elo - w[0].elo : 0; } },
  ];
  function renderTable() {
    const list = rows();
    const all = allRows();
    const lo = Math.min(...all.map((m) => m.lo)) - 10, hi = Math.max(...all.map((m) => m.hi)) + 10;
    const X = (v) => ((v - lo) / (hi - lo)) * 100;
    const tier = tiers(all);
    const prev = prevDay(); const prevRanks = prev ? prev[S.votes] || {} : null;
    const fr = new Set(frontier(all, key()).map((m) => m.slug));
    const c = COLS.find((x) => x.k === S.sort.k);
    const sorted = list.slice().sort((a, b) => { const va = c.v(a), vb = c.v(b); return (va < vb ? -1 : va > vb ? 1 : 0) * S.sort.dir || a.rank - b.rank; });
    const lead = all[0];
    const ticks = []; for (let t = Math.ceil(lo / 50) * 50; t <= hi; t += 50) ticks.push(t);

    $("#lb thead").innerHTML = "<tr>" + COLS.map((x) => `<th data-k="${x.k}" class="${x.l ? "l" : ""}${x.k === S.sort.k ? " sorted" : ""}" title="Sort by ${esc(x.t)}">${esc(x.t)}${x.k === S.sort.k ? `<span class="ar">${S.sort.dir > 0 ? "▲" : "▼"}</span>` : ""}</th>`).join("") + "</tr>";
    let html = "", lastTier = 0;
    const byRank = S.sort.k === "rank" && S.sort.dir === 1;
    for (const m of sorted) {
      if (byRank && tier[m.slug] !== lastTier) {
        lastTier = tier[m.slug];
        const n = all.filter((x) => tier[x.slug] === lastTier).length;
        html += `<tr class="tierrow"><td colspan="${COLS.length}">Tier ${lastTier}${n > 1 ? ` · ${n} agents statistically tied` : ""}</td></tr>`;
      }
      const d = det(m.slug);
      let delta = "";
      if (prevRanks && prevRanks[m.slug]) {
        const dr = prevRanks[m.slug][0] - m.rank;
        delta = dr > 0 ? `<span class="delta up" title="Up ${dr} since the previous daily copy">▲${dr}</span>` : dr < 0 ? `<span class="delta down" title="Down ${-dr} since the previous daily copy">▼${-dr}</span>` : `<span class="delta flat">–</span>`;
      } else if (prevRanks) delta = `<span class="delta up" title="New since the previous daily copy">new</span>`;
      const w = (D.models[m.slug] || {}).weekly || [];
      html += `<tr data-slug="${esc(m.slug)}" class="${S.model === m.slug ? "hl" : ""}">
        <td class="rk">${m.rank}${delta}</td>
        <td class="l">${dot(m.slug)} <span class="mname">${esc(m.name)}</span><span class="mlab">${esc(m.lab)}</span>${fr.has(m.slug) ? `<span class="front" title="On the ${S.metric} Pareto frontier: no other agent is both stronger and ${S.metric === "cost" ? "cheaper" : "faster"}">best value</span>` : ""}</td>
        <td><b>${m.elo}</b> <span class="hint">+${m.plus}/−${m.minus}</span></td>
        <td><span class="cibar" title="95% interval ${m.lo}–${m.hi}">
          <span class="lead" style="left:${X(lead.lo)}%;width:${X(lead.hi) - X(lead.lo)}%" title="Leader's range"></span>
          ${ticks.map((t) => `<span class="g" style="left:${X(t)}%"></span>`).join("")}
          <span class="w" style="left:${X(m.lo)}%;width:${X(m.hi) - X(m.lo)}%"></span>
          <span class="c" style="left:${X(m.lo)}%"></span><span class="c" style="left:${X(m.hi)}%"></span>
          <span class="e" style="left:${X(m.elo)}%;background:${color[m.slug]}"></span></span></td>
        <td><span class="pbar" style="width:${Math.round((m.pFirst || 0) * 60)}px"></span>${pct(m.pFirst)}</td>
        <td>${m.rankRange95 ? m.rankRange95.join("–") : "—"}</td>
        <td>${pct(d.rawWinRate)}</td>
        <td>${d.battles != null ? d.battles.toLocaleString() : "—"}</td>
        <td>${fmtV(m["cost" + (S.stat === "median" ? "Median" : "Mean")], "cost")}</td>
        <td>${fmtV(m["time" + (S.stat === "median" ? "Median" : "Mean")], "time")}</td>
        <td>${d.tps != null ? d.tps : "—"}</td>
        <td>${spark(w, m.slug)}</td></tr>`;
    }
    if (!sorted.length) html = `<tr><td colspan="${COLS.length}" class="l hint">No agents match the filters.</td></tr>`;
    $("#lb tbody").innerHTML = html;
    // tick labels under the CI column header
    const th = $('#lb th[data-k="ci"]');
    th.title = `95% interval of the Elo score; shaded band = leader's interval. Axis ${ticks[0]}–${ticks[ticks.length - 1]}`;
  }
  function spark(w, slug) {
    if (!w || w.length < 2) return `<span class="hint">${w && w.length ? w[0].elo : "—"}</span>`;
    const W = 80, Hh = 20, ys = w.map((p) => p.elo), mn = Math.min(...ys) - 2, mx = Math.max(...ys) + 2;
    const pts = w.map((p, i) => `${(i / (w.length - 1)) * W},${Hh - ((p.elo - mn) / (mx - mn)) * Hh}`).join(" ");
    const d = ys[ys.length - 1] - ys[0];
    return `<svg class="spark" width="${W + 26}" height="${Hh}" viewBox="0 0 ${W + 26} ${Hh}" aria-label="Weekly Elo ${ys.join(", ")}"><polyline points="${pts}" fill="none" stroke="${color[slug]}" stroke-width="1.6"/><text x="${W + 4}" y="${Hh - 6}" font-size="9" fill="${d >= 0 ? "#15803d" : "#b91c1c"}">${d >= 0 ? "+" : ""}${d}</text></svg>`;
  }

  // ---------- scatter ----------
  function logTicks(a, b, metric) {
    const c = metric === "cost" ? [0.01, 0.03, 0.1, 0.3, 1, 3, 10, 30] : [0.5, 1, 2, 5, 10, 20, 50, 100];
    return c.filter((t) => t >= a * 0.999 && t <= b * 1.001);
  }
  function budgetRange() {
    const vs = allRows().map(val).filter((v) => v != null);
    return [Math.min(...vs), Math.max(...vs)];
  }
  function budgetValue() {
    const [a, b] = budgetRange(); const s = +$("#budget").value / 100;
    return Math.exp(Math.log(a) + s * (Math.log(b) - Math.log(a)));
  }
  function renderScatter() {
    const el = $("#scatter");
    const W = Math.max(300, el.clientWidth || 600), Hh = Math.round(Math.min(420, Math.max(300, W * 0.62)));
    const m = { l: 44, r: 16, t: 12, b: 36 };
    const all = allRows().filter((x) => val(x) != null);
    const [vmin, vmax] = budgetRange();
    const xa = vmin / 1.6, xb = vmax * 1.6;
    const ya = Math.min(...all.map((x) => x.lo)) - 8, yb = Math.max(...all.map((x) => x.hi)) + 8;
    const X = (v) => m.l + ((Math.log(v) - Math.log(xa)) / (Math.log(xb) - Math.log(xa))) * (W - m.l - m.r);
    const Y = (v) => m.t + (1 - (v - ya) / (yb - ya)) * (Hh - m.t - m.b);
    const budget = budgetValue();
    const f = frontier(all, key());
    let s = `<svg viewBox="0 0 ${W} ${Hh}" role="img" aria-label="Elo against ${S.stat} ${S.metric} per task">`;
    s += `<g class="ax">`;
    for (let t = Math.ceil(ya / 25) * 25; t <= yb; t += 25) s += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${m.l - 6}" y="${Y(t) + 3}" text-anchor="end">${t}</text>`;
    for (const t of logTicks(xa, xb, S.metric)) s += `<line x1="${X(t)}" x2="${X(t)}" y1="${m.t}" y2="${Hh - m.b}"/><text x="${X(t)}" y="${Hh - m.b + 14}" text-anchor="middle">${S.metric === "cost" ? "$" + t : t + "m"}</text>`;
    s += `<text x="${(W + m.l) / 2}" y="${Hh - 4}" text-anchor="middle">${S.stat} ${S.metric === "cost" ? "cost" : "time"} per task (log) — left is ${S.metric === "cost" ? "cheaper" : "faster"}</text>`;
    s += `<text x="10" y="${m.t + 4}" transform="rotate(-90 10 ${m.t + 4})" text-anchor="end">Elo</text></g>`;
    if (budget < vmax * 0.999) s += `<rect x="${X(budget)}" y="${m.t}" width="${W - m.r - X(budget)}" height="${Hh - m.t - m.b}" fill="rgba(0,0,0,.045)"/><line x1="${X(budget)}" x2="${X(budget)}" y1="${m.t}" y2="${Hh - m.b}" stroke="var(--accent)" stroke-width="1"/>`;
    // frontier as a staircase
    if (f.length > 1) {
      let d = `M${X(f[0][key()])},${Y(f[0].elo)}`;
      for (let i = 1; i < f.length; i++) d += ` H${X(f[i][key()])} V${Y(f[i].elo)}`;
      s += `<path class="frontier" d="${d}"/>`;
    }
    // labels: simple vertical relaxation so names don't sit on top of each other
    const pts = all.map((x) => ({ x, px: X(val(x)), py: Y(x.elo), ly: Y(x.elo) }));
    pts.sort((a, b) => a.py - b.py);
    for (let it = 0; it < 40; it++) for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < pts.length; j++) {
      const a = pts[i], b = pts[j];
      if (Math.abs(a.px - b.px) < 120 && Math.abs(a.ly - b.ly) < 12) { const dd = (12 - Math.abs(a.ly - b.ly)) / 2; if (a.ly <= b.ly) { a.ly -= dd; b.ly += dd; } else { a.ly += dd; b.ly -= dd; } }
    }
    for (const p of pts) {
      const x = p.x, on = visible(x), over = val(x) > budget * 1.0001;
      const op = !on ? 0.12 : over ? 0.3 : 1;
      const right = p.px < W - 140;
      s += `<g data-slug="${esc(x.slug)}" opacity="${op}" style="cursor:pointer">
        <line x1="${p.px}" x2="${p.px}" y1="${Y(x.lo)}" y2="${Y(x.hi)}" stroke="${color[x.slug]}" stroke-width="2" opacity=".45"/>
        <circle cx="${p.px}" cy="${p.py}" r="${S.model === x.slug ? 8 : 6}" fill="${color[x.slug]}" stroke="#fff" stroke-width="1.5"/>
        <text class="lbl" x="${p.px + (right ? 10 : -10)}" y="${p.ly + 3.5}" text-anchor="${right ? "start" : "end"}">${esc(x.name)}</text>
        <circle cx="${p.px}" cy="${p.py}" r="14" fill="transparent"/></g>`;
    }
    s += "</svg>";
    el.innerHTML = s;
    $$("g[data-slug]", el).forEach((g) => {
      const x = bySlug(g.dataset.slug);
      g.addEventListener("mousemove", (e) => showTip(`<b>${esc(x.name)}</b> · ${esc(x.lab)}<br>Elo ${x.elo} (${x.lo}–${x.hi}) · rank ${x.rank}<br>${S.stat} cost ${fmtV(x["cost" + (S.stat === "median" ? "Median" : "Mean")], "cost")} · time ${fmtV(x["time" + (S.stat === "median" ? "Median" : "Mean")], "time")}${f.some((q) => q.slug === x.slug) ? "<br>On the frontier" : ""}`, e));
      g.addEventListener("mouseleave", hideTip);
      g.addEventListener("click", () => openModel(x.slug));
    });
    $("#scTitle").textContent = `Score vs ${S.metric}`;
    $("#budgetLbl").textContent = S.metric === "cost" ? "Budget per task" : "Time limit per task";
    $("#budgetOut").textContent = "≤ " + fmtV(budget);
    const within = allRows().filter((x) => visible(x) && val(x) != null && val(x) <= budget * 1.0001).sort((a, b) => b.elo - a.elo);
    const tier = tiers(allRows());
    if (!within.length) $("#budgetPick").innerHTML = `Nothing visible fits within ${fmtV(budget)}.`;
    else {
      const best = within[0];
      const ties = within.filter((x) => x !== best && x.hi >= best.lo).slice(0, 3);
      const cheapest = ties.concat([best]).sort((a, b) => val(a) - val(b))[0];
      $("#budgetPick").innerHTML = `Strongest within ${fmtV(budget)}: ${dot(best.slug)}<b>${esc(best.name)}</b> (Elo ${best.elo}, ${fmtV(val(best))}).` +
        (ties.length ? ` Statistically close: ${ties.map((t) => esc(t.name)).join(", ")}${cheapest !== best ? ` — <b>${esc(cheapest.name)}</b> is the ${S.metric === "cost" ? "cheapest" : "fastest"} of these at ${fmtV(val(cheapest))}` : ""}.` : "") +
        (tier[best.slug] === 1 ? "" : ` (tier ${tier[best.slug]})`);
    }
  }

  // ---------- compare ----------
  function renderCompare() {
    const all = allRows();
    const opts = all.map((m) => `<option value="${esc(m.slug)}">${m.rank}. ${esc(m.name)}</option>`).join("");
    if (!S.a || !bySlug(S.a)) S.a = all[0].slug;
    if (!S.b || !bySlug(S.b) || S.b === S.a) S.b = (all.find((m) => m.slug !== S.a) || all[1]).slug;
    $("#cmpA").innerHTML = opts; $("#cmpB").innerHTML = opts;
    $("#cmpA").value = S.a; $("#cmpB").value = S.b;
    const A = bySlug(S.a), B = bySlug(S.b);
    const p = pWin(A.elo, B.elo);
    const hh = ((D.models[A.slug] || {}).h2h || []).find((r) => r.opp === B.slug);
    let html = `<div class="hint">Elo predicts</div>
      <div class="cmp-bar"><span style="width:${p * 100}%;background:${color[A.slug]}">${esc(A.name)} ${pct(p)}</span><span style="width:${(1 - p) * 100}%;background:${color[B.slug]};justify-content:flex-end">${pct(1 - p)}</span></div>`;
    if (hh && hh.w + hh.t + hh.l > 0) {
      const n = hh.w + hh.t + hh.l;
      html += `<div class="hint">Actual: ${hh.w} win${hh.w === 1 ? "" : "s"}, ${hh.t} tie${hh.t === 1 ? "" : "s"}, ${hh.l} loss${hh.l === 1 ? "" : "es"} in ${n} battle${n > 1 ? "s" : ""}${hh.lo != null ? ` (win rate ${pct(hh.rate)}, 95% ${pct(hh.lo)}–${pct(hh.hi)})` : ""}</div>
      <div class="cmp-bar"><span style="width:${(hh.w / n) * 100}%;background:${color[A.slug]}">${hh.w ? hh.w : ""}</span><span class="tie" style="width:${(hh.t / n) * 100}%">${hh.t ? hh.t : ""}</span><span style="width:${(hh.l / n) * 100}%;background:${color[B.slug]};justify-content:flex-end">${hh.l ? hh.l : ""}</span></div>`;
      if (n < 15) html += `<div class="cmp-note">Few direct battles — treat the actual record as anecdotal; the Elo prediction uses every vote.</div>`;
    } else html += `<div class="cmp-note">These two haven't met in eligible battles yet; the prediction comes from how each did against everyone else.</div>`;
    const dA = det(A.slug), dB = det(B.slug);
    const rowsK = [
      ["Elo", A.elo, B.elo, 1, (v) => v], ["Chance #1", A.pFirst, B.pFirst, 1, (v) => pct(v)],
      ["Cost / task", val(A) != null && S.metric === "cost" ? val(A) : A["cost" + (S.stat === "median" ? "Median" : "Mean")], B["cost" + (S.stat === "median" ? "Median" : "Mean")], -1, (v) => fmtV(v, "cost")],
      ["Time / task", A["time" + (S.stat === "median" ? "Median" : "Mean")], B["time" + (S.stat === "median" ? "Median" : "Mean")], -1, (v) => fmtV(v, "time")],
      ["Tokens / s", dA.tps, dB.tps, 1, (v) => v ?? "—"], ["Raw win rate", dA.rawWinRate, dB.rawWinRate, 1, (v) => pct(v)],
      ["Battles", dA.battles, dB.battles, 0, (v) => v ?? "—"],
    ];
    html += `<div class="cmp-k"><div class="h"></div><div class="h r">${esc(A.name)}</div><div class="h r">${esc(B.name)}</div>` +
      rowsK.map(([k, a, b, dir, f]) => {
        const wa = dir && a != null && b != null && (dir > 0 ? a > b : a < b), wb = dir && a != null && b != null && (dir > 0 ? b > a : b < a);
        return `<div>${k}</div><div class="r${wa ? " win" : ""}">${f(a)}</div><div class="r${wb ? " win" : ""}">${f(b)}</div>`;
      }).join("") + "</div>";
    const r = S.metric === "cost" ? (val(A) && val(B) ? val(A) / val(B) : null) : null;
    if (r) html += `<div class="cmp-note">${esc(A.name)} costs ${r >= 1 ? r.toFixed(1) + "×" : (1 / r).toFixed(1) + "× less than"} ${r >= 1 ? "as much as " : ""}${esc(B.name)} per task (${S.stat}).</div>`;
    $("#cmpOut").innerHTML = html;
  }

  // ---------- head-to-head matrix ----------
  function mix(c1, c2, t) {
    const a = c1.match(/\w\w/g).map((h) => parseInt(h, 16)), b = c2.match(/\w\w/g).map((h) => parseInt(h, 16));
    return "rgb(" + a.map((v, i) => Math.round(v + (b[i] - v) * t)).join(",") + ")";
  }
  function divColor(v) {   // v in [-1, 1]: negative = loss (orange), positive = win (blue)
    v = Math.max(-1, Math.min(1, v));
    return v >= 0 ? mix("f5f5f4", "1d4ed8", v) : mix("f5f5f4", "c2410c", -v);
  }
  function renderMatrix() {
    const list = rows().slice().sort((a, b) => a.rank - b.rank);
    const short = (n) => n.length > 16 ? n.replace(/ \d{4}$/, "") : n;
    let h = `<thead><tr><th></th>${list.map((c) => `<th><div title="${esc(c.name)}">${esc(short(c.name))}</div></th>`).join("")}</tr></thead><tbody>`;
    for (const r of list) {
      h += `<tr><th>${dot(r.slug)} ${esc(short(r.name))}</th>`;
      const hh = (D.models[r.slug] || {}).h2h || [];
      for (const c of list) {
        if (c.slug === r.slug) { h += `<td class="self"></td>`; continue; }
        const x = hh.find((q) => q.opp === c.slug);
        const n = x ? x.w + x.t + x.l : 0;
        const exp = pWin(r.elo, c.elo);
        let text = "", bg = "", op = 1, fg = "";
        if (S.h2h === "expected") { text = Math.round(exp * 100); bg = divColor((exp - 0.5) * 2.5); }
        else if (!n) { h += `<td class="none" data-r="${esc(r.slug)}" data-c="${esc(c.slug)}">–</td>`; continue; }
        else if (S.h2h === "actual") { text = Math.round(x.rate * 100); bg = divColor((x.rate - 0.5) * 2); op = Math.min(1, 0.3 + n / 40); }
        else if (S.h2h === "surprise") { const d = x.rate - exp; text = (d >= 0 ? "+" : "") + Math.round(d * 100); bg = divColor(d * 2.5); op = Math.min(1, 0.3 + n / 40); }
        else { text = n; bg = mix("f5f5f4", "1d1d1d", Math.min(1, n / 80)); fg = n > 45 ? "#fff" : ""; }
        if (!fg) fg = (S.h2h !== "battles" && Math.abs(parseFloat(bg.split(",")[2]) - 245) > 120) ? "#fff" : "";
        h += `<td data-r="${esc(r.slug)}" data-c="${esc(c.slug)}" style="background:${bg};opacity:${op}${fg ? ";color:" + fg : ""}">${text}</td>`;
      }
      h += "</tr>";
    }
    h += "</tbody>";
    $("#mx").innerHTML = h;
    $$("#mx td[data-r]").forEach((td) => {
      td.addEventListener("mousemove", (e) => {
        const r = bySlug(td.dataset.r), c = bySlug(td.dataset.c);
        const x = ((D.models[r.slug] || {}).h2h || []).find((q) => q.opp === c.slug);
        const exp = pWin(r.elo, c.elo);
        showTip(`<b>${esc(r.name)}</b> vs <b>${esc(c.name)}</b><br>` +
          (x && x.w + x.t + x.l ? `${x.w} W · ${x.t} T · ${x.l} L (${x.w + x.t + x.l} battles)<br>Actual win rate ${pct(x.rate)}${x.lo != null ? ` (95% ${pct(x.lo)}–${pct(x.hi)})` : ""}<br>` : "No eligible battles yet<br>") +
          `Elo-predicted ${pct(exp)}`, e);
      });
      td.addEventListener("mouseleave", hideTip);
      td.addEventListener("click", () => { S.a = td.dataset.r; S.b = td.dataset.c; writeHash(); renderCompare(); $("#compareCard").scrollIntoView({ behavior: "smooth", block: "center" }); });
    });
  }

  // ---------- rank odds ----------
  function renderOdds() {
    const list = rows().slice().sort((a, b) => a.rank - b.rank);
    const N = allRows().length;
    let h = `<thead><tr><th></th>${Array.from({ length: N }, (_, i) => `<th>${i + 1}</th>`).join("")}</tr></thead><tbody>`;
    for (const m of list) {
      const p = (D.models[m.slug] || {}).rankProb || [];
      const rr = (D.models[m.slug] || {}).rankRange95 || m.rankRange95 || [0, 0];
      h += `<tr><th>${dot(m.slug)} ${esc(m.name.length > 18 ? m.name.replace(/ \d{4}$/, "") : m.name)}</th>`;
      for (let i = 0; i < N; i++) {
        const v = p[i] || 0, a = Math.min(1, v * 1.6);
        h += `<td class="${i + 1 >= rr[0] && i + 1 <= rr[1] ? "in" : ""}" style="background:rgba(255,74,28,${a.toFixed(3)})${a > 0.55 ? ";color:#fff" : ""}" title="${esc(m.name)}: rank ${i + 1} in ${pct(v)} of resamples">${v >= 0.005 ? Math.round(v * 100) : v > 0 ? "·" : ""}</td>`;
      }
      h += "</tr>";
    }
    $("#odds").innerHTML = h + "</tbody>";
  }

  // ---------- weekly trend ----------
  function renderTrend() {
    const el = $("#trend");
    const W = Math.max(300, el.clientWidth || 500), Hh = Math.round(Math.min(340, Math.max(240, W * 0.6)));
    const m = { l: 40, r: 12, t: 10, b: 26 };
    const list = rows().filter((x) => ((D.models[x.slug] || {}).weekly || []).length);
    const weeks = [...new Set(list.flatMap((x) => D.models[x.slug].weekly.map((w) => w.week)))].sort();
    if (!weeks.length) { el.innerHTML = `<p class="hint">No weekly data.</p>`; return; }
    const shown = list.filter((x) => !S.hidden.has(x.slug));
    const ys = shown.flatMap((x) => D.models[x.slug].weekly.map((w) => w.elo));
    const ya = Math.min(...ys, 1000) - 15, yb = Math.max(...ys, 1000) + 15;
    const X = (wk) => m.l + (weeks.length === 1 ? 0.5 : weeks.indexOf(wk) / (weeks.length - 1)) * (W - m.l - m.r);
    const Y = (v) => m.t + (1 - (v - ya) / (yb - ya)) * (Hh - m.t - m.b);
    let s = `<svg viewBox="0 0 ${W} ${Hh}" role="img" aria-label="Weekly Elo by agent"><g class="ax">`;
    for (let t = Math.ceil(ya / 25) * 25; t <= yb; t += 25) s += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${m.l - 5}" y="${Y(t) + 3}" text-anchor="end">${t}</text>`;
    const step = Math.ceil(weeks.length / Math.max(2, Math.floor(W / 80)));
    weeks.forEach((wk, i) => { if (i % step === 0 || i === weeks.length - 1) s += `<text x="${X(wk)}" y="${Hh - 8}" text-anchor="middle">${new Date(wk + "T00:00:00").toLocaleDateString("en-GB", { day: "numeric", month: "short" })}</text>`; });
    s += `</g><g id="band"></g>`;
    for (const x of shown) {
      const w = D.models[x.slug].weekly;
      const pts = w.map((p) => `${X(p.week)},${Y(p.elo)}`);
      const part = D.models[x.slug].weeklyPartialLast && w.length > 1;
      const solid = part ? pts.slice(0, -1) : pts;
      s += `<g class="tl" data-slug="${esc(x.slug)}">`;
      if (solid.length > 1) s += `<polyline points="${solid.join(" ")}" fill="none" stroke="${color[x.slug]}" stroke-width="2"/>`;
      if (part) s += `<polyline points="${pts.slice(-2).join(" ")}" fill="none" stroke="${color[x.slug]}" stroke-width="2" stroke-dasharray="3 3"/>`;
      s += w.map((p) => `<circle cx="${X(p.week)}" cy="${Y(p.elo)}" r="2.6" fill="${color[x.slug]}"/>`).join("");
      s += `<polyline points="${pts.join(" ")}" fill="none" stroke="transparent" stroke-width="12"/>`;
      if (w.length === 1) s += `<circle cx="${X(w[0].week)}" cy="${Y(w[0].elo)}" r="9" fill="transparent"/>`;
      s += `</g>`;
    }
    el.innerHTML = s + "</svg>";
    $$("g.tl", el).forEach((g) => {
      const x = bySlug(g.dataset.slug), w = D.models[x.slug].weekly;
      g.addEventListener("mousemove", (e) => {
        $$("g.tl", el).forEach((o) => o.setAttribute("opacity", o === g ? 1 : 0.15));
        const band = w.filter((p) => p.lo != null);
        $("#band", el).innerHTML = band.length > 1 ? `<path d="M${band.map((p) => `${X(p.week)},${Y(p.hi)}`).join(" L")} L${band.slice().reverse().map((p) => `${X(p.week)},${Y(p.lo)}`).join(" L")} Z" fill="${color[x.slug]}" opacity=".12"/>` : "";
        // nearest week to the pointer
        const rect = el.getBoundingClientRect(), px = ((e.clientX - rect.left) / rect.width) * W;
        const near = w.reduce((a, b) => Math.abs(X(b.week) - px) < Math.abs(X(a.week) - px) ? b : a);
        showTip(`<b>${esc(x.name)}</b><br>Week of ${esc(new Date(near.week + "T00:00:00").toLocaleDateString("en-GB", { day: "numeric", month: "short" }))}: Elo ${near.elo}${near.lo != null ? ` (${near.lo}–${near.hi})` : ""}`, e);
      });
      g.addEventListener("mouseleave", () => { $$("g.tl", el).forEach((o) => o.setAttribute("opacity", 1)); $("#band", el).innerHTML = ""; hideTip(); });
      g.addEventListener("click", () => openModel(x.slug));
    });
    $("#trendLegend").innerHTML = list.map((x) => `<button type="button" data-slug="${esc(x.slug)}" class="${S.hidden.has(x.slug) ? "off" : ""}">${dot(x.slug)}${esc(x.name)}</button>`).join("");
  }

  // ---------- drawer ----------
  function openModel(slug) { S.model = slug; writeHash(); renderDrawer(); renderTable(); renderScatter(); }
  function closeModel() { S.model = null; writeHash(); $("#drawer").hidden = true; renderTable(); renderScatter(); }
  function renderDrawer() {
    const m = bySlug(S.model) || D.datasets.all.models.find((x) => x.slug === S.model);
    const md = D.models[S.model];
    if (!m || !md) { $("#drawer").hidden = true; return; }
    const d = md.details || {};
    const other = S.votes === "all" ? "noEarly" : "all";
    const om = D.datasets[other].models.find((x) => x.slug === m.slug);
    const row = (k, v) => v == null || v === "" ? "" : `<tr><th>${k}</th><td class="r">${v}</td></tr>`;
    let h = `<h3>${dot(m.slug)} ${esc(m.name)}</h3><div class="sub">${esc(m.lab)} · ${esc(md.slug || m.slug)} · <a href="${AD}/leaderboard/models/${esc(m.slug)}" rel="noopener">Agent Duel page ↗</a></div>
      <table>${row("Rank", `${m.rank} (95% range ${m.rankRange95 ? m.rankRange95.join("–") : "—"})`)}${row("Elo", `${m.elo} (${m.lo}–${m.hi})`)}
      ${om ? row(other === "noEarly" ? "Without early votes" : "With all votes", `rank ${om.rank}, Elo ${om.elo}`) : ""}
      ${row("Chance of being #1", pct(m.pFirst))}${row("Eligible battles", d.battles)}${row("Unique voters", d.voters)}${row("Raw win rate", pct(d.rawWinRate))}
      ${row("Cost / task", `${fmtV(d.costMedian, "cost")} median · ${fmtV(d.costMean, "cost")} mean`)}
      ${row("Time / task", `${fmtV(d.timeMedian, "time")} median · ${fmtV(d.timeMean, "time")} mean`)}
      ${row("Tokens / second", d.tps)}${row("Thinking", esc(d.thinking))}${row("Served through", esc(d.servedThrough))}
      ${row("Version", esc(d.version))}${row("First eligible vote", d.firstVote ? new Date(d.firstVote + "T00:00:00").toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : null)}</table>`;
    const hh = (md.h2h || []).slice().sort((a, b) => (b.w + b.t + b.l) - (a.w + a.t + a.l));
    if (hh.length) {
      h += `<h4>Head-to-head (most-played first)</h4><table>${hh.map((x) => {
        const n = x.w + x.t + x.l;
        return `<tr><th>${dot(x.opp)} ${esc(x.oppName)}</th><td><span class="wtl" title="${x.w} W · ${x.t} T · ${x.l} L"><span style="width:${(x.w / n) * 100}%;background:#1d4ed8"></span><span style="width:${(x.t / n) * 100}%;background:#9ca3af"></span><span style="width:${(x.l / n) * 100}%;background:#c2410c"></span></span></td><td class="r">${x.w}-${x.t}-${x.l}</td><td class="r">${pct(x.rate)}</td></tr>`;
      }).join("")}</table>`;
    }
    if ((md.weekly || []).length) h += `<h4>Elo by week</h4><table>${md.weekly.map((w, i) => `<tr><th>${esc(new Date(w.week + "T00:00:00").toLocaleDateString("en-GB", { day: "numeric", month: "short" }))}${i === md.weekly.length - 1 && md.weeklyPartialLast ? " (partial)" : ""}</th><td class="r">${w.elo}</td><td class="r hint">${w.lo != null ? w.lo + "–" + w.hi : ""}</td></tr>`).join("")}</table>`;
    if (d.other) h += `<h4>More</h4><table>${Object.entries(d.other).map(([k, v]) => row(esc(k), esc(v))).join("")}</table>`;
    $("#drawerBody").innerHTML = h;
    $("#drawer").hidden = false;
  }

  // ---------- render all ----------
  function renderAll() {
    renderControls(); renderStamp(); renderTable(); renderScatter(); renderCompare(); renderMatrix(); renderOdds(); renderTrend();
    if (S.model) renderDrawer();
  }

  function wire() {
    document.addEventListener("click", (e) => {
      const b = e.target.closest(".seg button[data-k]");
      if (b) { S[b.dataset.k] = b.dataset.v; if (b.dataset.k === "metric" || b.dataset.k === "stat") $("#budget").value = 100; writeHash(); renderAll(); return; }
      const chip = e.target.closest(".chip[data-lab]");
      if (chip) { const l = chip.dataset.lab; S.labsOff.has(l) ? S.labsOff.delete(l) : S.labsOff.add(l); writeHash(); renderAll(); return; }
      const th = e.target.closest("#lb th[data-k]");
      if (th) { const k = th.dataset.k; S.sort = { k, dir: S.sort.k === k ? -S.sort.dir : (["rank", "name", "rr", "cost", "time"].includes(k) ? 1 : -1) }; renderTable(); return; }
      const tr = e.target.closest("#lb tbody tr[data-slug]");
      if (tr) { openModel(tr.dataset.slug); return; }
      const lg = e.target.closest("#trendLegend button");
      if (lg) { const s = lg.dataset.slug; S.hidden.has(s) ? S.hidden.delete(s) : S.hidden.add(s); renderTrend(); return; }
      if (e.target.closest("[data-flip]")) { e.preventDefault(); S.votes = S.votes === "all" ? "noEarly" : "all"; writeHash(); renderAll(); return; }
      if (!$("#drawer").hidden && !e.target.closest("#drawer") && !e.target.closest("[data-slug]") && !e.target.closest("#mx")) closeModel();
    });
    $("#drawerX").addEventListener("click", closeModel);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#drawer").hidden) closeModel(); });
    $("#agSearch").addEventListener("input", (e) => { S.q = e.target.value.trim().toLowerCase(); renderTable(); renderScatter(); renderMatrix(); renderOdds(); renderTrend(); });
    $("#budget").addEventListener("input", renderScatter);
    $("#cmpA").addEventListener("change", (e) => { S.a = e.target.value; if (S.a === S.b) S.b = null; writeHash(); renderCompare(); });
    $("#cmpB").addEventListener("change", (e) => { S.b = e.target.value; writeHash(); renderCompare(); });
    let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { renderScatter(); renderTrend(); }, 150); });
  }

  readHash();
  Promise.all([
    fetch("/data/agents/leaderboard.json", { cache: "no-cache" }).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); }),
    fetch("/data/agents/history.json", { cache: "no-cache" }).then((r) => (r.ok ? r.json() : null)).catch(() => null),
  ]).then(([d, h]) => {
    D = d; H = h;
    const order = D.datasets.all.models.concat(D.datasets.noEarly.models).map((m) => m.slug);
    [...new Set(order)].forEach((s, i) => { color[s] = PALETTE[i % PALETTE.length]; });
    wire(); renderAll();
  }).catch((e) => { $("#stamp").textContent = "Couldn't load the leaderboard data (" + e.message + ")."; });
})();
