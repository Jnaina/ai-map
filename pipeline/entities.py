"""Entity recognition for AI news.

Three layers:
1. SEED: a curated dictionary of well-known AI entities and their aliases.
2. Discovery: regexes for versioned model names (GPT-5.5, Claude Opus 5, Qwen3.8...),
   plus trending Hugging Face models and new GitHub repos taken straight from those sources.
3. Optional LLM extraction (any OpenAI-compatible endpoint) to catch everything else.
"""
import json
import os
import re
import urllib.request

TYPES = ["Company", "Model", "Product", "Open source", "Person", "Hardware", "Concept"]

# (label, type, aliases, parent label)  -- aliases are regex fragments; matching is case-sensitive
# unless the entity is a Concept.
SEED = [
    # --- companies / labs
    ("OpenAI", "Company", ["OpenAI"], None),
    ("Anthropic", "Company", ["Anthropic"], None),
    ("Google DeepMind", "Company", ["DeepMind", "Google DeepMind"], "Google"),
    ("Google", "Company", ["Google", "Alphabet"], None),
    ("Meta", "Company", ["Meta", "Facebook", "Meta AI", "Meta Superintelligence Labs"], None),
    ("Microsoft", "Company", ["Microsoft"], None),
    ("NVIDIA", "Company", ["NVIDIA", "Nvidia"], None),
    ("AMD", "Company", ["AMD"], None),
    ("Intel", "Company", ["Intel"], None),
    ("Apple", "Company", ["Apple"], None),
    ("Amazon", "Company", ["Amazon", "AWS"], None),
    ("xAI", "Company", ["xAI"], None),
    ("Mistral AI", "Company", ["Mistral AI", "Mistral"], None),
    ("Cohere", "Company", ["Cohere"], None),
    ("Hugging Face", "Company", ["Hugging Face", "HuggingFace"], None),
    ("DeepSeek", "Company", ["DeepSeek"], None),
    ("Alibaba (Qwen)", "Company", ["Alibaba"], None),
    ("Moonshot AI", "Company", ["Moonshot AI", "Moonshot"], None),
    ("Zhipu AI", "Company", ["Zhipu", "Z\\.ai"], None),
    ("MiniMax", "Company", ["MiniMax"], None),
    ("ByteDance", "Company", ["ByteDance", "Bytedance"], None),
    ("Tencent", "Company", ["Tencent"], None),
    ("Baidu", "Company", ["Baidu"], None),
    ("Huawei", "Company", ["Huawei"], None),
    ("Samsung", "Company", ["Samsung"], None),
    ("TSMC", "Company", ["TSMC"], None),
    ("SK hynix", "Company", ["SK hynix", "SK Hynix"], None),
    ("Broadcom", "Company", ["Broadcom"], None),
    ("Qualcomm", "Company", ["Qualcomm"], None),
    ("Arm", "Company", ["Arm Holdings"], None),
    ("Oracle", "Company", ["Oracle"], None),
    ("IBM", "Company", ["IBM"], None),
    ("Salesforce", "Company", ["Salesforce"], None),
    ("Databricks", "Company", ["Databricks"], None),
    ("Snowflake", "Company", ["Snowflake"], None),
    ("Palantir", "Company", ["Palantir"], None),
    ("CoreWeave", "Company", ["CoreWeave"], None),
    ("Nebius", "Company", ["Nebius"], None),
    ("Cerebras", "Company", ["Cerebras"], None),
    ("Groq", "Company", ["Groq"], None),
    ("Tenstorrent", "Company", ["Tenstorrent"], None),
    ("Perplexity", "Company", ["Perplexity"], None),
    ("Scale AI", "Company", ["Scale AI"], None),
    ("Safe Superintelligence", "Company", ["Safe Superintelligence", "SSI"], None),
    ("Thinking Machines Lab", "Company", ["Thinking Machines"], None),
    ("Reflection AI", "Company", ["Reflection AI"], None),
    ("Stability AI", "Company", ["Stability AI"], None),
    ("Black Forest Labs", "Company", ["Black Forest Labs"], None),
    ("Runway", "Company", ["Runway"], None),
    ("Midjourney", "Company", ["Midjourney"], None),
    ("ElevenLabs", "Company", ["ElevenLabs"], None),
    ("Anysphere (Cursor)", "Company", ["Anysphere"], None),
    ("Cognition", "Company", ["Cognition AI", "Cognition Labs"], None),
    ("Replit", "Company", ["Replit"], None),
    ("Figure AI", "Company", ["Figure AI"], None),
    ("Tesla", "Company", ["Tesla"], None),
    ("Waymo", "Company", ["Waymo"], "Google"),
    ("Character.AI", "Company", ["Character\\.AI", "Character AI"], None),
    ("Harvey", "Company", ["Harvey AI"], None),
    ("Sakana AI", "Company", ["Sakana"], None),
    ("Liquid AI", "Company", ["Liquid AI"], None),
    ("AI2", "Company", ["Allen Institute for AI", "Ai2", "AI2"], None),
    # --- model families
    ("GPT", "Model", ["GPT"], "OpenAI"),
    ("o-series reasoning", "Model", ["o3", "o4-mini", "o3-pro"], "OpenAI"),
    ("Claude", "Model", ["Claude"], "Anthropic"),
    ("Gemini", "Model", ["Gemini"], "Google"),
    ("Gemma", "Model", ["Gemma"], "Google"),
    ("Llama", "Model", ["Llama", "LLaMA"], "Meta"),
    ("Grok", "Model", ["Grok"], "xAI"),
    ("Qwen", "Model", ["Qwen"], "Alibaba (Qwen)"),
    ("Kimi", "Model", ["Kimi"], "Moonshot AI"),
    ("GLM", "Model", ["GLM"], "Zhipu AI"),
    ("Phi", "Model", ["Phi-\\d"], "Microsoft"),
    ("Nemotron", "Model", ["Nemotron"], "NVIDIA"),
    ("Command", "Model", ["Command A", "Command R"], "Cohere"),
    ("FLUX", "Model", ["FLUX", "Flux\\.\\d"], "Black Forest Labs"),
    ("Stable Diffusion", "Model", ["Stable Diffusion"], "Stability AI"),
    ("Whisper", "Model", ["Whisper"], "OpenAI"),
    ("gpt-oss", "Open source", ["gpt-oss"], "OpenAI"),
    # --- products
    ("ChatGPT", "Product", ["ChatGPT"], "OpenAI"),
    ("Sora", "Product", ["Sora"], "OpenAI"),
    ("Codex", "Product", ["Codex"], "OpenAI"),
    ("Claude Code", "Product", ["Claude Code"], "Anthropic"),
    ("NotebookLM", "Product", ["NotebookLM"], "Google"),
    ("Veo", "Product", ["Veo"], "Google"),
    ("Microsoft Copilot", "Product", ["Copilot"], "Microsoft"),
    ("GitHub Copilot", "Product", ["GitHub Copilot"], "Microsoft"),
    ("Cursor", "Product", ["Cursor"], "Anysphere (Cursor)"),
    ("Windsurf", "Product", ["Windsurf"], None),
    ("Devin", "Product", ["Devin"], "Cognition"),
    ("Perplexity Comet", "Product", ["Comet browser"], "Perplexity"),
    ("Apple Intelligence", "Product", ["Apple Intelligence", "Siri"], "Apple"),
    ("Alexa+", "Product", ["Alexa"], "Amazon"),
    ("Meta AI glasses", "Product", ["Ray-Ban Meta", "Meta Ray-Ban"], "Meta"),
    # --- open-source tools
    ("PyTorch", "Open source", ["PyTorch"], None),
    ("vLLM", "Open source", ["vLLM"], None),
    ("SGLang", "Open source", ["SGLang"], None),
    ("llama.cpp", "Open source", ["llama\\.cpp"], None),
    ("Ollama", "Open source", ["Ollama"], None),
    ("LangChain", "Open source", ["LangChain", "LangGraph"], None),
    ("LlamaIndex", "Open source", ["LlamaIndex"], None),
    ("Transformers", "Open source", ["Transformers library", "HF Transformers"], "Hugging Face"),
    ("MCP", "Open source", ["MCP", "Model Context Protocol"], "Anthropic"),
    ("OpenClaw", "Open source", ["OpenClaw"], None),
    ("ComfyUI", "Open source", ["ComfyUI"], None),
    ("MLX", "Open source", ["MLX"], "Apple"),
    ("Unsloth", "Open source", ["Unsloth"], None),
    # --- hardware
    ("Blackwell GPUs", "Hardware", ["Blackwell", "B200", "B300", "GB200", "GB300"], "NVIDIA"),
    ("Rubin GPUs", "Hardware", ["Rubin", "Vera Rubin"], "NVIDIA"),
    ("H100 / H200", "Hardware", ["H100", "H200", "Hopper"], "NVIDIA"),
    ("TPU", "Hardware", ["TPU", "TPUs", "Ironwood"], "Google"),
    ("Trainium", "Hardware", ["Trainium", "Inferentia"], "Amazon"),
    ("MI300 / MI400", "Hardware", ["MI300X", "MI325X", "MI350", "MI355X", "MI400", "MI450", "Instinct"], "AMD"),
    ("HBM memory", "Hardware", ["HBM", "HBM3E", "HBM4"], None),
    ("DGX Spark", "Hardware", ["DGX Spark", "DGX Station"], "NVIDIA"),
    # --- people
    ("Sam Altman", "Person", ["Sam Altman", "Altman"], "OpenAI"),
    ("Dario Amodei", "Person", ["Dario Amodei", "Amodei"], "Anthropic"),
    ("Demis Hassabis", "Person", ["Demis Hassabis", "Hassabis"], "Google DeepMind"),
    ("Sundar Pichai", "Person", ["Sundar Pichai", "Pichai"], "Google"),
    ("Satya Nadella", "Person", ["Satya Nadella", "Nadella"], "Microsoft"),
    ("Mustafa Suleyman", "Person", ["Mustafa Suleyman", "Suleyman"], "Microsoft"),
    ("Jensen Huang", "Person", ["Jensen Huang"], "NVIDIA"),
    ("Lisa Su", "Person", ["Lisa Su"], "AMD"),
    ("Mark Zuckerberg", "Person", ["Mark Zuckerberg", "Zuckerberg"], "Meta"),
    ("Elon Musk", "Person", ["Elon Musk", "Musk"], "xAI"),
    ("Ilya Sutskever", "Person", ["Ilya Sutskever", "Sutskever"], "Safe Superintelligence"),
    ("Mira Murati", "Person", ["Mira Murati", "Murati"], "Thinking Machines Lab"),
    ("Andrej Karpathy", "Person", ["Andrej Karpathy", "Karpathy"], None),
    ("Yann LeCun", "Person", ["Yann LeCun", "LeCun"], None),
    ("Geoffrey Hinton", "Person", ["Geoffrey Hinton", "Hinton"], None),
    ("Alexandr Wang", "Person", ["Alexandr Wang"], "Meta"),
    ("Liang Wenfeng", "Person", ["Liang Wenfeng"], "DeepSeek"),
    ("Arthur Mensch", "Person", ["Arthur Mensch"], "Mistral AI"),
    ("Aravind Srinivas", "Person", ["Aravind Srinivas"], "Perplexity"),
    ("Tim Cook", "Person", ["Tim Cook"], "Apple"),
    ("Andy Jassy", "Person", ["Andy Jassy"], "Amazon"),
    # --- concepts (case-insensitive)
    ("AI agents", "Concept", ["AI agents?", "agentic", "agents"], None),
    ("Reasoning models", "Concept", ["reasoning models?", "chain[- ]of[- ]thought"], None),
    ("Open-weight models", "Concept", ["open[- ]weights?", "open[- ]source models?"], None),
    ("RAG", "Concept", ["RAG", "retrieval[- ]augmented"], None),
    ("AGI / superintelligence", "Concept", ["AGI", "superintelligence"], None),
    ("AI safety & alignment", "Concept", ["AI safety", "alignment", "jailbreaks?", "prompt injection"], None),
    ("AI regulation", "Concept", ["AI Act", "regulation", "regulators?", "executive order"], None),
    ("Copyright & lawsuits", "Concept", ["copyright", "lawsuits?", "sued"], None),
    ("AI data centers", "Concept", ["data cent(?:er|re)s?", "gigawatts?", "Stargate"], None),
    ("AI chips & export controls", "Concept", ["export controls?", "chip ban", "semiconductors?"], None),
    ("Coding agents", "Concept", ["coding agents?", "vibe coding", "AI coding"], None),
    ("Humanoid robots", "Concept", ["humanoids?", "robotics"], None),
    ("Video generation", "Concept", ["video generation", "text-to-video", "AI video"], None),
    ("Fine-tuning", "Concept", ["fine[- ]tun(?:e|ing)", "LoRA", "RLHF", "reinforcement learning"], None),
    ("Benchmarks", "Concept", ["benchmarks?", "leaderboard", "SWE-bench", "ARC-AGI"], None),
    ("On-device AI", "Concept", ["on-device", "local LLMs?", "local models?", "edge AI"], None),
    ("AI funding", "Concept", ["raises \\$", "funding round", "valuation", "Series [A-F]"], None),
    ("Jobs & AI", "Concept", ["layoffs", "jobs", "workforce"], None),
]

# Versioned model-name discovery: (regex, family-prefix normaliser, parent)
MODEL_PATTERNS = [
    (r"\bGPT-\d(?:\.\d)?(?:[- ](?:mini|nano|pro|turbo|codex))?\b", "OpenAI"),
    (r"\bClaude (?:Opus|Sonnet|Haiku|Fable) ?\d(?:\.\d)?\b", "Anthropic"),
    (r"\bGemini \d(?:\.\d)? ?(?:Pro|Flash|Ultra|Nano|Flash-Lite)?\b", "Google"),
    (r"\bGemma ?\d\w*\b", "Google"),
    (r"\bVeo ?\d(?:\.\d)?\b", "Google"),
    (r"\bLlama ?\d(?:\.\d)?\b", "Meta"),
    (r"\bQwen ?\d(?:\.\d)?(?:-(?:Max|Coder|VL|Omni|Next|Flash|Plus))?\b", "Alibaba (Qwen)"),
    (r"\bDeepSeek[- ](?:V|R)\d(?:\.\d)?\b", "DeepSeek"),
    (r"\bGrok ?\d(?:\.\d)?\b", "xAI"),
    (r"\bKimi[- ]K\d(?:\.\d)?\b", "Moonshot AI"),
    (r"\bGLM-\d(?:\.\d)?\b", "Zhipu AI"),
    (r"\bMiniMax[- ]M\d(?:\.\d)?\b", "MiniMax"),
    (r"\bMistral (?:Large|Medium|Small) ?\d?(?:\.\d)?\b", "Mistral AI"),
    (r"\bSora ?\d\b", "OpenAI"),
    (r"\bNemotron[- ]\w+\b", "NVIDIA"),
]

# first word of a discovered model name -> family entity id
FAMILY = {"gpt": "gpt", "claude": "claude", "gemini": "gemini", "gemma": "gemma", "veo": "veo", "llama": "llama",
          "qwen": "qwen", "grok": "grok", "kimi": "kimi", "glm": "glm", "sora": "sora", "nemotron": "nemotron",
          "deepseek": "deepseek", "mistral": "mistral-ai", "minimax": "minimax"}

HF_ORGS = {
    "qwen": "Alibaba (Qwen)", "meta-llama": "Meta", "facebook": "Meta", "deepseek-ai": "DeepSeek",
    "mistralai": "Mistral AI", "google": "Google", "nvidia": "NVIDIA", "microsoft": "Microsoft",
    "openai": "OpenAI", "moonshotai": "Moonshot AI", "zai-org": "Zhipu AI", "thudm": "Zhipu AI",
    "minimaxai": "MiniMax", "black-forest-labs": "Black Forest Labs", "stabilityai": "Stability AI",
    "ibm-granite": "IBM", "apple": "Apple", "tencent": "Tencent", "baidu": "Baidu",
    "bytedance-seed": "ByteDance", "bytedance": "ByteDance", "allenai": "AI2", "huggingfacetb": "Hugging Face",
    "liquidai": "Liquid AI", "cohere": "Cohere", "coherelabs": "Cohere", "xai-org": "xAI",
}
GH_ORGS = {"openai": "OpenAI", "anthropics": "Anthropic", "google": "Google", "google-deepmind": "Google DeepMind",
           "microsoft": "Microsoft", "meta-llama": "Meta", "facebookresearch": "Meta", "nvidia": "NVIDIA",
           "huggingface": "Hugging Face", "deepseek-ai": "DeepSeek", "qwenlm": "Alibaba (Qwen)", "apple": "Apple"}


def slug(label):
    # "Qwen 3.8" and "Qwen3.8" (or "GPT 6" / "GPT-6") become the same id
    label = re.sub(r"(?<=[A-Za-z])[\s-]+(?=\d)", "", label)
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


# Is a story about AI at all? (used to drop off-topic Hacker News items)
AI_RX = re.compile(r"(?<![\w-])(?:AI|A\.I\.|AGI|LLMs?|GPTs?|genAI)(?![\w-])|"
                   r"(?i:\b(?:artificial intelligence|machine learning|deep learning|neural|transformers?|chatbots?|"
                   r"agentic|ai agents?|language models?|foundation models?|inference|fine-?tun\w*|embeddings?|"
                   r"diffusion|prompts?|reasoning models?|open[- ]weights?|training runs?|copilot|gpus?|tokens?)\b)")
GENERIC = {"llm", "llms", "ai", "gpu", "gpus", "cpu", "linux", "git", "rust", "python", "mac", "macos", "windows",
           "ios", "android", "vulkan", "esp32", "minecraft", "reddit", "trump", "ftc", "sec", "eu", "us", "china",
           "model", "models", "agent", "agents", "api", "chatbot", "internet", "web", "cloud", "aws"}


class Recogniser:
    def __init__(self):
        self.ents = {}  # id -> {label,type,parent,seed}
        self.rx = []    # (compiled regex, id)
        for label, typ, aliases, parent in SEED:
            eid = slug(label)
            self.ents[eid] = dict(label=label, type=typ, parent=slug(parent) if parent else None, seed=True)
            flags = re.IGNORECASE if typ == "Concept" else 0
            pat = r"(?<![\w.-])(?:" + "|".join(aliases) + r")(?![\w-])"
            self.rx.append((re.compile(pat, flags), eid))
        self.model_rx = [(re.compile(p), slug(parent)) for p, parent in MODEL_PATTERNS]

    def _ensure(self, label, typ, parent=None, seed=False):
        eid = slug(label)
        if eid not in self.ents:
            self.ents[eid] = dict(label=label, type=typ, parent=parent, seed=seed)
        return eid

    def find(self, story):
        text = story.get("text") or story.get("title", "")
        found = set()
        for rx, eid in self.rx:
            if rx.search(text):
                found.add(eid)
        for rx, parent in self.model_rx:
            for m in rx.finditer(text):
                name = re.sub(r"\s+", " ", m.group(0)).strip()
                found.add(self._ensure(name, "Model", parent))
                fam = FAMILY.get(re.match(r"[A-Za-z]+", name).group(0).lower())
                if fam:
                    found.add(fam)
        if story.get("hf_model"):
            org, name = story["hf_model"].split("/", 1)
            clean = re.sub(r"[-_.](?:GGUF|AWQ|GPTQ|FP8|FP4|NVFP4|MLX|bnb|4bit|8bit|i1|int4|int8|Q\d\w*)\b.*$", "", name, flags=re.I)
            parent = HF_ORGS.get(org.lower())
            found.add(self._ensure(clean, "Open source", slug(parent) if parent else None))
            if parent:
                found.add(slug(parent))
        if story.get("gh_repo"):
            org, name = story["gh_repo"].split("/", 1)
            parent = GH_ORGS.get(org.lower())
            found.add(self._ensure(name, "Open source", slug(parent) if parent else None))
            if parent:
                found.add(slug(parent))
        # a versioned model implies its family/company is discussed
        for eid in list(found):
            p = self.ents[eid].get("parent")
            if p and self.ents[eid]["type"] in ("Model",) and not self.ents[eid]["seed"]:
                found.add(p)
        return found


def _merge(rec, name, typ):
    """Map an LLM-found name onto an existing entity when it's a partial match
    (e.g. "Opus 5.5" -> "Claude Opus 5.5"), otherwise create it."""
    sid = slug(name)
    if sid in rec.ents:
        return sid
    for eid, info in rec.ents.items():
        lab = info["label"].lower()
        if lab.endswith(" " + name.lower()) or name.lower().endswith(" " + lab):
            return eid
    return rec._ensure(name, typ)


def llm_extract(stories, rec, log=print, batch=40):
    """Optional: ask an OpenAI-compatible LLM for entities the dictionary misses.
    Enabled when AIMAP_LLM_BASE_URL and AIMAP_LLM_MODEL are set."""
    base = os.environ.get("AIMAP_LLM_BASE_URL")
    model = os.environ.get("AIMAP_LLM_MODEL")
    if not base or not model:
        return {}
    key = os.environ.get("AIMAP_LLM_API_KEY", "none")
    extra = {}
    news = [s for s in stories if s["kind"] == "news" or (s["kind"] == "discussion" and AI_RX.search(s["title"]))]
    # cache: story id -> [[name, type], ...]; reuse earlier answers, only send new headlines
    cache_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "llm-cache.json")
    try:
        cache = json.load(open(cache_path))
    except Exception:
        cache = {}
    for s in news:
        for name, typ in cache.get(s["id"], []):
            extra.setdefault(s["id"], set()).add(_merge(rec, name, typ))
    todo = [s for s in news if s["id"] not in cache]
    log(f"LLM extraction: {len(news) - len(todo)} headlines from cache, {len(todo)} new via {base} ({model})")
    news = todo
    for i in range(0, len(news), batch):
        chunk = news[i:i + batch]
        lines = "\n".join(f"{j}. {s['title']}" for j, s in enumerate(chunk))
        prompt = ("Extract named entities that matter to the AI industry from each headline: AI companies and labs, "
                  "AI models, AI products, open-source AI projects, people known for AI, AI chips. Ignore anything "
                  "not about AI (politicians, sports, food, general software, operating systems, programming "
                  "languages) and generic terms (LLM, GPU, CPU, AI, agent). Types: Company, Model, Product, "
                  "Open source, Person, Hardware. Reply ONLY with JSON: "
                  '{"items":[{"i":<headline number>,"entities":[{"name":"...","type":"..."}]}]}\n\n' + lines)
        body = json.dumps({"model": model, "temperature": 0, "messages": [{"role": "user", "content": prompt}],
                           "chat_template_kwargs": {"enable_thinking": False}}).encode()
        try:
            req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body,
                                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
            out = json.loads(urllib.request.urlopen(req, timeout=300).read())
            txt = out["choices"][0]["message"]["content"]
            txt = txt[txt.find("{"): txt.rfind("}") + 1]
            for it in json.loads(txt).get("items", []):
                s = chunk[int(it["i"])]
                cache[s["id"]] = []
                for e in it.get("entities", []):
                    typ = e.get("type") if e.get("type") in TYPES else "Product"
                    name = (e.get("name") or "").strip()
                    if 2 <= len(name) <= 40 and name.lower() not in GENERIC:
                        cache[s["id"]].append([name, typ])
                        extra.setdefault(s["id"], set()).add(_merge(rec, name, typ))
            for s in chunk:  # headlines with no entities are cached as empty too
                cache.setdefault(s["id"], [])
        except Exception as ex:
            log(f"  ! LLM batch {i // batch}: {str(ex)[:100]}")
    try:
        json.dump(cache, open(cache_path, "w"))
    except Exception as ex:
        log(f"  ! could not save LLM cache: {ex}")
    return extra
