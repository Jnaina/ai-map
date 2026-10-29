// Topics page: read web/data/topics.json (last 90 days, grouped by topic/entity), show topics as
// tabs on the left and all of a topic's articles on the right — every article it appears in, with
// NO heat ranking or cap (unlike the map's node panel). The JSON is normalised: a flat articles[]
// array plus byTopic{ entityId: [indices] }, so an article under several topics is stored once.
"use strict";

const state = { data: null, topic: null, q: "" };

const el = {
  nav: document.getElementById("srcNav"),
  list: document.getElementById("articles"),
  head: document.getElementById("listHead"),
  empty: document.getElementById("empty"),
  search: document.getElementById("search"),
};

const KIND_ENG = { discussion: "pts", paper: "▲", model: "▲", repo: "★" };
const KIND_LABEL = { news: "news", discussion: "discussion", paper: "paper", model: "model", repo: "repo" };

// Order the type groups appear in the sidebar. Any type not listed falls to the end, alphabetically.
const TYPE_ORDER = ["Company", "Concept", "Model", "Product", "Open source", "Hardware", "Person"];

function esc(s) {
  return (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function fmtDate(sec) {
  if (!sec) return "";
  const d = new Date(sec * 1000);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const date = `${months[d.getMonth()]} ${d.getDate()}`;
  const age = (Date.now() / 1000 - sec);
  let rel;
  if (age < 3600) rel = `${Math.max(1, Math.round(age / 60))}m ago`;
  else if (age < 86400) rel = `${Math.round(age / 3600)}h ago`;
  else rel = `${Math.round(age / 86400)}d ago`;
  return `${date} · ${rel}`;
}

// Sort key: when we discovered the link (first_seen), newest first — matches the on-map incoming
// feed and the by-source Articles page. Fall back to published for any older row.
const byDiscovery = (x, y) => (y.first_seen || y.published || 0) - (x.first_seen || x.published || 0);

function topicById(id) {
  return state.data.topics.find(t => t.id === id) || null;
}

// Sidebar icon for a topic: a curated company favicon when present (lazy-loaded, falling back to
// the letter tile if the image is missing or fails), otherwise the letter tile alone.
function tileHtml(t) {
  const letter = esc((t.label || "?").trim().charAt(0).toUpperCase());
  if (t.favicon) {
    return `<span class="favwrap"><img class="fav" src="${esc(t.favicon)}" alt="" width="18" height="18" loading="lazy" decoding="async" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><span class="favtile topicon" style="display:none">${letter}</span></span>`;
  }
  return `<span class="favwrap"><span class="favtile topicon">${letter}</span></span>`;
}

// The articles for the current selection, newest-discovered first. "All topics" (topic === null)
// is the whole flat array (already the deduped union across every topic).
function currentArticles() {
  if (state.topic === null) return state.data.articles.slice().sort(byDiscovery);
  const idxs = state.data.byTopic[state.topic] || [];
  return idxs.map(i => state.data.articles[i]).sort(byDiscovery);
}

function renderNav() {
  const d = state.data;
  const q = state.q.trim().toLowerCase();

  // Group the (optionally search-filtered) topics by type. The selected topic always stays visible.
  const groups = new Map();
  for (const t of d.topics) {
    if (q && !t.label.toLowerCase().includes(q) && t.id !== state.topic) continue;
    const ty = t.type || "Other";
    (groups.get(ty) || groups.set(ty, []).get(ty)).push(t);
  }
  // Order groups by TYPE_ORDER, then any unlisted type alphabetically at the end.
  const types = [...groups.keys()].sort((a, b) => {
    const ia = TYPE_ORDER.indexOf(a), ib = TYPE_ORDER.indexOf(b);
    if (ia !== -1 && ib !== -1) return ia - ib;
    if (ia !== -1) return -1;
    if (ib !== -1) return 1;
    return a.localeCompare(b);
  });

  const items = [`<button class="src all${state.topic === null ? " on" : ""}" data-topic="">
      <span class="favwrap"><span class="favtile allicon">★</span></span>
      <span class="name">All topics</span><span class="n">${d.total}</span></button>`];
  for (const ty of types) {
    const list = groups.get(ty).sort((a, b) => a.label.toLowerCase().localeCompare(b.label.toLowerCase()));
    items.push(`<div class="grouphead">${esc(ty)}</div>`);
    for (const t of list) {
      items.push(`<button class="src${state.topic === t.id ? " on" : ""}" data-topic="${esc(t.id)}">
        ${tileHtml(t)}<span class="name" title="${esc(t.label)}">${esc(t.label)}</span><span class="n">${t.count}</span></button>`);
    }
  }
  el.nav.innerHTML = items.join("");
  el.nav.querySelectorAll(".src").forEach(b => b.addEventListener("click", () => {
    selectTopic(b.dataset.topic || null);
  }));
}

function renderList(keepScroll) {
  const scroller = el.list.parentElement;
  const prevTop = scroller.scrollTop;
  let items = currentArticles();
  const q = state.q.trim().toLowerCase();
  if (q) items = items.filter(a => (a.title || "").toLowerCase().includes(q) || (a.domain || "").includes(q));

  const t = state.topic === null ? null : topicById(state.topic);
  const label = t ? t.label : "All topics";
  const typeChip = t && t.type ? `<span class="ttype">${esc(t.type)}</span>` : "";
  el.head.innerHTML = `<b>${esc(label)}</b>${typeChip} — ${items.length} article${items.length === 1 ? "" : "s"}` +
    (q ? ` matching “${esc(q)}”` : "") + ` · last ${state.data.days} days`;

  el.empty.hidden = items.length > 0;
  el.list.innerHTML = items.map(a => {
    const eng = a.engagement > 0 ? `<span class="eng">${a.engagement.toLocaleString()} ${KIND_ENG[a.kind] || ""}</span>` : "";
    const disc = a.discussion ? ` <a href="${esc(a.discussion)}" target="_blank" rel="noopener">discussion ↗</a>` : "";
    return `<li>
      <a class="title" href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title) || esc(a.url)}</a>
      <div class="ameta"><span>${esc(a.source)}</span><span class="dom">${esc(a.domain)}</span><span>${fmtDate(a.published)}</span>${eng}<span class="kind">${KIND_LABEL[a.kind] || a.kind || ""}</span>${disc}</div>
    </li>`;
  }).join("");
  scroller.scrollTop = keepScroll ? prevTop : 0;
}

function selectTopic(id) {
  state.topic = id;
  const hash = id ? "#" + encodeURIComponent(id) : "";
  if (location.hash !== hash) history.replaceState(null, "", location.pathname + hash);
  renderNav();
  renderList();
}

function topicFromHash() {
  const h = decodeURIComponent(location.hash.replace(/^#/, ""));
  if (h && state.data.byTopic[h]) return h;
  return null;
}

async function main() {
  try {
    const r = await fetch("data/topics.json", { cache: "no-store" });
    state.data = await r.json();
  } catch (e) {
    el.head.innerHTML = "Could not load topics.";
    return;
  }
  state.topic = topicFromHash();
  renderNav();
  renderList();

  el.search.addEventListener("input", () => { state.q = el.search.value; renderNav(); renderList(); });
  window.addEventListener("hashchange", () => { state.topic = topicFromHash(); renderNav(); renderList(); });

  // Live refresh, same as the by-source page: poll the file the build redeploys and re-render only
  // when it changed, preserving the selected topic, the search box and the scroll position.
  async function refresh() {
    if (document.hidden) return;
    let nd;
    try { nd = await (await fetch("data/topics.json", { cache: "no-store" })).json(); }
    catch (e) { return; }
    if (!nd || nd.generated_at === state.data.generated_at) return;  // nothing new
    const keepTopic = state.topic && nd.byTopic[state.topic] ? state.topic : null;
    state.data = nd;
    state.topic = keepTopic;
    renderNav();
    renderList(true);  // keep scroll position
  }
  setInterval(refresh, 120000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
}

main();
