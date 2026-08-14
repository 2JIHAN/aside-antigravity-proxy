import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Tuple, Optional

# ponytail: path for internal cache file (gitignored)
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".models-cache.json")
ANTIGRAVITY_ENDPOINT = os.environ.get(
    'ANTIGRAVITY_ENDPOINT',
    'https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse'
)

# Static candidate IDs used if `agy models` is missing/failing and no cache exists
STATIC_CANDIDATES = [
    "gemini-3.6-flash-high",
    "gemini-3.6-flash-medium",
    "gemini-3.6-flash-low",
    "gemini-3.5-flash-high",
    "gemini-3.5-flash-medium",
    "gemini-3.5-flash-low",
    "gemini-3.1-pro-high",
    "gemini-3.1-pro-low",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
]

def _m(model_id: str, name: str, **kw: Any) -> Dict[str, Any]:
    return {
        "id": model_id,
        "name": name,
        "reasoning": kw.get("reasoning", True),
        "input": kw.get("input", ["text", "image"]),
        "contextWindow": kw.get("contextWindow", 1000000),
        "maxTokens": kw.get("maxTokens", 64000),
    }

# ponytail: fallback list matching known baseline if cache and discovery both fail
STATIC_FALLBACK_MODELS: List[Dict[str, Any]] = [
    _m("gemini-3.6-flash-high", "Gemini 3.6 Flash"),
    _m("gemini-3.5-flash-low", "Gemini 3.5 Flash"),
    _m("gemini-3.1-pro-low", "Gemini 3.1 Pro"),
    _m("claude-sonnet-4-6", "Claude Sonnet 4.6"),
    _m("claude-opus-4-6-thinking", "Claude Opus 4.6"),
    _m("gpt-oss-120b-medium", "GPT-OSS 120B", contextWindow=400000),
]

# Reasoning-effort suffixes. They are stripped from display names and used to
# rank variants of the same model — the picker shows one entry per model, not
# one per effort level.
EFFORT_SUFFIXES = ("-high", "-medium", "-low", "-thinking")
_EFFORT_RANK = {"-high": 3, "-medium": 2, "-low": 1, "-thinking": 2}


def model_family(model_id: str) -> str:
    """Model ID without its effort suffix. Variants share one family."""
    for s in EFFORT_SUFFIXES:
        if model_id.endswith(s):
            return model_id[: -len(s)]
    return model_id


def effort_rank(model_id: str) -> int:
    """Higher is better. Used to pick which variant backs the plain name."""
    for s in EFFORT_SUFFIXES:
        if model_id.endswith(s):
            return _EFFORT_RANK[s]
    return 2


def format_display_name(model_id: str) -> str:
    """Human-readable name for a model ID, without the effort suffix."""
    base_id = model_family(model_id)

    if base_id.startswith("gemini-"):
        parts = base_id.split("-")
        if len(parts) >= 3:
            return f"Gemini {parts[1]} {parts[2].capitalize()}"
    elif base_id.startswith("claude-"):
        parts = base_id.split("-")
        if len(parts) >= 4:
            return f"Claude {parts[1].capitalize()} {parts[2]}.{parts[3]}"
    elif base_id.startswith("gpt-oss-"):
        parts = base_id.split("-")
        if len(parts) >= 3:
            return f"GPT-OSS {parts[2].upper()}"

    return base_id.replace("-", " ").title()


def pick_best_per_family(models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep one model per family — the highest effort variant that works."""
    best: Dict[str, Dict[str, Any]] = {}
    for m in models:
        fam = model_family(m["id"])
        if fam not in best or effort_rank(m["id"]) > effort_rank(best[fam]["id"]):
            best[fam] = m
    return [m for m in models if best.get(model_family(m["id"]), {}).get("id") == m["id"]]

def get_candidates() -> List[str]:
    """Collect candidate model IDs from `agy models`, falling back to a static list.

    The cache holds only models that passed, so it must never be the sole source
    of candidates — probing just the survivors would shrink the list on every run
    and a model that starts working again would never be retried.
    """
    agy_bin = shutil.which('agy') or os.path.expanduser('~/.local/bin/agy')
    if os.path.isfile(agy_bin) and os.access(agy_bin, os.X_OK):
        try:
            # `agy models` takes ~3s on a warm start; leave real headroom.
            res = subprocess.run([agy_bin, 'models'], capture_output=True, text=True, timeout=30)
            if res.returncode == 0 and res.stdout.strip():
                # `agy models` prints "<id>\t<display name>". Taking the whole line sends
                # "gemini-3.6-flash-high\tGemini 3.6 Flash (High)" as the model id and the
                # gateway answers 404 for every candidate — the refresh then keeps the stale
                # cache and looks like the models vanished (2026-08-14). Keep the first field
                # only; a plain id with no tab survives this untouched.
                lines = [
                    line.split('\t')[0].strip() for line in res.stdout.splitlines()
                    if line.strip() and not line.startswith('#')
                ]
                lines = [mid for mid in lines if mid and ' ' not in mid]
                if lines:
                    return lines
        except Exception:
            pass

    # agy unavailable: probe the static baseline plus anything already known good.
    candidates = list(STATIC_CANDIDATES)
    cached = load_cached_models(raw=True)
    if cached and isinstance(cached.get("models"), list):
        for m in cached["models"]:
            if m["id"] not in candidates:
                candidates.append(m["id"])
    return candidates

def probe_model(model_id: str) -> Tuple[bool, int, str]:
    """Probe gateway with a minimal prompt to verify model availability and text response."""
    from auth import TokenManager
    from translator import anthropic_to_antigravity

    anthropic_req = {
        "model": model_id,
        "max_tokens": 8000,
        "messages": [{"role": "user", "content": "say ok"}]
    }
    ag_payload = anthropic_to_antigravity(anthropic_req)
    ag_payload['model'] = model_id

    try:
        token = TokenManager().get_access_token()
    except Exception as e:
        return (False, 0, f"Token error: {e}")

    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'User-Agent': 'antigravity-cli'
    }
    data = json.dumps(ag_payload).encode('utf-8')
    req = urllib.request.Request(ANTIGRAVITY_ENDPOINT, data=data, headers=headers, method='POST')

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            has_text = False
            for raw_line in resp:
                line = raw_line.decode('utf-8', errors='ignore').strip()
                if line.startswith('data: '):
                    data_json = line[6:].strip()
                    if not data_json:
                        continue
                    try:
                        payload = json.loads(data_json)
                        candidates = payload.get('response', {}).get('candidates', [])
                        for cand in candidates:
                            for part in cand.get('content', {}).get('parts', []):
                                if part.get('text', '').strip():
                                    has_text = True
                                    break
                            if has_text:
                                break
                    except Exception:
                        pass
                if has_text:
                    break

            if has_text:
                return (True, 200, "OK")
            else:
                return (False, 200, "Empty Response")
    except urllib.error.HTTPError as e:
        return (False, e.code, f"HTTP {e.code}")
    except Exception as ex:
        return (False, 0, str(ex))

def discover_models(progress_callback: Optional[Any] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Tuple[bool, int, str]]]:
    """Probe candidate models in parallel (max 3 workers) and return verified models and detail results."""
    candidates = get_candidates()
    results: Dict[str, Tuple[bool, int, str]] = {}
    verified_models: List[Dict[str, Any]] = []

    def _worker(model_id: str):
        res = probe_model(model_id)
        if progress_callback:
            progress_callback(model_id, res)
        return (model_id, res)

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(_worker, mid) for mid in candidates]
        for f in futures:
            mid, res = f.result()
            results[mid] = res
            if res[0]:
                kwargs = {}
                if "gpt-oss-120b" in mid:
                    kwargs["contextWindow"] = 400000
                verified_models.append(_m(mid, format_display_name(mid), **kwargs))

    # ponytail: one entry per model, not one per effort level
    return pick_best_per_family(verified_models), results

def load_cached_models(raw: bool = False) -> Any:
    """Read cached models from file without probing."""
    if os.path.isfile(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if raw:
                    return data
                if isinstance(data, dict) and isinstance(data.get("models"), list) and data["models"]:
                    return data["models"]
        except Exception:
            pass
    return None if raw else list(STATIC_FALLBACK_MODELS)

def save_cache(models: List[Dict[str, Any]]) -> None:
    """Save verified models to cache file."""
    data = {
        "fetched_at": int(time.time()),
        "models": models
    }
    tmp_file = CACHE_FILE + ".tmp"
    with open(tmp_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_file, CACHE_FILE)

def get_models() -> List[Dict[str, Any]]:
    """Return currently available models from cache (or static fallback). NO PROBING."""
    models = load_cached_models()
    return models if models else list(STATIC_FALLBACK_MODELS)

def get_default_model() -> str:
    """Return default model ID (first model in list)."""
    models = get_models()
    return models[0]["id"] if models else "gemini-3.6-flash-high"

# Backward compatibility attributes
def __getattr__(name: str) -> Any:
    if name == "DEFAULT_MODEL":
        return get_default_model()
    if name == "SUPPORTED_MODELS":
        return get_models()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
