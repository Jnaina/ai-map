#!/usr/bin/env python3
"""Dictionary curator — a local web app for Jag's AI Map.

Shows every entity in the dictionary (SEED + the human overlay), grouped by type, and the entities the
LLM is recommending (from emerging_entities). The curator can add new entities, attach an alias to an
existing node, remove entities that are no longer relevant, and dismiss recommendations. Nothing here
edits source: all changes are written to data/curation.json, which entities.py merges on top of SEED
at the next pipeline run (every ~2 min). Standard library only.

Run:  python3 curator/app.py [--port 8096] [--host 0.0.0.0]
"""
import argparse
import json
import os
import sys
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))
import entities            # noqa: E402
import emerging_entities   # noqa: E402

CUR = entities.CURATION_PATH


# ---- curation.json overlay (atomic read/write) ------------------------------------------------
def load_cur():
    try:
        c = json.load(open(CUR))
    except Exception:
        c = {}
    c.setdefault("added", [])
    c.setdefault("removed", [])
    c.setdefault("aliases", {})
    c.setdefault("dismissed", [])
    return c


def save_cur(c):
    os.makedirs(os.path.dirname(CUR), exist_ok=True)
    tmp = CUR + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, indent=2)
    os.replace(tmp, CUR)   # atomic: the pipeline never reads a half-written file


def dictionary(cur):
    """Every entity: SEED base + overlay additions, each annotated with removed/extra-alias state."""
    removed = set(cur["removed"])
    out = []
    for label, typ, aliases, parent in entities.SEED:
        eid = entities.slug(label)
        out.append(dict(id=eid, label=label, type=typ,
                        parent=entities.slug(parent) if parent else None,
                        aliases=list(aliases), source="seed", removed=eid in removed,
                        extra_aliases=cur["aliases"].get(eid, [])))
    for e in cur["added"]:
        label = (e.get("label") or "").strip()
        if not label:
            continue
        eid = entities.slug(label)
        out.append(dict(id=eid, label=label, type=e.get("type", "Product"),
                        parent=entities.slug(e["parent"]) if e.get("parent") else None,
                        aliases=e.get("aliases") or [label], source="added", removed=eid in removed,
                        extra_aliases=cur["aliases"].get(eid, [])))
    return out


def state(days, min_stories):
    cur = load_cur()
    d = dictionary(cur)
    rows, curated_hit = emerging_entities.compute(days, min_stories)
    by_type = Counter(x["type"] for x in d if not x["removed"])
    return dict(
        dictionary=d, recommendations=rows,
        types=entities.TYPES,
        stats=dict(active=sum(1 for x in d if not x["removed"]),
                   by_type=dict(by_type), added=len(cur["added"]),
                   removed=len(cur["removed"]), dismissed=len(cur["dismissed"]),
                   recommendations=len(rows), covered=sum(curated_hit.values())),
        days=days, min_stories=min_stories)


# ---- mutations --------------------------------------------------------------------------------
def do_add(body):
    cur = load_cur()
    label = (body.get("label") or "").strip()
    if not label:
        return {"error": "label required"}
    eid = entities.slug(label)
    typ = body.get("type") if body.get("type") in entities.TYPES else "Product"
    aliases = [a.strip() for a in (body.get("aliases") or [label]) if a and a.strip()] or [label]
    cur["added"] = [e for e in cur["added"] if entities.slug(e.get("label", "")) != eid]
    cur["added"].append(dict(label=label, type=typ, aliases=aliases,
                             parent=(body.get("parent") or "").strip() or None,
                             note=(body.get("note") or "").strip(), added=int(time.time())))
    cur["removed"] = [r for r in cur["removed"] if r != eid]      # adding un-removes
    if body.get("dismiss"):
        cur["dismissed"] = sorted(set(cur["dismissed"]) | {body["dismiss"]})
    save_cur(cur)
    return {"ok": True, "id": eid}


def do_add_alias(body):
    cur = load_cur()
    eid = (body.get("entity_id") or "").strip()
    alias = (body.get("alias") or "").strip()
    if not eid or not alias:
        return {"error": "entity_id and alias required"}
    lst = cur["aliases"].setdefault(eid, [])
    if alias not in lst:
        lst.append(alias)
    if body.get("dismiss"):
        cur["dismissed"] = sorted(set(cur["dismissed"]) | {body["dismiss"]})
    save_cur(cur)
    return {"ok": True}


def do_remove(body):
    cur = load_cur()
    eid = (body.get("id") or "").strip()
    added_ids = {entities.slug(e.get("label", "")) for e in cur["added"]}
    if eid in added_ids:
        cur["added"] = [e for e in cur["added"] if entities.slug(e.get("label", "")) != eid]
    else:
        cur["removed"] = sorted(set(cur["removed"]) | {eid})
    cur["aliases"].pop(eid, None)
    save_cur(cur)
    return {"ok": True}


def do_restore(body):
    cur = load_cur()
    cur["removed"] = [r for r in cur["removed"] if r != (body.get("id") or "").strip()]
    save_cur(cur)
    return {"ok": True}


def do_dismiss(body):
    cur = load_cur()
    s = (body.get("slug") or "").strip()
    if s:
        cur["dismissed"] = sorted(set(cur["dismissed"]) | {s})
    save_cur(cur)
    return {"ok": True}


def do_undismiss(body):
    cur = load_cur()
    cur["dismissed"] = [d for d in cur["dismissed"] if d != (body.get("slug") or "").strip()]
    save_cur(cur)
    return {"ok": True}


ROUTES = {"/api/add": do_add, "/api/add_alias": do_add_alias, "/api/remove": do_remove,
          "/api/restore": do_restore, "/api/dismiss": do_dismiss, "/api/undismiss": do_undismiss}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/state":
            q = parse_qs(u.query)
            days = int((q.get("days") or [14])[0])
            mins = int((q.get("min_stories") or [2])[0])
            try:
                self._send(state(days, mins))
            except Exception as e:
                self._send({"error": str(e)}, 500)
            return
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        fn = ROUTES.get(u.path)
        if not fn:
            self._send({"error": "not found"}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            self._send(fn(body))
        except Exception as e:
            self._send({"error": str(e)}, 500)


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>AI Map — Dictionary Curator</title>
<style>
:root{--bg:#f6f5f2;--panel:#fff;--ink:#1d1d1d;--mid:#6f6f6f;--soft:#9a9a96;--line:#d7d6d2;
--accent:#ff4a1c;--accent-soft:rgba(255,74,28,.09);--good:#1a7f37;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--body:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
@media(prefers-color-scheme:dark){:root{--bg:#141311;--panel:#1f1e1b;--ink:#e9e7e2;--mid:#a3a19b;
--soft:#6f6d67;--line:#34322e;--accent:#ff6a44;--accent-soft:rgba(255,106,68,.14);--good:#3fb950}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--body);font-size:15px}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:2px solid var(--ink);padding:14px 20px}
h1{font-family:var(--mono);font-size:18px;margin:0;letter-spacing:-.3px}
.stat{font-family:var(--mono);font-size:12px;color:var(--mid);margin-top:6px;display:flex;gap:14px;flex-wrap:wrap}
.tabs{display:flex;gap:8px;margin-top:12px}
.tabs button{font-family:var(--mono);font-size:13px;border:1px solid var(--line);background:var(--panel);
color:var(--ink);padding:6px 14px;border-radius:7px;cursor:pointer}
.tabs button.on{border-color:var(--accent);color:var(--accent);font-weight:600}
main{max-width:1000px;margin:0 auto;padding:18px 20px 60px}
.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
input,select,textarea{font:inherit;font-size:14px;color:var(--ink);background:var(--panel);
border:1px solid var(--line);border-radius:7px;padding:7px 10px}
input:focus,select:focus{outline:2px solid var(--accent-soft);border-color:var(--accent)}
button.act{font-family:var(--mono);font-size:12px;border:1px solid var(--line);background:var(--panel);
color:var(--ink);padding:5px 10px;border-radius:6px;cursor:pointer}
button.act:hover{border-color:var(--accent);color:var(--accent)}
button.pri{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}
.grp{margin:18px 0 6px;font-family:var(--mono);font-size:12px;letter-spacing:1px;text-transform:uppercase;
color:var(--accent);display:flex;align-items:center;gap:8px}
.grp .n{color:var(--soft)}
.row{display:flex;align-items:center;gap:12px;padding:8px 10px;border:1px solid var(--line);
border-radius:8px;background:var(--panel);margin-bottom:6px}
.row.rm{opacity:.5}.row.rm .lab{text-decoration:line-through}
.lab{font-weight:600}.al{font-family:var(--mono);font-size:11px;color:var(--mid);word-break:break-word}
.pill{font-family:var(--mono);font-size:10px;padding:1px 6px;border-radius:10px;border:1px solid var(--line);color:var(--mid)}
.pill.added{color:var(--good);border-color:var(--good)}
.pill.xa{color:var(--accent);border-color:var(--accent)}
.sp{flex:1}
.rec{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:10px 12px;margin-bottom:8px}
.rec .top{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.rec .ex{font-size:12.5px;color:var(--mid);margin-top:4px}
.tag{font-family:var(--mono);font-size:10px;padding:1px 6px;border-radius:10px}
.tag.new{background:var(--accent-soft);color:var(--accent)}
.tag.alias{border:1px solid var(--line);color:var(--mid)}
.muted{color:var(--mid);font-size:13px}
form.add{border:1px dashed var(--line);border-radius:8px;padding:12px;margin-bottom:16px;display:none;gap:8px;flex-wrap:wrap;align-items:center}
form.add.on{display:flex}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);
font-family:var(--mono);font-size:13px;padding:9px 16px;border-radius:8px;opacity:0;transition:opacity .2s;pointer-events:none}
#toast.on{opacity:1}
.hint{font-family:var(--mono);font-size:11px;color:var(--soft)}
</style></head><body>
<header>
  <h1>AI Map — Dictionary Curator</h1>
  <div class=stat id=stat></div>
  <div class=tabs><button id=tDict class=on onclick="tab('dict')">Dictionary</button>
  <button id=tRec onclick="tab('rec')">Recommendations</button></div>
</header>
<main>
  <div id=dict>
    <div class=bar>
      <select id=ftype onchange=renderDict()><option value="">All types</option></select>
      <input id=fsearch oninput=renderDict() placeholder="Search name or alias…" style="flex:1;min-width:160px">
      <label class=hint><input type=checkbox id=fremoved onchange=renderDict()> show removed</label>
      <button class="act pri" onclick="document.getElementById('addform').classList.toggle('on')">+ Add entity</button>
    </div>
    <form class=add id=addform onsubmit="return addEntity(event)">
      <input id=aLabel placeholder="Name (e.g. Mercury 3)" required style="min-width:180px">
      <select id=aType></select>
      <input id=aAliases placeholder="aliases, comma-separated (optional)" style="flex:1;min-width:160px">
      <input id=aParent placeholder="parent slug (optional)" style="width:160px">
      <button class="act pri" type=submit>Add</button>
      <span class=hint>aliases are matched literally; defaults to the name</span>
    </form>
    <div id=dlist></div>
  </div>
  <div id=rec style=display:none>
    <div class=bar>
      <label class=hint>window&nbsp;<select id=rdays onchange=reload()>
        <option value=7>7 days</option><option value=14 selected>14 days</option><option value=30>30 days</option></select></label>
      <label class=hint>min stories&nbsp;<select id=rmin onchange=reload()>
        <option>2</option><option selected>3</option><option>4</option><option>5</option></select></label>
      <span class=sp></span><span class=muted id=rcount></span>
    </div>
    <div id=rlist></div>
  </div>
</main>
<div id=toast></div>
<script>
let S=null;
const q=s=>document.querySelector(s);
function toast(m){const t=q('#toast');t.textContent=m;t.classList.add('on');setTimeout(()=>t.classList.remove('on'),1800)}
async function reload(){
  const days=q('#rdays').value,mins=q('#rmin').value;
  const r=await fetch(`/api/state?days=${days}&min_stories=${mins}`);S=await r.json();
  // type filters
  const ft=q('#ftype'),at=q('#aType');
  if(ft.options.length<=1){S.types.forEach(t=>{ft.add(new Option(t,t));at.add(new Option(t,t))})}
  const st=S.stats;
  q('#stat').innerHTML=`<span><b>${st.active}</b> active entities</span>`+
    Object.entries(st.by_type).map(([k,v])=>`<span>${k}: ${v}</span>`).join('')+
    `<span style="color:var(--accent)">${st.added} added · ${st.removed} removed</span>`;
  renderDict();renderRec();
}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function renderDict(){
  const type=q('#ftype').value,search=q('#fsearch').value.toLowerCase(),showRm=q('#fremoved').checked;
  let items=S.dictionary.filter(e=>(!type||e.type===type)&&(showRm||!e.removed));
  if(search)items=items.filter(e=>e.label.toLowerCase().includes(search)||
    (e.aliases||[]).join(' ').toLowerCase().includes(search));
  const byType={};items.forEach(e=>{(byType[e.type]=byType[e.type]||[]).push(e)});
  const order=S.types.filter(t=>byType[t]);
  q('#dlist').innerHTML=order.map(t=>{
    const rows=byType[t].sort((a,b)=>a.label.localeCompare(b.label)).map(e=>{
      const xa=(e.extra_aliases||[]).length?`<span class="pill xa">+${e.extra_aliases.length} alias</span>`:'';
      const src=e.source==='added'?'<span class="pill added">added</span>':'';
      const al=(e.aliases||[]).slice(0,6).join(' · ');
      const btn=e.removed?`<button class=act onclick="api('/api/restore',{id:'${e.id}'})">restore</button>`
        :`<button class=act onclick="if(confirm('Remove '+'${esc(e.label)}'+' from the map?'))api('/api/remove',{id:'${e.id}'})">remove</button>`;
      return `<div class="row ${e.removed?'rm':''}"><span class=lab>${esc(e.label)}</span>${src}${xa}
        <span class=al>${esc(al)}</span>${e.parent?`<span class=pill>↳ ${esc(e.parent)}</span>`:''}
        <span class=sp></span>${btn}</div>`;
    }).join('');
    return `<div class=grp>${t} <span class=n>${byType[t].length}</span></div>${rows}`;
  }).join('')||'<p class=muted>No matches.</p>';
}
function renderRec(){
  const rows=S.recommendations;
  q('#rcount').textContent=`${rows.length} candidates`;
  q('#rlist').innerHTML=rows.map(r=>{
    const tag=r.maps_to&&r.maps_to.length?`<span class="tag alias">alias of ${esc(r.maps_to[0])}</span>`
      :'<span class="tag new">NEW</span>';
    const add=r.maps_to&&r.maps_to.length
      ? `<button class="act pri" onclick="api('/api/add_alias',{entity_id:'${r.maps_to[0]}',alias:${JSON.stringify(r.name)},dismiss:'${r.slug}'})">add as alias</button>`
      : `<button class="act pri" onclick='addRec(${JSON.stringify(r).replace(/'/g,"&#39;")})'>add entity</button>`;
    return `<div class=rec><div class=top><span class=lab>${esc(r.name)}</span>
      <span class=pill>${r.type}</span>${tag}
      <span class=muted>${r.stories} stories · ${esc(r.top_source)} · ${r.last_seen}</span>
      <span class=sp></span>${add}
      <button class=act onclick="api('/api/dismiss',{slug:'${r.slug}'})">dismiss</button></div>
      <div class=ex>${esc((r.examples||[])[0]||'')}</div></div>`;
  }).join('')||'<p class=muted>Nothing new — the dictionary is covering what the LLM finds.</p>';
}
async function api(path,body){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json();
  if(j.error){toast('error: '+j.error);return}
  toast('saved');await reload();
}
function addRec(r){
  q('#addform').classList.add('on');q('#aLabel').value=r.name;q('#aType').value=r.type;
  q('#aAliases').value=r.name;q('#aParent').value='';q('#aLabel').dataset.dismiss=r.slug;
  tab('dict');q('#aLabel').focus();
}
function addEntity(ev){
  ev.preventDefault();
  const aliases=q('#aAliases').value.split(',').map(s=>s.trim()).filter(Boolean);
  api('/api/add',{label:q('#aLabel').value,type:q('#aType').value,aliases,
    parent:q('#aParent').value,dismiss:q('#aLabel').dataset.dismiss||''});
  q('#addform').classList.remove('on');q('#aLabel').value='';q('#aAliases').value='';
  q('#aLabel').dataset.dismiss='';return false;
}
function tab(t){
  q('#dict').style.display=t==='dict'?'':'none';q('#rec').style.display=t==='rec'?'':'none';
  q('#tDict').classList.toggle('on',t==='dict');q('#tRec').classList.toggle('on',t==='rec');
}
reload();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8096)
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    print(f"Dictionary curator on http://{a.host}:{a.port}  (overlay: {CUR})")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
