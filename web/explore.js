// Explore menu: open/close, keyboard, and the "new" markers (new articles from app.js's #newcount; a newsletter
// the viewer hasn't opened yet, from /weekly/latest.json + localStorage).
(function () {
  const m = document.getElementById("explore");
  if (!m) return;
  const btn = m.querySelector(".explore-btn"), list = m.querySelector(".explore-list"), dot = m.querySelector(".explore-dot");
  const items = () => Array.from(list.querySelectorAll("a"));
  function set(open) { list.hidden = !open; btn.setAttribute("aria-expanded", open ? "true" : "false"); }
  btn.addEventListener("click", (e) => { e.stopPropagation(); set(list.hidden); if (!list.hidden) items()[0].focus(); });
  document.addEventListener("click", (e) => { if (!m.contains(e.target)) set(false); });
  m.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { set(false); btn.focus(); return; }
    if (list.hidden || (e.key !== "ArrowDown" && e.key !== "ArrowUp")) return;
    e.preventDefault();
    const a = items(), i = a.indexOf(document.activeElement);
    a[(i + (e.key === "ArrowDown" ? 1 : a.length - 1)) % a.length].focus();
  });

  const nc = document.getElementById("newcount"), nl = document.getElementById("nlNew");
  function updDot() { if (dot) dot.hidden = !((nc && !nc.hidden) || (nl && !nl.hidden && nl.classList.contains("new"))); }
  if (nc) new MutationObserver(updDot).observe(nc, { attributes: true, attributeFilter: ["hidden"] });

  let seen = null;
  try { seen = localStorage.getItem("aiw_seen"); } catch (e) { /* private mode */ }
  const here = document.body.dataset.issue;            // set on a newsletter issue page
  if (here && (!seen || here > seen)) { seen = here; try { localStorage.setItem("aiw_seen", here); } catch (e) {} }
  fetch("/weekly/latest.json", { cache: "no-cache" }).then((r) => (r.ok ? r.json() : null)).then((j) => {
    if (!j || !nl) return;
    const fresh = !seen || j.label > seen;
    nl.textContent = (fresh ? "New: week " : "Week ") + j.week;
    nl.classList.toggle("new", fresh);
    nl.hidden = false;
    updDot();
  }).catch(() => {});
  updDot();
})();
