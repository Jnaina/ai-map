#!/usr/bin/env python3
"""Agent Duel leaderboard snapshot for the ai-news Agent Leaderboard page (/agents).

Reads the PUBLIC, server-rendered pages of https://agentduel.com/leaderboard once a day:
  /leaderboard, /leaderboard?excludeEarlyVotes=true, and each ranked model's page
(14 requests, 1.5 s apart). The leaderboard's JSON API needs a signed-in app account and
robots.txt disallows /api/, so it is NOT used. Credit + links back are shown on the page.

Writes
  web/data/agents/leaderboard.json   latest snapshot (both vote filters + per-model detail)
  web/data/agents/history.json       one compact row per model per day (our own trend data)
  data/agentduel-raw/YYYY-MM-DD/*.html.gz   raw pages, kept 30 days (debugging the parser)

A parse that looks wrong (no models, missing Elo, model pages failing) keeps the last good
files, exits 1 and sends an ntfy alert (AIMAP_NTFY_URL from /opt/ai-map/.env).

  python3 pipeline/agentduel.py               fetch + write
  python3 pipeline/agentduel.py --from DIR    parse saved pages (board.html, board_ex.html,
                                              models/<lab>__<slug>.html) - for testing
  python3 pipeline/agentduel.py --dry-run     fetch + parse, print a summary, write nothing
"""
import argparse, datetime as dt, glob, gzip, json, os, re, shutil, subprocess, sys, time, urllib.request

from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "web", "data", "agents")
RAW_DIR = os.path.join(ROOT, "data", "agentduel-raw")
BASE = "https://agentduel.com"
UA = "Mozilla/5.0 (compatible; leaderboard-snapshot/1.0; daily)"
PAUSE = 1.5
RAW_KEEP_DAYS = 30
NTFY_URL = os.environ.get("AIMAP_NTFY_URL", "")
DB_CNF = os.path.join(ROOT, ".agentduel-db.cnf")   # apps-db on the Dell (agentduel_writer), chmod 600


# ---------- helpers ----------
def get(path):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": UA, "Accept": "text/html"})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 - retry any network error
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"GET {path}: {type(last).__name__}: {last}")


def txt(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def num(s):
    """'$1.58' -> 1.58, '2.8 min' -> 2.8, '1,060' -> 1060, '<1%' -> 0.5, '' -> None"""
    if s is None:
        return None
    s = s.strip()
    if s.startswith("<"):
        m = re.search(r"[\d.]+", s)
        return float(m.group()) / 2 if m else None
    m = re.search(r"-?[\d,]*\.?\d+", s.replace("−", "-"))
    return float(m.group().replace(",", "")) if m else None


def slug_of(href):
    m = re.search(r"/leaderboard/models/([^/?#]+)/([^/?#]+)", href or "")
    return f"{m.group(1)}/{m.group(2)}" if m else None


def rng(s):
    """'1008–1110' / '1–7' -> [1008, 1110]"""
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*[–-]\s*(-?\d+(?:\.\d+)?)", s or "")
    return [float(m.group(1)), float(m.group(2))] if m else None


def intify(v):
    return int(v) if isinstance(v, float) and v.is_integer() else v


# ---------- board page ----------
def parse_board(html):
    soup = BeautifulSoup(html, "html.parser")
    t = soup.select_one("p.generated-at time")
    generated = t.get("datetime") if t else None
    rows = []
    for r in soup.select("div.ratings div.rating"):
        a = r.select_one("a.model-link")
        org = r.select_one(".org")
        logo = org.select_one("img") if org else None
        elo = num(txt(r.select_one(".elo")))
        reach = txt(r.select_one(".reach"))           # +50/−52
        m = re.match(r"\+?(\d+(?:\.\d+)?)\s*/\s*[−-](\d+(?:\.\d+)?)", reach)
        ci = rng(txt(r.select_one(".bar .visually-hidden")))
        vals = [txt(x) for x in r.select(".figure-value")]
        tag = txt(r.select_one(".tag"))
        rows.append({
            "rank": int(num(txt(r.select_one(".rank")))),
            "slug": slug_of(a.get("href")),
            "name": txt(a),
            "lab": txt(org),
            "logo": (logo.get("src") or "").rsplit("/", 1)[-1] if logo else None,
            "tag": tag or None,
            "elo": intify(elo),
            "plus": intify(float(m.group(1))) if m else None,
            "minus": intify(float(m.group(2))) if m else None,
            "lo": intify(ci[0]) if ci else None,
            "hi": intify(ci[1]) if ci else None,
            "costMedian": num(vals[0]) if len(vals) > 0 and vals[0] not in ("", "—") else None,
            "timeMedian": num(vals[1]) if len(vals) > 1 and vals[1] not in ("", "—") else None,
        })
    by_name = {x["name"]: x for x in rows}

    # Chance of ranking first + rank range (ridge section, compact strip has plain text per model)
    for strip in soup.select(".ridge-strip, .ridge-row"):
        a = strip.select_one("a.model-link")
        if not a or txt(a) not in by_name:
            continue
        t_ = txt(strip)
        rr = re.search(r"Rank range (\d+)\s*[–-]\s*(\d+)", t_)
        pf = re.search(r"(<?\s*~?\d+(?:\.\d+)?)%", t_)
        x = by_name[txt(a)]
        if rr and "rankRange95" not in x:
            x["rankRange95"] = [int(rr.group(1)), int(rr.group(2))]
        if pf and "pFirst" not in x:
            x["pFirst"] = num(pf.group(1).replace("~", "")) / 100
    # fallback: anything left over from the ridge plot's text
    full = txt(soup.select_one("main"))
    for x in rows:
        if "pFirst" not in x or "rankRange95" not in x:
            m = re.search(re.escape(x["name"]) + r" Elo \d+ \+\d+/−\d+ · rank \d+ Rank range (\d+)–(\d+) · approximate ~?(<?\d+)%", full)
            if m:
                x.setdefault("rankRange95", [int(m.group(1)), int(m.group(2))])
                x.setdefault("pFirst", num(m.group(3)) / 100)

    # Pareto section: mean values + frontier membership (median/mean x cost/time)
    frontier = {}
    for body in soup.select(".pareto-body"):
        cls = body.get("class", [])
        metric = "cost" if "cost-only" in cls else "time" if "time-only" in cls else None
        stat = "median" if "median-only" in cls else "mean" if "mean-only" in cls else None
        if not metric or not stat:
            continue
        key = f"{metric}{stat.capitalize()}"
        for tip in body.select(".tip"):
            name = txt(tip.select_one(".tip-title"))
            note = txt(tip)
            m = re.search(r"(Median|Mean): \$?([\d.,]+)\s*(min)?", note)
            if name in by_name and m:
                by_name[name].setdefault(key, num(m.group(2)))
        frontier[key] = [txt(a) for a in body.select(".frontier-list-row a.model-link")]
    frontier = {k: [by_name[n]["slug"] for n in v if n in by_name] for k, v in frontier.items()}
    return {"generatedAt": generated, "models": rows, "frontier": frontier}


# ---------- model page ----------
def parse_model(html):
    soup = BeautifulSoup(html, "html.parser")
    out = {"name": txt(soup.select_one("h1.detail-title")),
           "slug": txt(soup.select_one(".detail-slug")) or None}
    # rank likelihood
    tbl = soup.select_one("table.rank-probabilities")
    if tbl:
        out["rankProb"] = [num(txt(td)) / 100 if num(txt(td)) is not None else 0 for td in tbl.select("tbody td")]
    rr = soup.select_one(".rank-range")
    if rr:
        lab = rr.get("aria-label", "")
        m95 = re.search(r"95% rank range (\d+)\s*[–-]\s*(\d+)", lab)
        m50 = re.search(r"middle 50% rank range (\d+)\s*[–-]\s*(\d+)", lab)
        if m95:
            out["rankRange95"] = [int(m95.group(1)), int(m95.group(2))]
        if m50:
            out["rankRange50"] = [int(m50.group(1)), int(m50.group(2))]
    # head to head
    h2h = []
    for tr in soup.select("table.matchups tbody tr"):
        a = tr.select_one("a.model-link")
        bar = tr.select_one(".matchup-interval")
        lab = bar.get("aria-label", "") if bar else ""
        w = re.search(r"(\d+) wins?", lab); t = re.search(r"(\d+) ties?", lab); l = re.search(r"(\d+) loss", lab)
        ci = re.search(r"interval (\d+)\s*[–-]\s*(\d+)%", lab)
        h2h.append({
            "opp": slug_of(a.get("href")) if a else None,
            "oppName": txt(a),
            "w": int(w.group(1)) if w else 0, "t": int(t.group(1)) if t else 0, "l": int(l.group(1)) if l else 0,
            "rate": (num(txt(tr.select_one("td.matchup-rate"))) or 0) / 100,
            "lo": int(ci.group(1)) / 100 if ci else None, "hi": int(ci.group(2)) / 100 if ci else None,
        })
    out["h2h"] = h2h
    # weekly Elo
    weekly = []
    for b in soup.select(".weekly-bar"):
        m = re.search(r"Week of ([A-Za-z]+ \d+, \d{4}): Elo (\d+)(?:, 95% confidence interval (\d+)\s*[–-]\s*(\d+))?", b.get("aria-label", ""))
        if m:
            d = dt.datetime.strptime(m.group(1), "%b %d, %Y").date().isoformat()
            weekly.append({"week": d, "elo": int(m.group(2)),
                           "lo": int(m.group(3)) if m.group(3) else None, "hi": int(m.group(4)) if m.group(4) else None})
    out["weekly"] = weekly
    out["weeklyPartialLast"] = bool(soup.select_one(".history-subtitle")) and "partial" in txt(soup.select_one(".history-subtitle"))
    # details table
    det = {}
    for tr in soup.select("table.model-data tr"):
        det[txt(tr.select_one("th"))] = txt(tr.select_one("td"))
    key = {
        "Eligible battles": ("battles", num), "Unique voters": ("voters", num), "Raw win rate": ("rawWinRate", lambda s: num(s) / 100),
        "Adjusted cost / task (median)": ("costMedian", num), "Adjusted cost / task (mean)": ("costMean", num),
        "Raw cost / task (median)": ("rawCostMedian", num), "Raw cost / task (mean)": ("rawCostMean", num),
        "Adjusted time / task (median)": ("timeMedian", num), "Adjusted time / task (mean)": ("timeMean", num),
        "Raw time / task (median)": ("rawTimeMedian", num), "Raw time / task (mean)": ("rawTimeMean", num),
        "Tokens per second": ("tps", num), "Lab": ("lab", str), "Served through": ("servedThrough", str),
        "Model ID": ("modelId", str), "Version": ("version", str), "Thinking": ("thinking", str),
        "First eligible vote": ("firstVote", lambda s: dt.datetime.strptime(s, "%b %d, %Y").date().isoformat()),
    }
    d = {}
    for k, v in det.items():
        if k in key:
            name, f = key[k]
            try:
                d[name] = intify(f(v)) if f is not str else v
            except Exception:  # noqa: BLE001 - keep the raw text when a value doesn't parse
                d[name] = v
        elif k:
            d.setdefault("other", {})[k] = v
    out["details"] = d
    return out


# ---------- checks ----------
def check(board, board_ex, models):
    errs = []
    for label, b in (("board", board), ("board_ex", board_ex)):
        ms = b["models"]
        if len(ms) < 5:
            errs.append(f"{label}: only {len(ms)} models parsed")
        for x in ms:
            if not x["slug"] or x["elo"] is None or x["lo"] is None:
                errs.append(f"{label}: incomplete row {x.get('name')}")
        if not b["generatedAt"]:
            errs.append(f"{label}: no generated time")
    for slug, m in models.items():
        if not m.get("rankProb") or not m.get("details", {}).get("battles"):
            errs.append(f"model {slug}: rank odds or details missing")
    if len(models) < len(board["models"]) - 1:
        errs.append(f"only {len(models)}/{len(board['models'])} model pages parsed")
    return errs


def ntfy(title, body):
    if not NTFY_URL:
        return
    try:
        req = urllib.request.Request(NTFY_URL, data=body.encode(), method="POST",
                                     headers={"Title": title, "Tags": "warning", "Priority": "default"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception:  # noqa: BLE001 - best effort
        pass


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)


# ---------- MariaDB history (apps-db `agentduel`) ----------
def tiers(models):
    out, t, i = {}, 0, 0
    arr = sorted(models, key=lambda m: m["rank"])
    while i < len(arr):
        t += 1
        lead = arr[i]
        while i < len(arr) and arr[i]["hi"] >= lead["lo"]:
            out[arr[i]["slug"]] = t
            i += 1
    return out


def q(v):
    """SQL literal for MariaDB (strings escaped; None -> NULL; bool -> 0/1)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v).replace("\\", "\\\\").replace("'", "\\'").replace("\0", "")
    return "'" + s + "'"


def ts(iso):
    if not iso:
        return None
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def snapshot_sql(snap, day):
    """One transaction: replace the day's snapshot and all its child rows."""
    A, N, M = snap["datasets"]["all"], snap["datasets"]["noEarly"], snap["models"]
    battles = sum((m.get("details") or {}).get("battles") or 0 for m in M.values()) // 2
    L = ["SET NAMES utf8mb4;", "START TRANSACTION;",
         f"DELETE FROM snapshots WHERE snap_date = {q(day)};",
         "INSERT INTO snapshots (snap_date, fetched_at, generated_all, generated_no_early, models_ranked, eligible_battles, raw_json) VALUES "
         f"({q(day)}, {q(ts(snap['fetchedAt']))}, {q(ts(A['generatedAt']))}, {q(ts(N['generatedAt']))}, {len(A['models'])}, {battles}, "
         f"{q(json.dumps(snap, separators=(',', ':'), ensure_ascii=False))});",
         "SET @sid = LAST_INSERT_ID();"]
    seen = {}
    for x in A["models"] + N["models"]:
        seen[x["slug"]] = x
    for slug, x in seen.items():
        L.append("INSERT INTO models (slug, name, lab, first_seen, last_seen) VALUES "
                 f"({q(slug)}, {q(x['name'])}, {q(x['lab'])}, {q(day)}, {q(day)}) ON DUPLICATE KEY UPDATE "
                 f"name = VALUES(name), lab = VALUES(lab), last_seen = GREATEST(last_seen, VALUES(last_seen)), first_seen = LEAST(first_seen, VALUES(first_seen));")
    for key, d in (("all", A), ("noEarly", N)):
        tr = tiers(d["models"])
        fr = {k: set(v) for k, v in (d.get("frontier") or {}).items()}
        vals = []
        for x in d["models"]:
            rr = x.get("rankRange95") or [None, None]
            vals.append("(@sid, " + ", ".join(q(v) for v in [
                key, x["slug"], x["rank"], x["elo"], x["lo"], x["hi"], x.get("plus"), x.get("minus"), tr.get(x["slug"]),
                x.get("pFirst"), rr[0], rr[1], x.get("costMedian"), x.get("costMean"), x.get("timeMedian"), x.get("timeMean"),
                x["slug"] in fr.get("costMedian", ()), x["slug"] in fr.get("costMean", ()),
                x["slug"] in fr.get("timeMedian", ()), x["slug"] in fr.get("timeMean", ())]) + ")")
        if vals:
            L.append("INSERT INTO ratings (snapshot_id, dataset, slug, rank_pos, elo, ci_lo, ci_hi, ci_plus, ci_minus, tier, p_first, "
                     "rank_lo95, rank_hi95, cost_median, cost_mean, time_median, time_mean, frontier_cost_median, frontier_cost_mean, "
                     "frontier_time_median, frontier_time_mean) VALUES\n" + ",\n".join(vals) + ";")
    for slug, m in M.items():
        d = m.get("details") or {}
        r50, r95 = m.get("rankRange50") or [None, None], m.get("rankRange95") or [None, None]
        fv = d.get("firstVote") if isinstance(d.get("firstVote"), str) and re.match(r"\d{4}-\d\d-\d\d$", d.get("firstVote", "")) else None
        num_ = lambda k: d.get(k) if isinstance(d.get(k), (int, float)) else None  # noqa: E731
        L.append("INSERT INTO model_stats (snapshot_id, slug, battles, voters, raw_win_rate, cost_median, cost_mean, raw_cost_median, "
                 "raw_cost_mean, time_median, time_mean, raw_time_median, raw_time_mean, tokens_per_sec, rank_lo50, rank_hi50, rank_lo95, "
                 "rank_hi95, served_through, model_id, version, thinking, first_vote) VALUES (@sid, " + ", ".join(q(v) for v in [
                     slug, num_("battles"), num_("voters"), num_("rawWinRate"), num_("costMedian"), num_("costMean"), num_("rawCostMedian"),
                     num_("rawCostMean"), num_("timeMedian"), num_("timeMean"), num_("rawTimeMedian"), num_("rawTimeMean"), num_("tps"),
                     r50[0], r50[1], r95[0], r95[1], d.get("servedThrough"), d.get("modelId"), d.get("version"), d.get("thinking"), fv]) + ");")
        probs = [f"(@sid, {q(slug)}, {i + 1}, {q(round(p, 4))})" for i, p in enumerate(m.get("rankProb") or [])]
        if probs:
            L.append("INSERT INTO rank_probs (snapshot_id, slug, rank_pos, prob) VALUES " + ", ".join(probs) + ";")
        hh = [f"(@sid, {q(slug)}, {q(h['opp'])}, {h['w']}, {h['t']}, {h['l']}, {q(h.get('rate'))}, {q(h.get('lo'))}, {q(h.get('hi'))})"
              for h in m.get("h2h") or [] if h.get("opp")]
        if hh:
            L.append("INSERT INTO h2h (snapshot_id, slug, opp_slug, wins, ties, losses, win_rate, ci_lo, ci_hi) VALUES " + ", ".join(hh) + ";")
        wk = m.get("weekly") or []
        ww = [f"(@sid, {q(slug)}, {q(w['week'])}, {w['elo']}, {q(w.get('lo'))}, {q(w.get('hi'))}, "
              f"{1 if (i == len(wk) - 1 and m.get('weeklyPartialLast')) else 0})" for i, w in enumerate(wk)]
        if ww:
            L.append("INSERT INTO weekly_elo (snapshot_id, slug, week_start, elo, ci_lo, ci_hi, partial) VALUES " + ", ".join(ww) + ";")
    L.append("COMMIT;")
    return "\n".join(L) + "\n"


def save_db(snap, day):
    if not os.path.exists(DB_CNF):
        return "no " + DB_CNF
    r = subprocess.run(["mariadb", f"--defaults-file={DB_CNF}", "--batch"], input=snapshot_sql(snap, day),
                       capture_output=True, text=True, timeout=120)
    return None if r.returncode == 0 else (r.stderr.strip() or f"exit {r.returncode}")[:400]


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db-only", metavar="JSON", help="load a saved leaderboard.json into MariaDB (snapshot date = its fetchedAt, SGT)")
    a = ap.parse_args()
    if a.db_only:
        snap = json.load(open(a.db_only))
        day = dt.datetime.fromisoformat(snap["fetchedAt"].replace("Z", "+00:00")).astimezone(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
        err = save_db(snap, day)
        print(f"MariaDB {day}: " + (err or "OK"))
        return 1 if err else 0

    pages = {}
    try:
        if a.src:
            pages["board"] = open(os.path.join(a.src, "board.html")).read()
            pages["board_ex"] = open(os.path.join(a.src, "board_ex.html")).read()
            for p in glob.glob(os.path.join(a.src, "models", "*.html")):
                pages["models/" + os.path.basename(p)] = open(p).read()
        else:
            pages["board"] = get("/leaderboard")
            time.sleep(PAUSE)
            pages["board_ex"] = get("/leaderboard?excludeEarlyVotes=true")
        board = parse_board(pages["board"])
        board_ex = parse_board(pages["board_ex"])
        slugs = []
        for x in board["models"] + board_ex["models"]:
            if x["slug"] and x["slug"] not in slugs:
                slugs.append(x["slug"])
        models, fails = {}, []
        for s in slugs:
            key = "models/" + s.replace("/", "__") + ".html"
            try:
                if not a.src:
                    time.sleep(PAUSE)
                    pages[key] = get("/leaderboard/models/" + s)
                if key in pages:
                    models[s] = parse_model(pages[key])
            except Exception as e:  # noqa: BLE001 - one bad model page shouldn't sink the run
                fails.append(f"{s}: {e}")
        errs = check(board, board_ex, models) + fails
    except Exception as e:  # noqa: BLE001
        board = board_ex = None
        models, errs = {}, [f"{type(e).__name__}: {e}"]

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    # raw copies for debugging, whatever happened
    if not a.src and not a.dry_run and pages:
        day = os.path.join(RAW_DIR, now[:10])
        os.makedirs(os.path.join(day, "models"), exist_ok=True)
        for k, v in pages.items():
            with gzip.open(os.path.join(day, k + ".gz"), "wt") as f:
                f.write(v)
        cutoff = time.time() - RAW_KEEP_DAYS * 86400
        for d in glob.glob(os.path.join(RAW_DIR, "*")):
            if os.path.isdir(d) and os.path.getmtime(d) < cutoff:
                shutil.rmtree(d, ignore_errors=True)

    if errs:
        msg = "; ".join(errs[:6])
        print("Agent Duel snapshot FAILED - kept last good data:", msg, file=sys.stderr)
        if not a.dry_run and not a.src:
            ntfy("AI Map: agent leaderboard snapshot failed", msg[:900])
        return 1

    snap = {
        "source": "Agent Duel", "sourceUrl": BASE + "/leaderboard",
        "methodologyUrl": BASE + "/leaderboard/methodology", "repoUrl": "https://github.com/bottomless/agent-duel",
        "fetchedAt": now,
        "datasets": {"all": board, "noEarly": board_ex},
        "models": models,
    }
    if a.dry_run or a.src:
        for x in board["models"]:
            m = models.get(x["slug"], {})
            print(f"{x['rank']:>2} {x['name']:<26} {x['elo']} [{x['lo']}-{x['hi']}] p1={x.get('pFirst')} "
                  f"cost {x.get('costMedian')}/{x.get('costMean')} time {x.get('timeMedian')}/{x.get('timeMean')} "
                  f"h2h={len(m.get('h2h', []))} weeks={len(m.get('weekly', []))} battles={m.get('details', {}).get('battles')}")
        print("frontier:", {k: len(v) for k, v in board["frontier"].items()})
        if a.src and a.dry_run is False:
            os.makedirs(OUT_DIR, exist_ok=True)
            out = os.path.join(a.src, "leaderboard.json")
            write_json(out, snap)
            print("wrote", out)
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    write_json(os.path.join(OUT_DIR, "leaderboard.json"), snap)

    # our own daily history (Singapore date), one row per model per dataset
    hp = os.path.join(OUT_DIR, "history.json")
    try:
        hist = json.load(open(hp))
    except Exception:  # noqa: BLE001 - first run
        hist = {"days": {}}
    day = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
    hist["days"][day] = {
        key: {x["slug"]: [x["rank"], x["elo"], x["lo"], x["hi"]] for x in ds["models"]}
        for key, ds in snap["datasets"].items()
    }
    hist["days"][day]["generatedAt"] = board["generatedAt"]
    hist["names"] = {**hist.get("names", {}), **{x["slug"]: x["name"] for x in board["models"] + board_ex["models"]}}
    write_json(hp, hist)
    err = save_db(snap, day)
    if err:
        print("MariaDB save FAILED (JSON files written):", err, file=sys.stderr)
        ntfy("AI Map: agent leaderboard DB save failed", err)
    print(f"Agent Duel snapshot OK: {len(board['models'])} models, {len(models)} model pages, generated {board['generatedAt']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
