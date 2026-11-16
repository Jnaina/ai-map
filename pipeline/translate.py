"""Display-only English translation of Mandarin headlines for the on-map "Incoming feed".

The ticker in the lower-left of the map (new_counts.json -> `latest`) streams raw headlines
straight from each source. Chinese-language AI outlets (QbitAI / 量子位, Jiqizhixin / 机器之心,
...) therefore show their titles in Mandarin. This module adds an *additive* `title_en` field to
those ticker items so the feed can render an English gloss, WITHOUT ever touching the stored
original: the articles DB, articles.json, topics.json and the map nodes all keep the source bytes.
`title_en` exists only inside new_counts.json's `latest[]`, and only for Mandarin titles.

Each title is translated once via the local OpenAI-compatible LLM (the same Strata/Qwen endpoint the
entity extractor uses, from AIMAP_LLM_BASE_URL / AIMAP_LLM_MODEL / AIMAP_LLM_API_KEY) and cached by
URL in data/title-en-cache.json, so a title is never re-translated on later 2-min builds. Entirely
best-effort: if the LLM is disabled or unreachable, items simply carry no `title_en` and the ticker
falls back to the original title.
"""
import json
import os
import re
import urllib.request

# Han ideographs (incl. the common extension-A block) = the Mandarin signal. Japanese kana and
# Korean hangul are detected separately so a Japanese/Korean headline (which also uses Han) is left
# alone -- the user asked for Mandarin only.
_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_KANA = re.compile(r"[぀-ゟ゠-ヿ]")      # hiragana + katakana (Japanese)
_HANGUL = re.compile(r"[가-힣ᄀ-ᇿ]")    # hangul syllables + jamo (Korean)

_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "title-en-cache.json")


def is_mandarin(title):
    """True when `title` is Mandarin: at least two Han ideographs and no Japanese kana / Korean
    hangul. The two-char floor keeps an otherwise-English headline that merely quotes a single
    Chinese character from being sent for translation."""
    if not title or _KANA.search(title) or _HANGUL.search(title):
        return False
    return len(_HAN.findall(title)) >= 2


def _load_cache(path):
    try:
        c = json.load(open(path))
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def _translate_batch(titles, base, model, key, timeout=300):
    """Ask the LLM to translate a list of Mandarin titles to English. Returns a list the same length
    as `titles` (None for any the model omitted). Raises on transport/parse failure (caller guards)."""
    lines = "\n".join(f"{i}. {t}" for i, t in enumerate(titles))
    prompt = (
        "Translate each of these news headlines from Chinese into natural, concise English. "
        "Keep proper nouns, company names, model names and acronyms (e.g. NVIDIA, Qwen, NeurIPS, "
        "VLA) as they are; do not add commentary, quotation marks or explanations; translate the "
        "whole headline including any Chinese already mixed with English. Reply ONLY with JSON: "
        '{"items":[{"i":<number>,"en":"<english headline>"}]}\n\n' + lines
    )
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    out = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    txt = out["choices"][0]["message"]["content"]
    txt = txt[txt.find("{"): txt.rfind("}") + 1]
    res = [None] * len(titles)
    for it in json.loads(txt).get("items", []):
        try:
            i = int(it["i"])
            en = (it.get("en") or "").strip()
        except Exception:
            continue
        if 0 <= i < len(titles) and en:
            res[i] = en
    return res


def annotate_latest_en(latest, log=print, batch=20, cache_path=_CACHE_PATH):
    """Mutate `latest` in place: add `title_en` to every Mandarin ticker item. Cached by URL so each
    title is translated once. No-op (and no field added) when the LLM env is unset or unreachable."""
    base = os.environ.get("AIMAP_LLM_BASE_URL")
    model = os.environ.get("AIMAP_LLM_MODEL")
    if not base or not model:
        return
    key = os.environ.get("AIMAP_LLM_API_KEY", "none")

    cache = _load_cache(cache_path)  # {url: {"t": original_title, "en": translation}}
    # Candidates: Mandarin titles only. Apply any valid cache hit; collect the rest to translate.
    todo = []  # list of (url, title) still needing a translation this run
    seen_urls = set()
    for it in latest:
        url, title = it.get("url"), it.get("title")
        if not url or not is_mandarin(title):
            continue
        seen_urls.add(url)
        hit = cache.get(url)
        if hit and hit.get("t") == title and hit.get("en"):
            it["title_en"] = hit["en"]
        else:
            todo.append((url, title))

    # De-dup identical titles within this run so we don't pay for the same headline twice.
    uniq = {}
    for url, title in todo:
        uniq.setdefault(title, []).append(url)
    pending = list(uniq.items())  # [(title, [urls...])]

    done = 0
    for i in range(0, len(pending), batch):
        chunk = pending[i:i + batch]
        titles = [t for t, _ in chunk]
        try:
            res = _translate_batch(titles, base, model, key)
        except Exception as e:
            log(f"[translate] batch failed ({e}); leaving {len(titles)} title(s) untranslated")
            continue
        by_title = {}
        for (title, urls), en in zip(chunk, res):
            if not en:
                continue
            by_title[title] = en
            for u in urls:
                cache[u] = {"t": title, "en": en}
            done += 1
        for it in latest:
            if "title_en" not in it and it.get("title") in by_title:
                it["title_en"] = by_title[it["title"]]

    # Prune the cache to URLs still in the current ticker window so it can't grow without bound
    # (the feed only ever shows the last 24h / latest_max items).
    cache = {u: v for u, v in cache.items() if u in seen_urls}
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        json.dump(cache, open(cache_path, "w"), ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        log(f"[translate] cache write skipped: {e}")

    translated = sum(1 for it in latest if it.get("title_en"))
    if translated or done:
        log(f"[translate] Mandarin ticker titles: {translated} shown in English "
            f"({done} newly translated this run, {translated - done} from cache)")
