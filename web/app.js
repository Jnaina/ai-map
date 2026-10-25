/* AI Map front end: a minimal, monochrome force-directed graph of AI entities and their "heat".
   Topics (concepts) are diamonds, everything else is a small dot; heat is shown by darkness.
   One accent colour marks the hottest topic, its cluster, and entities that are new this week.

   Live updates: the backend rebuilds hourly and pushes a fresh data.json to the edge. An open
   page polls a tiny freshness token (data/new_counts.json) and, when the build changes, either
   applies the new data silently (if the viewer is idle) or offers a subtle "Fresh data" pill
   (if they're actively exploring). Applying crossfades and re-lays-out the graph in place —
   zoom, filters and the selected entity are preserved — instead of a brute-force page reload. */
(async function () {
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const ago = (iso) => {
    const h = (Date.now() - new Date(iso).getTime()) / 36e5;
    if (h < 1) return "just now";
    if (h < 24) return `${Math.round(h)} h ago`;
    return `${Math.round(h / 24)} d ago`;
  };
  const TYPES = ["Company", "Model", "Product", "Open source", "Person", "Hardware", "Concept"];
  const TYPE_LABEL = { "Concept": "Topics" };
  const arrow = (d) => (d.trend >= 1.5 ? " ↑" : d.trend <= 0.75 ? " ↓" : "");

  const view = new URLSearchParams(location.search).get("view") === "today" ? "today" : "week";
  document.querySelectorAll(".views a").forEach((a) => a.classList.toggle("on", a.dataset.view === view));

  // ---- graph state, reassigned on every (re)build so the shared functions below always see the
  //      current data. Kept as `let` at this scope precisely so a live update can swap them in.
  let data, nodes, byId, links, neigh, hottestTopic, topicMax, lag;
  let svg, root, hullLayer, linkSel, nodeSel, labelSel, sim, zoom, tip;
  let zoomK = 1;
  let selected = null, selectedId = null;       // selectedId survives a rebuild; selected is a live node
  const activeTypes = new Set(TYPES);            // type filter — persists across live updates
  const MOBILE = window.matchMedia("(max-width: 760px), (pointer: coarse)").matches;
  let archive = null;
  let firstBuild = true;

  const stage = $("#stage");
  const W = () => stage.clientWidth, H = () => stage.clientHeight;

  // ============================================================================================
  //  Shared behaviours (defined once; they read the mutable state above, so a rebuild is enough)
  // ============================================================================================
  function visible(d) {
    const st = $("#status").value;
    return activeTypes.has(d.type) && (st === "all" || (st === "breaking" ? (!!d.breaking || !!d.surging) : d.status === st));
  }
  function applyFilters() {
    nodeSel.style("display", (d) => (visible(d) ? null : "none"));
    labelSel.style("display", (d) => (visible(d) ? null : "none"));
    linkSel.style("display", (l) => (visible(l.source) && visible(l.target) ? null : "none"));
    drawHull();
  }

  function highlight(d) {
    if (!d) { nodeSel.classed("faded", false); labelSel.classed("faded", false); linkSel.classed("faded", false); return; }
    const nb = neigh.get(d.id);
    nodeSel.classed("faded", (n) => n !== d && !nb.has(n.id));
    labelSel.classed("faded", (n) => n !== d && !nb.has(n.id));
    linkSel.classed("faded", (l) => l.source !== d && l.target !== d);
  }
  function select(d) {
    selected = d;
    selectedId = d ? d.id : null;
    nodeSel.classed("sel", (n) => n === d);
    highlight(d); drawHull();
    if (!d) { $("#panel").hidden = true; return; }
    renderPanel(d);
  }

  // cluster hull: the selected node (or, by default, the hottest topic) and its closest neighbours
  function cluster() {
    const c = selected || hottestTopic;
    if (!c || !visible(c)) return null;
    const nb = [...neigh.get(c.id).entries()].sort((a, b) => b[1] - a[1]).slice(0, 6).map(([id]) => byId.get(id)).filter(visible);
    return nb.length >= 2 ? { center: c, members: [c, ...nb] } : null;
  }
  function drawHull() {
    hullLayer.selectAll("*").remove();
    const cl = cluster();
    if (!cl) return;
    const pts = [];
    cl.members.forEach((n) => {
      const r = (n.topic ? n.size : 8) + 22;
      for (let a = 0; a < 8; a++) pts.push([n.x + r * Math.cos(a * Math.PI / 4), n.y + r * Math.sin(a * Math.PI / 4)]);
    });
    const hull = d3.polygonHull(pts);
    if (!hull) return;
    hullLayer.append("path").attr("class", "hull").attr("d", "M" + hull.join("L") + "Z");
    const top = hull.reduce((a, b) => (b[1] < a[1] ? b : a));
    const tag = hullLayer.append("g").attr("class", "hullTag").attr("transform", `translate(${top[0] - 20},${top[1] - 34})`);
    tag.append("rect").attr("width", 30 + cl.center.label.length * 7.4).attr("height", 24);
    tag.append("text").attr("x", 9).attr("y", 16).html(`<tspan fill="#ff4a1c">◆</tspan> ${esc(cl.center.label)}`);
  }

  function spark(series) {
    if (!series || series.length < 2) return '<p class="meta">trend line appears after a few refreshes</p>';
    const v = series.map((s) => s[1]), max = Math.max(...v), min = Math.min(...v);
    const pts = v.map((y, i) => `${(i / (v.length - 1)) * 300},${36 - ((y - min) / (max - min || 1)) * 32}`).join(" ");
    return `<svg class="spark" viewBox="0 0 300 40" preserveAspectRatio="none" aria-label="Heat over recent refreshes"><polyline points="${pts}" fill="none" stroke="#1d1d1d" stroke-width="1.5"/></svg>`;
  }

  // Star-history sparkline for a trending repo: absolute star counts over up to 30 daily snapshots,
  // with a "+N★ this week" caption. Separate from spark() (which plots a node's recent heat).
  function starSpark(d) {
    const s = d.star_series || [];
    const g = d.star_growth || {};
    const latest = s.length ? s[s.length - 1].s : null;
    if (s.length < 2) {
      return latest != null ? `<p class="meta">★ ${latest.toLocaleString()} · star history builds over the next few days</p>` : "";
    }
    const v = s.map((p) => p.s), max = Math.max(...v), min = Math.min(...v);
    const pts = v.map((y, i) => `${(i / (v.length - 1)) * 300},${36 - ((y - min) / (max - min || 1)) * 32}`).join(" ");
    const wk = g.d7 != null ? `+${g.d7.toLocaleString()}★ this week${g.pct7 != null ? ` (${Math.round(g.pct7 * 100)}%)` : ""}`
      : (g.d1 != null ? `+${g.d1.toLocaleString()}★ today` : "star history");
    return `<div class="starwrap"><div class="starhead"><span>★ ${v[v.length - 1].toLocaleString()}</span>`
      + `<span class="${d.surging ? "surge" : "meta"}">${wk}</span></div>`
      + `<svg class="spark starspark" viewBox="0 0 300 40" preserveAspectRatio="none" aria-label="GitHub stars over time">`
      + `<polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="1.5"/></svg></div>`;
  }

  function renderPanel(d) {
    const nb = [...neigh.get(d.id).entries()].sort((a, b) => b[1] - a[1]).slice(0, 16).map(([id]) => byId.get(id));
    const trendTxt = d.trend >= 9.9 ? `new in last ${lag} h` : `${d.trend >= 1 ? "+" : ""}${Math.round((d.trend - 1) * 100)}% vs ${lag} h ago`;
    const parent = d.parent && byId.get(d.parent);
    $("#panelBody").innerHTML = `
      <h2>${esc(d.label)}${arrow(d)}</h2>
      <div class="badges">
        <span>${esc(d.topic ? "topic" : d.type.toLowerCase())}</span>
        ${d.breaking ? `<span class="badge breaking">breaking: ${d.breaking.last6h} stories in last 6 h (${d.breaking.ratio}× the 6 h before)</span>` : ""}
        ${d.surging ? '<span class="badge surging">surging on GitHub</span>' : ""}
        ${d.status === "new" ? '<span class="badge new">new this week</span>' : ""}
        ${d.status === "rising" ? '<span class="badge rising">rising</span>' : ""}
        ${parent ? `<span>part of ${esc(parent.label)}</span>` : ""}
      </div>
      <div class="stats">
        <div class="stat"><b>#${d.rank}</b><span>of ${nodes.length} by heat</span></div>
        <div class="stat"><b>${d.heat.toFixed(1)}</b><span>${esc(trendTxt)}</span></div>
        <div class="stat"><b>${d.stories}</b><span>stories ${view === "today" ? "today" : "this week"}</span></div>
      </div>
      ${d.star_series ? starSpark(d) : spark(d.series)}
      <p class="meta">first spotted ${new Date(d.first_seen).toLocaleDateString(undefined, { day: "numeric", month: "short" })} · ${Object.entries(d.sources).map(([s, n]) => `${esc(s)} ${n}`).join(" · ")}</p>
      <h3>Top stories</h3>
      <ul class="stories">${d.top_stories.slice().sort((a, b) => new Date(b.published) - new Date(a.published)).map((s) => `
        <li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>
        <div class="meta">${esc(s.source)} · ${ago(s.published)}${s.discussion ? ` · <a href="${esc(s.discussion)}" target="_blank" rel="noopener">discussion</a>` : ""}</div></li>`).join("")}</ul>
      ${nb.length ? `<h3>Connected</h3><div class="conn">${nb.map((n) => `<span data-id="${esc(n.id)}" role="button" tabindex="0">${esc(n.label)}</span>`).join("")}</div>` : ""}`;
    $("#panel").hidden = false;
    $("#panelBody").querySelectorAll(".conn span").forEach((c) => c.addEventListener("click", () => focusNode(byId.get(c.dataset.id))));
  }

  function focusNode(d) {
    if (!d) return;
    const k = Math.max(zoomK, 1.5);
    const wide = window.innerWidth > 760;
    svg.transition().duration(650).call(zoom.transform,
      d3.zoomIdentity.translate(W() / 2 - (wide ? 190 : 0), H() / 2 - (wide ? 0 : 120)).scale(k).translate(-d.x, -d.y));
    select(d);
  }

  // Lazy-loaded flat list of the 30-day article archive, so search can surface news for entities
  // that aren't big enough to be nodes on the map. Fetched once per build (cleared on live update).
  async function getArchive() {
    if (archive) return archive;
    archive = [];
    try {
      const a = await (await fetch("data/articles.json", { cache: "no-store" })).json();
      const seen = new Set();
      for (const [source, items] of Object.entries(a.articles || {})) {
        for (const it of items) {
          if (!it.url || seen.has(it.url)) continue;
          seen.add(it.url);
          archive.push({
            title: it.title || "", url: it.url, source,
            published: new Date((+it.published || 0) * 1000).toISOString(),
            discussion: it.discussion && it.discussion !== "None" ? it.discussion : null,
          });
        }
      }
      archive.sort((x, y) => new Date(y.published) - new Date(x.published));
    } catch (e) { /* archive unavailable: search just falls back to nodes only */ }
    return archive;
  }

  function renderBreakingPanel() {
    const score = (n) => (n.breaking ? (n.breaking.ratio || 0) : 0) + (n.surging ? 1 + (n.star_growth?.pct7 || 0) : 0);
    const br = nodes.filter((n) => n.breaking || n.surging).sort((a, b) => score(b) - score(a));
    select(null); // clear any node selection, then show our own panel
    if (!br.length) {
      $("#panelBody").innerHTML = `<h2>Breaking now</h2><p class="meta">Nothing is breaking right now.</p>`;
      $("#panel").hidden = false;
      return;
    }
    // Per entity, show only the latest news headline(s) driving the surge — prefer actual news
    // articles over the mixed feed (HN discussions, GitHub repos, papers); newest first, max 2.
    const latestNews = (d) => {
      const all = d.top_stories || [];
      const news = all.filter((s) => s.kind === "news");
      return (news.length ? news : all).slice()
        .sort((a, b) => new Date(b.published) - new Date(a.published)).slice(0, 2);
    };
    $("#panelBody").innerHTML = `
      <h2>Breaking now</h2>
      <div class="badges"><span>${br.length} ${br.length === 1 ? "entity" : "entities"} surging right now</span></div>
      ${br.map((d) => `
        <div class="brk">
          <h3 class="brkHead" data-id="${esc(d.id)}" role="button" tabindex="0" title="Show on the map">${esc(d.label)}
            <span class="badge breaking">${d.breaking ? `${d.breaking.last6h} in 6 h · ${d.breaking.ratio}×`
              : `+${(d.star_growth?.d7 ?? 0).toLocaleString()}★ / 7 d${d.star_growth?.pct7 != null ? ` · ${Math.round(d.star_growth.pct7 * 100)}%` : ""}`}</span></h3>
          <ul class="stories">${latestNews(d).map((s) => `
            <li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>
            <div class="meta">${esc(s.source)} · ${ago(s.published)}${s.discussion ? ` · <a href="${esc(s.discussion)}" target="_blank" rel="noopener">discussion</a>` : ""}</div></li>`).join("")}</ul>
        </div>`).join("")}`;
    $("#panel").hidden = false;
    $("#panelBody").querySelectorAll(".brkHead").forEach((h) =>
      h.addEventListener("click", () => focusNode(byId.get(h.dataset.id))));
  }

  function renderSearchPanel(query, hits) {
    select(null); // clear any node selection/highlight, then show our own panel
    const top = hits.slice(0, 30);
    $("#panelBody").innerHTML = `
      <h2>“${esc(query)}”</h2>
      <div class="badges"><span>search · not on the map</span></div>
      ${top.length ? `<p class="meta">${hits.length} article${hits.length === 1 ? "" : "s"} in the last 30 days with “${esc(query)}” in the headline${hits.length > top.length ? ` · showing ${top.length}` : ""}</p>
      <h3>Headlines</h3>
      <ul class="stories">${top.map((s) => `
        <li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>
        <div class="meta">${esc(s.source)} · ${ago(s.published)}${s.discussion ? ` · <a href="${esc(s.discussion)}" target="_blank" rel="noopener">discussion</a>` : ""}</div></li>`).join("")}</ul>`
      : `<p class="meta">No headlines in the last 30 days mention “${esc(query)}”. It may not have appeared in the sources yet — the archive covers headlines, so very new or niche entities can be missing.</p>`}`;
    $("#panel").hidden = false;
  }

  function fit(animate) {
    const vis = nodes.filter(visible);
    if (!vis.length || vis[0].x === undefined) return;
    const [x0, x1] = d3.extent(vis, (d) => d.x), [y0, y1] = d3.extent(vis, (d) => d.y);
    const padX = Math.min(120, W() * 0.06);
    const padTop = document.querySelector("#types").getBoundingClientRect().bottom + 24;
    const padBottom = W() > 760 ? 95 : 20;
    const k = Math.max(W() > 760 ? 0.3 : 0.55, Math.min((W() - 2 * padX) / Math.max(1, x1 - x0), (H() - padTop - padBottom) / Math.max(1, y1 - y0), 1.6));
    const t = d3.zoomIdentity.translate(W() / 2, padTop + (H() - padTop - padBottom) / 2).scale(k).translate(-(x0 + x1) / 2, -(y0 + y1) / 2);
    (animate ? svg.transition().duration(500) : svg).call(zoom.transform, t);
  }
  function initialView() {
    if (MOBILE) return fit(false);
    const vis = nodes.filter(visible);
    if (!vis.length || vis[0].x === undefined) return fit(false);
    // Desktop: open zoomed into the hottest cluster near the centre, so detail is legible and
    // panning/zooming is discoverable — instead of the fit-all view that sits fully zoomed out.
    const top = vis.slice().sort((a, b) => b.heat - a.heat).slice(0, Math.min(12, vis.length));
    const cx = d3.mean(top, (d) => d.x), cy = d3.mean(top, (d) => d.y);
    const padTop = document.querySelector("#types").getBoundingClientRect().bottom + 24;
    const padBottom = 95;
    const t = d3.zoomIdentity.translate(W() / 2, padTop + (H() - padTop - padBottom) / 2).scale(1.1).translate(-cx, -cy);
    svg.call(zoom.transform, t);
  }

  // ============================================================================================
  //  Build (or rebuild) the whole graph from the current `data`. On a live update, opts.transform
  //  carries the viewer's current pan/zoom so the view doesn't jump.
  // ============================================================================================
  function buildGraph(opts = {}) {
    nodes = data.nodes.map((d, i) => ({ ...d, rank: i + 1, topic: d.type === "Concept" }));
    byId = new Map(nodes.map((n) => [n.id, n]));
    links = data.links.filter((l) => byId.has(l.source) && byId.has(l.target)).map((l) => ({ ...l }));
    neigh = new Map(nodes.map((n) => [n.id, new Map()]));
    links.forEach((l) => { neigh.get(l.source).set(l.target, l.weight); neigh.get(l.target).set(l.source, l.weight); });

    // sizes and tones: hotter = bigger and darker
    topicMax = Math.max(...nodes.filter((n) => n.topic).map((n) => n.heat_norm), 0.0001);
    nodes.forEach((n) => {
      n.size = n.topic ? 13 + 13 * Math.sqrt(n.heat_norm / topicMax) : 2.6 + 3.2 * Math.sqrt(n.heat_norm);
      const t = Math.min(1, 0.15 + 1.6 * Math.sqrt(n.heat_norm)) + (n.type === "Company" ? 0.2 : 0);
      n.tone = d3.interpolateRgb("#a3a29e", "#1d1d1d")(Math.min(1, t));
      // collision radius covers the label width, so names don't sit on top of each other
      n.labelR = Math.max(n.topic ? n.size + 26 : n.size + 16, (n.label.length + 2) * (n.topic ? 4.6 : 3.6));
    });
    // The "Trending repos" hub is a navigational cluster, not a news topic -- exclude it from the
    // "hottest topic" accent (solid-orange fill) so that styling marks the genuinely-hottest news
    // topic. Its size still scales with heat (now decayed, see first-seen re-dating in build.py).
    hottestTopic = nodes.filter((n) => n.topic && n.id !== "trending-repos").sort((a, b) => b.heat - a.heat)[0];
    lag = data.trend_lag_hours || 24;

    // ---- header / legend / source list / search autocomplete
    document.querySelector(".tag").textContent = view === "today" ? "what's hot in AI right now · last 24 hours" : "what's heating up in AI · last 7 days";
    $("#legTrend").textContent = `↑ rising · ↓ cooling (vs ${lag} h ago)`;
    const nBreaking = nodes.filter((n) => n.breaking || n.surging).length;
    $("#updated").innerHTML = `updated ${esc(ago(data.generated_at))} · ${data.story_count} stories · refreshes hourly`
      + (nBreaking ? ` · <button id="breakingBtn" class="linklike" title="Show the breaking entities">${nBreaking} breaking</button>` : "");
    if (nBreaking) $("#breakingBtn").addEventListener("click", renderBreakingPanel);
    $("#hl").textContent = data.half_life_hours;
    $("#srcList").innerHTML = Object.entries(data.sources).map(([s, n]) => `<li>${esc(s)}: ${n}</li>`).join("");
    $("#labels").innerHTML = nodes.map((n) => `<option value="${esc(n.label)}">`).join("");

    // ---- type-filter chips (rebuilt for the types present, honouring the persisted activeTypes)
    const present = TYPES.filter((t) => nodes.some((n) => n.type === t));
    $("#types").innerHTML = present.map((t) =>
      `<span class="chip${activeTypes.has(t) ? "" : " off"}" data-type="${t}" role="button" tabindex="0" aria-pressed="${activeTypes.has(t)}">${TYPE_LABEL[t] || t} <span class="meta">${nodes.filter((n) => n.type === t).length}</span></span>`).join("");

    // ---- SVG scaffold
    svg = d3.select(stage).append("svg");
    root = svg.append("g");
    hullLayer = root.append("g");
    linkSel = root.append("g").selectAll("line").data(links).join("line")
      .attr("class", (d) => "link" + (d.weight < 0.35 ? " weak" : ""));
    nodeSel = root.append("g").selectAll("g").data(nodes, (d) => d.id).join("g").attr("class", "node");
    nodeSel.filter((d) => d.topic).append("rect").attr("class", (d) => "diamond" + (d === hottestTopic ? " hot" : ""))
      .attr("x", (d) => -d.size / 2).attr("y", (d) => -d.size / 2).attr("width", (d) => d.size).attr("height", (d) => d.size)
      .attr("transform", "rotate(45)");
    nodeSel.filter((d) => !d.topic).append("circle").attr("class", "dot").attr("r", (d) => d.size).attr("fill", (d) => d.tone);
    nodeSel.filter((d) => d.status === "new").append("circle").attr("class", "newring")
      .attr("r", (d) => (d.topic ? d.size * 0.95 : d.size + 5));
    nodeSel.filter((d) => d.breaking || d.surging).append("circle").attr("class", "breakring").attr("r", (d) => (d.topic ? d.size : d.size + 7));
    // invisible hit area so small dots are easy to click
    nodeSel.append("circle").attr("r", (d) => Math.max(10, d.size)).attr("fill", "transparent");

    labelSel = root.append("g").selectAll("text").data(nodes, (d) => d.id).join("text")
      .attr("class", (d) => "label" + (d.topic ? " topic" : "") + (d.breaking || d.surging ? " breaking" : ""))
      .attr("text-anchor", "middle")
      .attr("dy", (d) => (d.topic ? d.size * 0.72 + 17 : d.size + 13))
      .attr("font-size", (d) => (d.topic ? null : 10.5 + 2.5 * Math.sqrt(d.heat_norm) + "px"))
      .attr("fill", (d) => (d.topic ? null : d.tone))
      .attr("font-weight", (d) => (!d.topic && d.heat_norm > 0.3 ? 600 : null))
      .text((d) => d.label + arrow(d));

    // ---- tooltip + interaction
    tip = d3.select("body").append("div").attr("id", "tooltip").style("display", "none");
    nodeSel.on("mouseenter", (ev, d) => {
      markActivity();
      highlight(d);
      tip.style("display", "block").html(`${esc(d.label)} · ${esc(d.topic ? "topic" : d.type.toLowerCase())}<br><span class="meta">heat ${d.heat.toFixed(1)} · ${d.stories} stories</span>`);
    }).on("mousemove", (ev) => tip.style("left", ev.clientX + 14 + "px").style("top", ev.clientY + 12 + "px"))
      .on("mouseleave", () => { tip.style("display", "none"); highlight(selected); })
      .on("click", (ev, d) => { ev.stopPropagation(); markActivity(); select(d); });

    // ---- physics: topics anchor the layout, entities orbit what they're linked to
    sim = d3.forceSimulation(nodes)
      .force("link", d3.forceLink(links).id((d) => d.id).distance((l) => 80 + 110 * (1 - l.weight)).strength((l) => 0.05 + 0.35 * l.weight))
      .force("charge", d3.forceManyBody().strength((d) => (d.topic ? -420 : -110 - 260 * d.heat_norm)))
      .force("collide", d3.forceCollide((d) => d.labelR).strength(0.9).iterations(2))
      .force("x", d3.forceX(0).strength((d) => 0.025 + 0.08 * d.heat_norm))
      .force("y", d3.forceY(0).strength((d) => 0.025 + 0.08 * d.heat_norm))
      .on("tick", () => {
        linkSel.attr("x1", (d) => d.source.x).attr("y1", (d) => d.source.y).attr("x2", (d) => d.target.x).attr("y2", (d) => d.target.y);
        nodeSel.attr("transform", (d) => `translate(${d.x},${d.y})`);
        labelSel.attr("transform", (d) => `translate(${d.x},${d.y})`);
        drawHull();
      });

    nodeSel.call(d3.drag()
      .on("start", (ev, d) => { markActivity(); if (!ev.active) sim.alphaTarget(0.25).restart(); d.fx = d.x; d.fy = d.y; })
      .on("drag", (ev, d) => { d.fx = ev.x; d.fy = ev.y; })
      .on("end", (ev, d) => { if (!ev.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }));

    // ---- zoom (a fresh behaviour per build, bound to the new svg)
    zoomK = opts.transform ? opts.transform.k : 1;
    zoom = d3.zoom().scaleExtent([0.25, 5]).on("zoom", (ev) => { root.attr("transform", ev.transform); zoomK = ev.transform.k; });
    svg.call(zoom).on("dblclick.zoom", null);
    svg.on("wheel.track mousedown.track touchstart.track", markActivity);
    svg.on("click", () => select(null));

    // settle the layout before showing it: deterministic, no wobble, and the fit sees final positions
    sim.stop();
    sim.tick(320);
    sim.on("tick").call(sim);

    applyFilters(); // honour any persisted type/status filter before we frame the view
    if (opts.transform) svg.call(zoom.transform, opts.transform); // live update: keep the viewer's view
    else initialView();

    // restore the selected entity (by id) across a rebuild, if it still exists
    if (selectedId && byId.has(selectedId)) select(byId.get(selectedId));
    else if (selectedId) select(null);

    if (firstBuild && !MOBILE) showNavHint();
    firstBuild = false;
  }

  // First-visit navigation hint (desktop only): fades in, auto-dismisses after 15 s, and also
  // clears on the first pan/zoom or on close. Never blocks map interaction (pointer-events via CSS).
  function showNavHint() {
    const help = $("#mapHelp");
    if (!help) return;
    help.hidden = false;
    requestAnimationFrame(() => help.classList.add("show"));
    let gone = false;
    const dismiss = () => {
      if (gone) return; gone = true;
      help.classList.remove("show");
      setTimeout(() => { help.hidden = true; }, 450);
    };
    const timer = setTimeout(dismiss, 15000);
    $("#mapHelpClose").addEventListener("click", () => { clearTimeout(timer); dismiss(); });
    svg.on("wheel.hint mousedown.hint touchstart.hint", () => { clearTimeout(timer); dismiss(); });
  }

  // ============================================================================================
  //  One-time wiring to the static DOM (these elements are never rebuilt, so bind them just once)
  // ============================================================================================
  function bindStaticOnce() {
    $("#types").addEventListener("click", (e) => {
      const c = e.target.closest(".chip"); if (!c) return;
      const t = c.dataset.type;
      activeTypes.has(t) ? activeTypes.delete(t) : activeTypes.add(t);
      c.classList.toggle("off", !activeTypes.has(t)); c.setAttribute("aria-pressed", activeTypes.has(t));
      applyFilters();
    });
    $("#status").addEventListener("change", applyFilters);
    $("#closePanel").addEventListener("click", () => select(null));
    $("#aboutBtn").addEventListener("click", () => $("#about").showModal());
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") select(null); });
    window.addEventListener("resize", () => fit(false));

    $("#search").addEventListener("change", async (e) => {
      const raw = e.target.value.trim();
      const q = raw.toLowerCase();
      if (!q) return;
      // 1) an entity that's on the map -> focus its node (reveal it if a filter is hiding it)
      const d = nodes.find((n) => n.label.toLowerCase() === q) || nodes.find((n) => n.label.toLowerCase().includes(q));
      if (d) { if (!visible(d)) { activeTypes.add(d.type); $("#status").value = "all"; applyFilters(); } focusNode(d); return; }
      // 2) not on the map -> surface matching news from the 30-day archive
      const hits = (await getArchive()).filter((a) => a.title.toLowerCase().includes(q));
      renderSearchPanel(raw, hits);
    });

    // activity tracking (for idle-aware live updates) + the "fresh data" pill
    ["pointerdown", "wheel", "keydown", "touchstart", "mousemove"].forEach((ev) =>
      document.addEventListener(ev, markActivity, { passive: true }));
    const pill = $("#freshPill");
    if (pill) pill.addEventListener("click", () => { if (!applying) applyUpdate(); });
  }

  // ============================================================================================
  //  Live updates: poll a tiny freshness token; apply silently when idle, offer a pill otherwise
  // ============================================================================================
  const POLL_MS = 90000;   // how often to check for a new backend build
  const IDLE_MS = 15000;   // "idle" = no interaction for this long (and nothing selected/open)
  let lastActivity = Date.now();
  let lastToken = null;        // new_counts.generated_at of the build we're currently showing
  let latestSeenToken = null;  // the most recent token the poll has observed (what an apply adopts)
  let applying = false;        // a crossfade/rebuild is in flight
  let pending = false;         // a newer build is available but the viewer is busy (pill shown)

  function markActivity() { lastActivity = Date.now(); }
  function isIdle() {
    return (Date.now() - lastActivity > IDLE_MS)
      && !selected
      && $("#panel").hidden
      && (!tip || tip.style("display") === "none");
  }

  function setNewCount(nc) {
    const el = $("#newcount");
    if (!el) return;
    if (nc && nc.new_last_hour > 0) {
      el.textContent = `+${nc.new_last_hour} new`;
      el.title = `${nc.new_last_hour} new articles in the last hour`;
      el.hidden = false;
    } else {
      el.hidden = true;
    }
  }

  // ---- Incoming-feed HUD (lower-left): the newest items, streamed from new_counts.latest (which
  //      the page already polls every 90s). New items appear at the top and push the rest down;
  //      the list holds at most FEED_MAX. Clicking an item opens the article in a new tab.
  const FEED_MAX = 10;
  let feedUrls = [];        // urls currently shown, top -> bottom
  let firstFeed = true;     // first paint shouldn't flash every item as "new"
  function renderFeed(latest) {
    const list = $("#feedList"), wrap = $("#feed");
    if (!list || !wrap) return;
    if (!Array.isArray(latest) || !latest.length) return;   // older build without `latest`: leave as-is
    const items = latest.filter((it) => it && it.url && it.title).slice(0, FEED_MAX);
    if (!items.length) return;
    const order = items.map((it) => it.url);
    wrap.hidden = false;
    if (order.length === feedUrls.length && order.every((u, i) => u === feedUrls[i])) return; // unchanged
    const prev = new Set(feedUrls);  // the build we were showing; after the first paint, anything not in it is new
    list.innerHTML = items.map((it) => {
      const fresh = !firstFeed && !prev.has(it.url);  // first paint: show the list without flashing every row
      return `<li><a class="feed-item${fresh ? " is-new" : ""}" href="${esc(it.url)}" target="_blank" rel="noopener">`
        + `<span class="ft">${esc(it.title)}</span><span class="fs">${esc(it.source || "")}</span></a></li>`;
    }).join("");
    feedUrls = order;
    firstFeed = false;
  }
  async function fetchCounts() {
    try { return await (await fetch("data/new_counts.json", { cache: "no-store" })).json(); }
    catch (e) { return null; }
  }
  async function fetchData() {
    return (await fetch(view === "today" ? "data/today.json" : "data/data.json", { cache: "no-store" })).json();
  }

  function showPill() { const p = $("#freshPill"); if (!p) return; p.hidden = false; requestAnimationFrame(() => p.classList.add("show")); }
  function hidePill() { const p = $("#freshPill"); if (!p) return; p.classList.remove("show"); setTimeout(() => { p.hidden = true; }, 400); }

  // Crossfade the current graph out, swap in the freshly fetched data, and fade back in — keeping
  // the viewer's zoom, filters and selected entity. No full-page reload.
  async function applyUpdate() {
    if (applying) return;
    applying = true;
    let nd;
    try { nd = await fetchData(); }
    catch (e) { applying = false; return; } // transient fetch error: leave pill/pending as-is
    lastToken = latestSeenToken || nd.generated_at; // adopt the build we're now showing

    const savedT = svg ? d3.zoomTransform(svg.node()) : null;
    const oldSvg = svg, oldTip = tip;
    if (oldSvg) oldSvg.transition().duration(220).style("opacity", 0);
    setTimeout(() => {
      if (oldSvg) oldSvg.remove();
      if (oldTip) oldTip.remove();
      data = nd;
      archive = null;                    // the 30-day archive may have moved on too
      buildGraph({ transform: savedT });
      svg.style("opacity", 0).transition().duration(300).style("opacity", 1);
      applying = false; pending = false;
      hidePill();
      fetchCounts().then((nc) => { setNewCount(nc); renderFeed(nc && nc.latest); }); // refresh tally + feed
    }, 240);
  }

  async function poll() {
    if (document.hidden || applying) return;
    const nc = await fetchCounts();
    if (!nc) return;
    setNewCount(nc);                     // keep the tally live regardless
    renderFeed(nc.latest);               // stream any new items into the lower-left HUD
    const token = nc.generated_at;
    if (!token) return;
    latestSeenToken = token;
    if (token !== lastToken) {
      if (isIdle()) applyUpdate();       // silent apply (applyUpdate adopts latestSeenToken)
      else { pending = true; showPill(); } // busy: offer the pill
    }
  }

  // ============================================================================================
  //  Boot
  // ============================================================================================
  try {
    data = await fetchData();
  } catch (e) {
    stage.innerHTML = '<p style="padding:120px 24px;color:#6f6f6f">No data yet. Run ./run.sh to build the map.</p>';
    return;
  }
  bindStaticOnce();
  buildGraph();
  lastToken = data.generated_at;
  // initial "+N new" tally, and adopt the live freshness token so the first real build is detected
  fetchCounts().then((nc) => { setNewCount(nc); renderFeed(nc && nc.latest); if (nc && nc.generated_at) lastToken = latestSeenToken = nc.generated_at; });

  setInterval(poll, POLL_MS);
  // check more often whether a pending update can now be applied silently (viewer went idle)
  setInterval(() => { if (pending && !applying && isIdle()) applyUpdate(); }, 3000);
  // re-check promptly when the tab regains focus
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
})();
