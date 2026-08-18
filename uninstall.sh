#!/usr/bin/env bash
set -e

# ponytail: simple uninstall script for macOS launchd daemon and models.json provider cleanup
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
PORT="${1:-8317}"
PLIST_LABEL="io.local.aside-antigravity-proxy"
OLD_PLIST_LABEL="com.does.aside-antigravity-proxy"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
OLD_PLIST_PATH="$HOME/Library/LaunchAgents/${OLD_PLIST_LABEL}.plist"
REFRESH_PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.refresh.plist"

echo "=== Aside Antigravity Proxy Uninstaller ==="

UID_VAL="$(id -u)"
echo "[1/4] Unregistering launchd agents..."
# The refresh agent has to go too. Left behind it wakes every morning to probe
# for a proxy that isn't there and writes the failure to a log nobody reads.
for ppath in "$PLIST_PATH" "$OLD_PLIST_PATH" "$REFRESH_PLIST_PATH"; do
    if [ -f "$ppath" ]; then
        launchctl bootout "gui/$UID_VAL" "$ppath" 2>/dev/null || launchctl unload "$ppath" 2>/dev/null || true
        rm -f "$ppath"
    fi
done

echo "[2/4] Removing provider from models.json..."
python3 - <<'EOF'
import json, os, glob, shutil

aside_u_dir = os.path.expanduser('~/.aside/u')
if os.path.isdir(aside_u_dir):
    for entry in os.listdir(aside_u_dir):
        models_file = os.path.join(aside_u_dir, entry, 'models.json')
        if os.path.isfile(models_file):
            try:
                shutil.copy2(models_file, models_file + '.bak')
                with open(models_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict) and 'providers' in data and isinstance(data['providers'], dict):
                    removed = False
                    for key in ['antigravity', 'local-antigravity-proxy']:
                        if key in data['providers']:
                            del data['providers'][key]
                            removed = True
                    if removed:
                        with open(models_file, 'w', encoding='utf-8') as f:
                            json.dump(data, f, indent=2, ensure_ascii=False)
                        print(f"  - Removed Antigravity provider keys from {models_file}")
            except Exception as e:
                print(f"  - Warning: Failed to update {models_file}: {e}")
EOF

echo "[3/4] Terminating leftover proxy processes..."
pkill -f "python3.*server.py" 2>/dev/null || true

echo "[4/4] Cleanup complete."
echo ""
echo "=== Uninstall Completed ==="
echo "Note: OAuth tokens (~/.gemini/antigravity-cli/antigravity-oauth-token) and source files remain intact."
