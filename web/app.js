/* AI Map front end: a minimal, monochrome force-directed graph of AI entities and their "heat".
   Topics (concepts) are diamonds, everything else is a small dot; heat is shown by darkness.
   One accent colour marks the hottest topic, its cluster, and entities that are new this week. */
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

  let selected = null; // declared first: zoom and filters read it
  const view = new URLSearchParams(location.search).get("view") === "today" ? "today" : "week";
  document.querySelectorAll(".views a").forEach((a) => a.classList.toggle("on", a.dataset.view === view));
  let data;
  try {
    data = await (await fetch(view === "today" ? "data/today.json" : "data/data.json", { cache: "no-store" })).json();
  } catch (e) {
    $("#stage").innerHTML = '<p style="padding:120px 24px;color:#6f6f6f">No data yet. Run ./run.sh to build the map.</p>';
    return;
  }

  const nodes = data.nodes.map((d, i) => ({ ...d, rank: i + 1, topic: d.type === "Concept" }));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const links = data.links.filter((l) => byId.has(l.source) && byId.has(l.target)).map((l) => ({ ...l }));
  const neigh = new Map(nodes.map((n) => [n.id, new Map()]));
  links.forEach((l) => { neigh.get(l.source).set(l.target, l.weight); neigh.get(l.target).set(l.source, l.weight); });

  // sizes and tones: hotter = bigger and darker
  const topicMax = Math.max(...nodes.filter((n) => n.topic).map((n) => n.heat_norm), 0.0001);
  nodes.forEach((n) => {
    n.size = n.topic ? 13 + 13 * Math.sqrt(n.heat_norm / topicMax) : 2.6 + 3.2 * Math.sqrt(n.heat_norm);
    const t = Math.min(1, 0.15 + 1.6 * Math.sqrt(n.heat_norm)) + (n.type === "Company" ? 0.2 : 0);
    n.tone = d3.interpolateRgb("#a3a29e", "#1d1d1d")(Math.min(1, t));
    // collision radius covers the label width, so names don't sit on top of each other
    n.labelR = Math.max(n.topic ? n.size + 26 : n.size + 16, (n.label.length + 2) * (n.topic ? 4.6 : 3.6));
  });
  const hottestTopic = nodes.filter((n) => n.topic).sort((a, b) => b.heat - a.heat)[0];
  const arrow = (d) => (d.trend >= 1.5 ? " ↑" : d.trend <= 0.75 ? " ↓" : "");

  const lag = data.trend_lag_hours || 24;
  document.querySelector(".tag").textContent = view === "today" ? "what's hot in AI right now · last 24 hours" : "what's heating up in AI · last 7 days";
  $("#legTrend").textContent = `↑ rising · ↓ cooling (vs ${lag} h ago)`;
  const nBreaking = nodes.filter((n) => n.breaking).length;
  $("#updated").textContent = `updated ${ago(data.generated_at)} · ${data.story_count} stories · refreshes hourly${nBreaking ? ` · ${nBreaking} breaking` : ""}`;
  $("#hl").textContent = data.half_life_hours;
  $("#srcList").innerHTML = Object.entries(data.sources).map(([s, n]) => `<li>${esc(s)}: ${n}</li>`).join("");
  $("#labels").innerHTML = nodes.map((n) => `<option value="${esc(n.label)}">`).join("");

  // ---- type filter (text toggles)
  const activeTypes = new Set(TYPES);
  const present = TYPES.filter((t) => nodes.some((n) => n.type === t));
  $("#types").innerHTML = present.map((t) =>
    `<span class="chip" data-type="${t}" role="button" tabindex="0" aria-pressed="true">${TYPE_LABEL[t] || t} <span class="meta">${nodes.filter((n) => n.type === t).length}</span></span>`).join("");
  $("#types").addEventListener("click", (e) => {
    const c = e.target.closest(".chip"); if (!c) return;
    const t = c.dataset.type;
    activeTypes.has(t) ? activeTypes.delete(t) : activeTypes.add(t);
    c.classList.toggle("off", !activeTypes.has(t)); c.setAttribute("aria-pressed", activeTypes.has(t));
    applyFilters();
  });
  $("#status").addEventListener("change", applyFilters);

  // ---- SVG
  const stage = $("#stage");
  const W = () => stage.clientWidth, H = () => stage.clientHeight;
  const svg = d3.select(stage).append("svg");
  const root = svg.append("g");
  const hullLayer = root.append("g");
  const linkSel = root.append("g").selectAll("line").data(links).join("line")
    .attr("class", (d) => "link" + (d.weight < 0.35 ? " weak" : ""));
  const nodeSel = root.append("g").selectAll("g").data(nodes, (d) => d.id).join("g").attr("class", "node");
  nodeSel.filter((d) => d.topic).append("rect").attr("class", (d) => "diamond" + (d === hottestTopic ? " hot" : ""))
    .attr("x", (d) => -d.size / 2).attr("y", (d) => -d.size / 2).attr("width", (d) => d.size).attr("height", (d) => d.size)
    .attr("transform", "rotate(45)");
  nodeSel.filter((d) => !d.topic).append("circle").attr("class", "dot").attr("r", (d) => d.size).attr("fill", (d) => d.tone);
  nodeSel.filter((d) => d.status === "new").append("circle").attr("class", "newring")
    .attr("r", (d) => (d.topic ? d.size * 0.95 : d.size + 5));
  nodeSel.filter((d) => d.breaking).append("circle").attr("class", "breakring").attr("r", (d) => (d.topic ? d.size : d.size + 7));
  // invisible hit area so small dots are easy to click
  nodeSel.append("circle").attr("r", (d) => Math.max(10, d.size)).attr("fill", "transparent");

  const labelSel = root.append("g").selectAll("text").data(nodes, (d) => d.id).join("text")
    .attr("class", (d) => "label" + (d.topic ? " topic" : "") + (d.breaking ? " breaking" : ""))
    .attr("text-anchor", "middle")
    .attr("dy", (d) => (d.topic ? d.size * 0.72 + 17 : d.size + 13))
    .attr("font-size", (d) => (d.topic ? null : 10.5 + 2.5 * Math.sqrt(d.heat_norm) + "px"))
    .attr("fill", (d) => (d.topic ? null : d.tone))
    .attr("font-weight", (d) => (!d.topic && d.heat_norm > 0.3 ? 600 : null))
    .text((d) => d.label + arrow(d));

  // ---- tooltip + interaction
  const tip = d3.select("body").append("div").attr("id", "tooltip").style("display", "none");
  nodeSel.on("mouseenter", (ev, d) => {
    highlight(d);
    tip.style("display", "block").html(`${esc(d.label)} · ${esc(d.topic ? "topic" : d.type.toLowerCase())}<br><span class="meta">heat ${d.heat.toFixed(1)} · ${d.stories} stories</span>`);
  }).on("mousemove", (ev) => tip.style("left", ev.clientX + 14 + "px").style("top", ev.clientY + 12 + "px"))
    .on("mouseleave", () => { tip.style("display", "none"); highlight(selected); })
    .on("click", (ev, d) => { ev.stopPropagation(); select(d); });

  // ---- physics: topics anchor the layout, entities orbit what they're linked to
  const sim = d3.forceSimulation(nodes)
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
    .on("start", (ev, d) => { if (!ev.active) sim.alphaTarget(0.25).restart(); d.fx = d.x; d.fy = d.y; })
    .on("drag", (ev, d) => { d.fx = ev.x; d.fy = ev.y; })
    .on("end", (ev, d) => { if (!ev.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }));

  // ---- cluster hull: the selected node (or, by default, the hottest topic) and its closest neighbours
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

  // ---- zoom
  let zoomK = 1;
  const zoom = d3.zoom().scaleExtent([0.25, 5]).on("zoom", (ev) => { root.attr("transform", ev.transform); zoomK = ev.transform.k; });
  svg.call(zoom).on("dblclick.zoom", null);
  let userMoved = false;
  svg.on("wheel.track mousedown.track touchstart.track", () => { userMoved = true; });
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
  // settle the layout before showing it: deterministic, no wobble, and the fit sees final positions
  sim.stop();
  sim.tick(320);
  sim.on("tick").call(sim);
  fit(false);
  window.addEventListener("resize", () => fit(false));
  svg.on("click", () => select(null));

  // ---- filtering
  function visible(d) {
    const st = $("#status").value;
    return activeTypes.has(d.type) && (st === "all" || (st === "breaking" ? !!d.breaking : d.status === st));
  }
  function applyFilters() {
    nodeSel.style("display", (d) => (visible(d) ? null : "none"));
    labelSel.style("display", (d) => (visible(d) ? null : "none"));
    linkSel.style("display", (l) => (visible(l.source) && visible(l.target) ? null : "none"));
    drawHull();
  }

  // ---- selection + highlight
  function highlight(d) {
    if (!d) { nodeSel.classed("faded", false); labelSel.classed("faded", false); linkSel.classed("faded", false); return; }
    const nb = neigh.get(d.id);
    nodeSel.classed("faded", (n) => n !== d && !nb.has(n.id));
    labelSel.classed("faded", (n) => n !== d && !nb.has(n.id));
    linkSel.classed("faded", (l) => l.source !== d && l.target !== d);
  }
  function select(d) {
    selected = d;
    nodeSel.classed("sel", (n) => n === d);
    highlight(d); drawHull();
    if (!d) { $("#panel").hidden = true; return; }
    renderPanel(d);
  }
  $("#closePanel").addEventListener("click", () => select(null));

  function spark(series) {
    if (!series || series.length < 2) return '<p class="meta">trend line appears after a few refreshes</p>';
    const v = series.map((s) => s[1]), max = Math.max(...v), min = Math.min(...v);
    const pts = v.map((y, i) => `${(i / (v.length - 1)) * 300},${36 - ((y - min) / (max - min || 1)) * 32}`).join(" ");
    return `<svg class="spark" viewBox="0 0 300 40" preserveAspectRatio="none" aria-label="Heat over recent refreshes"><polyline points="${pts}" fill="none" stroke="#1d1d1d" stroke-width="1.5"/></svg>`;
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
        ${d.status === "new" ? '<span class="badge new">new this week</span>' : ""}
        ${d.status === "rising" ? '<span class="badge rising">rising</span>' : ""}
        ${parent ? `<span>part of ${esc(parent.label)}</span>` : ""}
      </div>
      <div class="stats">
        <div class="stat"><b>#${d.rank}</b><span>of ${nodes.length} by heat</span></div>
        <div class="stat"><b>${d.heat.toFixed(1)}</b><span>${esc(trendTxt)}</span></div>
        <div class="stat"><b>${d.stories}</b><span>stories ${view === "today" ? "today" : "this week"}</span></div>
      </div>
      ${spark(d.series)}
      <p class="meta">first spotted ${new Date(d.first_seen).toLocaleDateString(undefined, { day: "numeric", month: "short" })} · ${Object.entries(d.sources).map(([s, n]) => `${esc(s)} ${n}`).join(" · ")}</p>
      <h3>Top stories</h3>
      <ul class="stories">${d.top_stories.map((s) => `
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

  $("#search").addEventListener("change", (e) => {
    const q = e.target.value.trim().toLowerCase();
    const d = nodes.find((n) => n.label.toLowerCase() === q) || nodes.find((n) => n.label.toLowerCase().includes(q));
    if (d) { if (!visible(d)) { activeTypes.add(d.type); $("#status").value = "all"; applyFilters(); } focusNode(d); }
  });

  $("#aboutBtn").addEventListener("click", () => $("#about").showModal());
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") select(null); });
})();
