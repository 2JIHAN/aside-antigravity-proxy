#!/usr/bin/env python3
import json
import os
import shutil
import sys
import argparse
from typing import List, Dict, Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import (
    get_models,
    discover_models,
    save_cache,
    CACHE_FILE
)

def update_aside_models_json(models: List[Dict[str, Any]], port: str = "8317") -> List[str]:
    """Update ~/.aside/u/*/models.json with the discovered models under key 'antigravity'."""
    aside_u_dir = os.path.expanduser('~/.aside/u')
    target_dirs = []
    if os.path.isdir(aside_u_dir):
        for entry in os.listdir(aside_u_dir):
            full_p = os.path.join(aside_u_dir, entry)
            if os.path.isdir(full_p):
                target_dirs.append(full_p)

    if not target_dirs:
        default_u0 = os.path.join(aside_u_dir, '0')
        os.makedirs(default_u0, exist_ok=True)
        target_dirs.append(default_u0)

    updated_files = []
    for d in target_dirs:
        models_file = os.path.join(d, 'models.json')
        data = {}
        target_port = port

        if os.path.isfile(models_file):
            try:
                shutil.copy2(models_file, models_file + '.bak')
                with open(models_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                data = {}

        if not isinstance(data, dict):
            data = {}
        if 'providers' not in data or not isinstance(data['providers'], dict):
            data['providers'] = {}

        # Preserve port if existing provider already has baseUrl
        if 'antigravity' in data['providers']:
            existing_url = data['providers']['antigravity'].get('baseUrl', '')
            if existing_url.startswith('http://127.0.0.1:'):
                target_port = existing_url.split(':')[-1].strip('/')

        proxy_provider_data = {
            "name": "Antigravity",
            "baseUrl": f"http://127.0.0.1:{target_port}",
            "apiKey": "dummy-local-key",
            "api": "anthropic-messages",
            "authHeader": False,
            "models": [
                {
                    "id": m["id"],
                    "name": m["name"],
                    "reasoning": m.get("reasoning", True),
                    "input": m.get("input", ["text", "image"]),
                    "contextWindow": m.get("contextWindow", 1000000),
                    "maxTokens": m.get("maxTokens", 64000)
                }
                for m in models
            ]
        }

        # Remove legacy key if present
        if 'local-antigravity-proxy' in data['providers']:
            del data['providers']['local-antigravity-proxy']

        data['providers']['antigravity'] = proxy_provider_data

        with open(models_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        updated_files.append(models_file)

    return updated_files

def run_refresh(port: str = "8317", quiet: bool = False) -> bool:
    if not quiet:
        print("=== Aside Antigravity Proxy Model Refresh ===")
        print("[*] Probing Antigravity Gateway for available models...")

    old_models = get_models()
    old_ids = [m["id"] for m in old_models]

    count = [0]
    total_candidates = [0]

    def _progress(model_id: str, res: tuple):
        count[0] += 1
        ok, status, msg = res
        symbol = "✅ OK" if ok else f"❌ FAIL ({msg})"
        if not quiet:
            print(f"  [{count[0]}] Probing {model_id:<28} ... {symbol}")

    new_models, details = discover_models(progress_callback=_progress)

    if not new_models:
        if not quiet:
            print("\n[!] Warning: Probing failed to find any valid models. Keeping existing cache intact.")
        return False

    new_ids = [m["id"] for m in new_models]

    # Save to .models-cache.json
    save_cache(new_models)
    if not quiet:
        print(f"\n[*] Updated local cache file: {CACHE_FILE}")

    # Update aside models.json
    updated_files = update_aside_models_json(new_models, port=port)
    if not quiet:
        for uf in updated_files:
            print(f"[*] Updated Aside config: {uf}")

    # Calculate diff
    added = set(new_ids) - set(old_ids)
    removed = set(old_ids) - set(new_ids)

    if not quiet:
        print("\n=== Model Discovery Summary ===")
        if added:
            for mid in sorted(added):
                print(f"  + {mid} 추가")
        if removed:
            for mid in sorted(removed):
                reason = details.get(mid, (False, 0, "Not found"))[2]
                print(f"  - {mid} 제거 ({reason})")
        if not added and not removed:
            print("  (변경 사항 없음: 이전 목록과 동일)")

        print(f"\nActive Models ({len(new_models)}):")
        for m in new_models:
            print(f"  - {m['id']:<28} -> {m['name']}")
        print()

    return True

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Refresh proxy model cache and aside configuration.")
    parser.add_argument("--port", default="8317", help="Service port for Aside baseUrl")
    parser.add_argument("--quiet", action="store_true", help="Suppress non-error output")
    args = parser.parse_args()

    success = run_refresh(port=args.port, quiet=args.quiet)
    sys.exit(0 if success else 1)
