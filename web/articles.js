// Articles page: read web/data/articles.json (last 90 days, grouped by source),
// show sources as tabs on the left and their articles on the right.
"use strict";

const state = { data: null, source: null, q: "" };

const el = {
  nav: document.getElementById("srcNav"),
  list: document.getElementById("articles"),
  head: document.getElementById("listHead"),
  empty: document.getElementById("empty"),
  search: document.getElementById("search"),
};

const KIND_ENG = { discussion: "pts", paper: "▲", model: "▲", repo: "★" };
const KIND_LABEL = { news: "news", discussion: "discussion", paper: "paper", model: "model", repo: "repo" };

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

// All articles across sources, newest first (for the "All sources" tab).
function allArticles() {
  const out = [];
  for (const s of state.data.sources) {
    for (const a of state.data.articles[s.source]) out.push({ ...a, source: s.source });
  }
  out.sort((x, y) => y.published - x.published);
  return out;
}

function currentArticles() {
  if (state.source === null) return allArticles();
  return (state.data.articles[state.source] || []).map(a => ({ ...a, source: state.source }));
}

function favHtml(s) {
  // A site icon, with a letter-tile fallback shown if the image is missing/fails.
  const letter = esc((s.source || "?").trim().charAt(0).toUpperCase());
  const img = s.favicon
    ? `<img class="fav" src="${esc(s.favicon)}" alt="" width="18" height="18" decoding="async" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">`
    : "";
  const tile = `<span class="favtile" style="${s.favicon ? "display:none" : ""}">${letter}</span>`;
  return `<span class="favwrap">${img}${tile}</span>`;
}

function renderNav() {
  const d = state.data;
  const items = [`<button class="src all${state.source === null ? " on" : ""}" data-src="">
      <span class="favwrap"><span class="favtile allicon">★</span></span>
      <span class="name">All sources</span><span class="n">${d.total}</span></button>`];
  for (const s of d.sources) {
    items.push(`<button class="src${state.source === s.source ? " on" : ""}" data-src="${esc(s.source)}">
      ${favHtml(s)}<span class="name" title="${esc(s.source)}">${esc(s.source)}</span><span class="n">${s.count}</span></button>`);
  }
  el.nav.innerHTML = items.join("");
  el.nav.querySelectorAll(".src").forEach(b => b.addEventListener("click", () => {
    selectSource(b.dataset.src || null);
  }));
}

function renderList() {
  let items = currentArticles();
  const q = state.q.trim().toLowerCase();
  if (q) items = items.filter(a => (a.title || "").toLowerCase().includes(q) || (a.domain || "").includes(q));

  const label = state.source === null ? "All sources" : state.source;
  el.head.innerHTML = `<b>${esc(label)}</b> — ${items.length} article${items.length === 1 ? "" : "s"}` +
    (q ? ` matching “${esc(q)}”` : "") + ` · last ${state.data.days} days`;

  el.empty.hidden = items.length > 0;
  el.list.innerHTML = items.map(a => {
    const eng = a.engagement > 0 ? `<span class="eng">${a.engagement.toLocaleString()} ${KIND_ENG[a.kind] || ""}</span>` : "";
    const disc = a.discussion ? ` <a href="${esc(a.discussion)}" target="_blank" rel="noopener">discussion ↗</a>` : "";
    const src = state.source === null ? `<span>${esc(a.source)}</span>` : "";
    return `<li>
      <a class="title" href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title) || esc(a.url)}</a>
      <div class="ameta">${src}<span class="dom">${esc(a.domain)}</span><span>${fmtDate(a.published)}</span>${eng}<span class="kind">${KIND_LABEL[a.kind] || a.kind || ""}</span>${disc}</div>
    </li>`;
  }).join("");
  el.list.parentElement.scrollTop = 0;
}

function selectSource(source) {
  state.source = source;
  const hash = source ? "#" + encodeURIComponent(source) : "";
  if (location.hash !== hash) history.replaceState(null, "", location.pathname + hash);
  renderNav();
  renderList();
}

function sourceFromHash() {
  const h = decodeURIComponent(location.hash.replace(/^#/, ""));
  if (h && state.data.articles[h]) return h;
  return null;
}

async function main() {
  try {
    const r = await fetch("data/articles.json", { cache: "no-store" });
    state.data = await r.json();
  } catch (e) {
    el.head.innerHTML = "Could not load articles.";
    return;
  }
  state.source = sourceFromHash();
  renderNav();
  renderList();

  el.search.addEventListener("input", () => { state.q = el.search.value; renderList(); });
  window.addEventListener("hashchange", () => { state.source = sourceFromHash(); renderNav(); renderList(); });
}

main();
