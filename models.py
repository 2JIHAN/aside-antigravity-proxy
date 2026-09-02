import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
import threading
import re
from typing import List, Dict, Any, Tuple, Optional, Set

# ponytail: path for internal cache file (gitignored)
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".models-cache.json")
ANTIGRAVITY_ENDPOINT = os.environ.get(
    'ANTIGRAVITY_ENDPOINT',
    'https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse'
)

# Reasoning-effort suffixes. Stripped from display names and used to rank variants.
EFFORT_SUFFIXES = ("-high", "-medium", "-low", "-thinking")
_EFFORT_RANK = {"-high": 3, "-medium": 2, "-low": 1, "-thinking": 2}


def _m(model_id: str, name: str, **kw: Any) -> Dict[str, Any]:
    return {
        "id": model_id,
        "name": name,
        "reasoning": kw.get("reasoning", True),
        "input": kw.get("input", ["text", "image"]),
        "contextWindow": kw.get("contextWindow", 1000000),
        "maxTokens": kw.get("maxTokens", 64000),
    }


def model_family(model_id: str) -> str:
    """Model ID without its effort suffix. Variants share one family."""
    for s in EFFORT_SUFFIXES:
        if model_id.endswith(s):
            return model_id[:-len(s)]
    return model_id


def effort_rank(model_id: str) -> int:
    """Higher is better. Used to pick which variant backs the plain name."""
    for s in EFFORT_SUFFIXES:
        if model_id.endswith(s):
            return _EFFORT_RANK[s]
    return 2


def format_display_name(model_id: str) -> str:
    """Dynamically generate human-readable name for any model ID."""
    base_id = model_family(model_id)

    if base_id.startswith("gemini-"):
        parts = base_id.split("-")
        if len(parts) >= 3:
            return f"Gemini {parts[1]} {parts[2].capitalize()}"
        elif len(parts) == 2:
            return f"Gemini {parts[1].capitalize()}"
    elif base_id.startswith("claude-"):
        parts = base_id.split("-")
        if len(parts) >= 4 and parts[1] in ("sonnet", "opus", "haiku"):
            return f"Claude {parts[1].capitalize()} {parts[2]}.{parts[3]}"
        elif len(parts) >= 4 and parts[3] in ("sonnet", "opus", "haiku"):
            return f"Claude {parts[3].capitalize()} {parts[1]}.{parts[2]}"
        elif len(parts) >= 3 and parts[1] in ("sonnet", "opus", "haiku"):
            return f"Claude {parts[1].capitalize()} {parts[2]}"
        elif len(parts) >= 3 and parts[2] in ("sonnet", "opus", "haiku"):
            return f"Claude {parts[2].capitalize()} {parts[1]}"
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


def model_sort_key(m: Dict[str, Any]) -> Tuple[int, float, int, str]:
    """Sort models intelligently:
    1. Gemini Flash (highest version first)
    2. Gemini Pro (highest version first)
    3. Claude Sonnet/Opus
    4. GPT-OSS
    5. Other
    """
    mid = m["id"].lower()
    if "gemini" in mid and "flash" in mid:
        tier = 1
    elif "gemini" in mid and "pro" in mid:
        tier = 2
    elif "claude" in mid and "sonnet" in mid:
        tier = 3
    elif "claude" in mid and "opus" in mid:
        tier = 4
    elif "claude" in mid:
        tier = 5
    elif "gpt-oss" in mid:
        tier = 6
    else:
        tier = 7

    version = 0.0
    v_match = re.search(r'(\d+)(?:[.-](\d+))?', mid)
    if v_match:
        major = v_match.group(1)
        minor = v_match.group(2) or "0"
        try:
            version = float(f"{major}.{minor}")
        except ValueError:
            version = float(major)

    return (tier, -version, -effort_rank(mid), mid)


def get_candidates() -> List[str]:
    """Dynamically discover candidate model IDs from multiple fast sources without hardcoding."""
    candidates: Set[str] = set()
    agy_bin = shutil.which('agy') or os.path.expanduser('~/.local/bin/agy')

    # 1. Non-blocking fast query to `agy models` (1.5s timeout)
    if os.path.isfile(agy_bin) and os.access(agy_bin, os.X_OK):
        try:
            res = subprocess.run([agy_bin, 'models'], capture_output=True, text=True, timeout=1.5)
            if res.returncode == 0 and res.stdout.strip():
                for line in res.stdout.splitlines():
                    mid = line.split('\t')[0].strip()
                    if mid and not mid.startswith('#') and ' ' not in mid:
                        candidates.add(mid)
        except Exception:
            pass

    # 2. Fast binary introspection of agy executable (<20ms)
    if os.path.isfile(agy_bin):
        try:
            with open(agy_bin, 'rb') as f:
                buf = f.read()
            for m in re.finditer(rb"(?:gemini-[0-9][a-z0-9.-]+|claude-[a-z0-9.-]+|gpt-oss-[a-z0-9.-]+)", buf):
                raw = m.group().decode("utf-8", errors="ignore")
                for part in re.split(r"[^a-zA-Z0-9.-]", raw):
                    part = part.strip(".-")
                    if any(part.startswith(p) for p in ["gemini-", "claude-", "gpt-oss-"]):
                        if any(k in part for k in ["flash", "pro", "sonnet", "opus", "haiku", "oss", "120b"]):
                            candidates.add(part)
        except Exception:
            pass

    # 3. Dynamic pattern generator across known version families (2.5 .. 4.0+)
    for v in ["2.5", "3.0", "3.1", "3.5", "3.6", "3.7", "3.8", "4.0"]:
        for t in ["flash", "pro"]:
            for e in ["-high", "-medium", "-low", ""]:
                candidates.add(f"gemini-{v}-{t}{e}")
    for v in ["4-5", "4-6", "4-7", "4-8", "5"]:
        for m in ["sonnet", "opus", "haiku"]:
            for e in ["-thinking", ""]:
                candidates.add(f"claude-{m}-{v}{e}")
    for v in ["3-7", "3.7", "3-5", "3.5"]:
        candidates.add(f"claude-{v}-sonnet")
        candidates.add(f"claude-{v}-sonnet-thinking")

    # 4. Include previously cached models
    cached = load_cached_models(raw=True)
    if cached and isinstance(cached.get("models"), list):
        for m in cached["models"]:
            candidates.add(m["id"])

    # Baseline seed models
    seed_models = [
        "gemini-3.8-flash-high", "gemini-3.8-flash",
        "gemini-3.6-flash-high", "gemini-3.5-flash-low",
        "gemini-3.1-pro-low", "gemini-3-flash",
        "claude-sonnet-4-6", "claude-opus-4-6-thinking", "gpt-oss-120b-medium"
    ]
    for sm in seed_models:
        candidates.add(sm)

    # Sort prioritizing newer versions
    return sorted(candidates, key=lambda x: (
        not x.startswith("gemini-3.8"),
        not x.startswith("gemini-3.6"),
        not x.startswith("gemini-3.7"),
        not x.startswith("claude-"),
        x
    ))

def probe_model(model_id: str, token: Optional[str] = None) -> Tuple[bool, int, str]:
    """Probe gateway with a minimal prompt to verify model availability and text response.

    Pass `token` to reuse one access token across a sweep. Without it every probe
    builds a fresh TokenManager, and a fresh manager holds no token — so it spends
    a full OAuth round-trip before it can ask about a single model.
    """
    from auth import TokenManager
    from translator import anthropic_to_antigravity

    anthropic_req = {
        "model": model_id,
        "max_tokens": 8000,
        "messages": [{"role": "user", "content": "say ok"}]
    }
    ag_payload = anthropic_to_antigravity(anthropic_req)
    ag_payload['model'] = model_id

    if token is None:
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
        with urllib.request.urlopen(req, timeout=3.5) as resp:
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
    """Probe candidate models concurrently (16 workers, sub-second) and return verified models."""
    from auth import TokenManager

    candidates = get_candidates()
    results: Dict[str, Tuple[bool, int, str]] = {}
    verified_models: List[Dict[str, Any]] = []

    try:
        token = TokenManager().get_access_token()
    except Exception as e:
        failure = (False, 0, f"Token error: {e}")
        for mid in candidates:
            results[mid] = failure
            if progress_callback:
                progress_callback(mid, failure)
        return [], results

    def _worker(model_id: str):
        res = probe_model(model_id, token=token)
        if progress_callback:
            progress_callback(model_id, res)
        return (model_id, res)

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(_worker, mid) for mid in candidates]
        for f in futures:
            mid, res = f.result()
            results[mid] = res
            if res[0]:
                kwargs = {}
                if "gpt-oss-120b" in mid:
                    kwargs["contextWindow"] = 400000
                verified_models.append(_m(mid, format_display_name(mid), **kwargs))

    best_models = pick_best_per_family(verified_models)
    best_models.sort(key=model_sort_key)
    return best_models, results


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
    fallback = [
        _m("gemini-3.8-flash-high", "Gemini 3.8 Flash"),
        _m("gemini-3.6-flash-high", "Gemini 3.6 Flash"),
        _m("gemini-3.5-flash-low", "Gemini 3.5 Flash"),
        _m("gemini-3.1-pro-low", "Gemini 3.1 Pro"),
        _m("claude-sonnet-4-6", "Claude Sonnet 4.6"),
        _m("claude-opus-4-6-thinking", "Claude Opus 4.6"),
        _m("gpt-oss-120b-medium", "GPT-OSS 120B", contextWindow=400000),
    ]
    return None if raw else fallback


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
    """Return currently available models from cache (or dynamic fallback). NO PROBING."""
    models = load_cached_models()
    return models if models else []


def get_default_model() -> str:
    """Return default model ID (first model in list)."""
    models = get_models()
    return models[0]["id"] if models else "gemini-3.8-flash-high"


_refresh_lock = threading.Lock()
_is_refreshing = False


def is_cache_stale(ttl_seconds: int = 3600) -> bool:
    """Check if the models cache is older than ttl_seconds or missing/empty."""
    if not os.path.isfile(CACHE_FILE):
        return True
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            fetched_at = data.get("fetched_at", 0)
            models = data.get("models", [])
            if not models:
                return True
            return (time.time() - fetched_at) > ttl_seconds
    except Exception:
        return True


def refresh_models_background(port: str = "8317", on_complete: Optional[Any] = None) -> bool:
    """Trigger a non-blocking background model probe and config update."""
    global _is_refreshing
    with _refresh_lock:
        if _is_refreshing:
            return False
        _is_refreshing = True

    def _run():
        global _is_refreshing
        try:
            from refresh_models import run_refresh
            run_refresh(port=str(port), quiet=True)
        except Exception as ex:
            sys.stderr.write(f"[AutoRefresh] Background model refresh failed: {ex}\n")
        finally:
            with _refresh_lock:
                _is_refreshing = False
            if on_complete:
                try:
                    on_complete()
                except Exception:
                    pass

    t = threading.Thread(target=_run, name="aside-model-refresh", daemon=True)
    t.start()
    return True


def invalidate_model(model_id: str, port: str = "8317") -> None:
    """Remove an invalid/dead model from cache and trigger async reprobe."""
    try:
        cached = load_cached_models(raw=True)
        if cached and isinstance(cached.get("models"), list):
            new_models = [m for m in cached["models"] if m["id"] != model_id and model_family(m["id"]) != model_family(model_id)]
            if len(new_models) != len(cached["models"]):
                save_cache(new_models)
                from refresh_models import update_aside_models_json
                update_aside_models_json(new_models, port=str(port))
    except Exception as ex:
        sys.stderr.write(f"[Invalidate] Failed to update cache for {model_id}: {ex}\n")
    refresh_models_background(port=str(port))


def get_fallback_model(failed_model_id: str) -> Optional[str]:
    """Find the best alternative working model when failed_model_id errors with 404."""
    models = get_models()
    if not models:
        return None

    # 1. Try finding another model in the same broad family (e.g. gemini -> newest gemini)
    fam_prefix = failed_model_id.split("-")[0] if "-" in failed_model_id else ""
    if fam_prefix:
        for m in models:
            if m["id"] != failed_model_id and m["id"].startswith(fam_prefix):
                return m["id"]

    # 2. Otherwise return the default model
    for m in models:
        if m["id"] != failed_model_id:
            return m["id"]
    return None


# Backward compatibility attributes
def __getattr__(name: str) -> Any:
    if name == "DEFAULT_MODEL":
        return get_default_model()
    if name == "SUPPORTED_MODELS":
        return get_models()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
