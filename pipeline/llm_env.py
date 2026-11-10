"""Print shell exports for the optional LLM step, taken from the "strata" provider in ~/.pi/agent/models.json."""
import json
import os
import shlex

try:
    p = json.load(open(os.path.expanduser("~/.pi/agent/models.json")))["providers"]["strata"]
    print("export AIMAP_LLM_BASE_URL=" + shlex.quote(p["baseUrl"]))
    print("export AIMAP_LLM_MODEL=" + shlex.quote(p["models"][0]["id"]))
    print("export AIMAP_LLM_API_KEY=" + shlex.quote(p.get("apiKey", "none")))
except Exception:
    pass
