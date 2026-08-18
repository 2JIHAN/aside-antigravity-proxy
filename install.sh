#!/usr/bin/env bash
set -e

# ponytail: simple one-click install script with auto port conflict detection and models.json registration
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
REQ_PORT="${1:-8317}"
PLIST_LABEL="io.local.aside-antigravity-proxy"
OLD_PLIST_LABEL="com.does.aside-antigravity-proxy"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
OLD_PLIST_PATH="$HOME/Library/LaunchAgents/${OLD_PLIST_LABEL}.plist"
REFRESH_LABEL="${PLIST_LABEL}.refresh"
REFRESH_PLIST_PATH="$HOME/Library/LaunchAgents/${REFRESH_LABEL}.plist"
TOKEN_PATH="$HOME/.gemini/antigravity-cli/antigravity-oauth-token"

echo "=== Aside Antigravity Proxy Installer ==="

# 1. 사전점검
echo "[1/5] Checking prerequisites..."
if ! command -v python3 >/dev/null 2>&1; then
    echo "[!] Error: python3 is not installed. Please install python3 and try again."
    exit 1
fi

PYTHON3_BIN="$(which python3)"
if [ ! -f "$DIR/server.py" ] || [ ! -f "$DIR/auth.py" ] || [ ! -f "$DIR/models.py" ]; then
    echo "[!] Error: Required proxy source files missing in $DIR"
    exit 1
fi
echo "  - python3 found: $PYTHON3_BIN"
echo "  - Source files located at: $DIR"

# 2. Google OAuth 상태 점검
echo "[2/5] Checking Google OAuth status..."
check_oauth() {
    # ponytail: TokenManager를 재사용해 만료 시 refresh까지 반영
    PROXY_DIR="$DIR" python3 - <<'EOF'
import json, os, sys, urllib.request
sys.path.insert(0, os.environ['PROXY_DIR'])
try:
    from auth import TokenManager
    token = TokenManager().get_access_token()
    req = urllib.request.Request(
        'https://www.googleapis.com/oauth2/v3/userinfo',
        headers={'Authorization': f'Bearer {token}'}
    )
    with urllib.request.urlopen(req) as resp:
        info = json.loads(resp.read().decode('utf-8'))
        print(f"  - Account: {info.get('email', 'Unknown')}")
    sys.exit(0)
except Exception:
    sys.exit(1)
EOF
}

if check_oauth; then
    echo "  - OAuth token is valid."
else
    echo "  - No valid Antigravity login found (checked the token file and the login keychain)."
    echo ""
    echo "  This proxy reuses the login created by the Antigravity CLI (agy)."
    echo "  To authenticate:"
    if command -v agy >/dev/null 2>&1; then
        echo "    1) Run 'agy' once in your terminal and complete the browser login."
    else
        echo "    1) Install the Antigravity CLI (agy), then run 'agy' once and log in."
    fi
    echo "    2) Confirm the token file was created at the path above."
    echo "    3) Re-run ./install.sh"
    echo ""
    echo "[!] Aborting: log in with agy first, then re-run this installer."
    exit 1
fi

# 3. 포트 점검 및 충돌 방지
echo "[3/5] Resolving service port..."
DETECTED_PORT=$(PROXY_DIR="$DIR" REQ_PORT="$REQ_PORT" python3 - <<'EOF'
import socket, sys, os, urllib.request, json

req_port = int(os.environ['REQ_PORT'])

def is_our_proxy(port):
    try:
        url = f"http://127.0.0.1:{port}/health"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=1) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return data.get('proxy') == 'aside-antigravity-proxy'
    except Exception:
        return False

def is_port_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False

if is_our_proxy(req_port) or is_port_available(req_port):
    print(req_port)
    sys.exit(0)

# Search for available port starting from req_port + 1
for p in range(req_port + 1, req_port + 100):
    if is_our_proxy(p) or is_port_available(p):
        print(p)
        sys.exit(0)

print(req_port)
EOF
)

PORT="$DETECTED_PORT"
if [ "$PORT" != "$REQ_PORT" ]; then
    echo "  - Port $REQ_PORT is in use by another service. Automatically selected unused port: $PORT"
else
    echo "  - Using port: $PORT"
fi

# 4. models.json 자동 등록/갱신
echo "[4/5] Running model discovery & updating aside models.json..."
python3 "$DIR/refresh_models.py" --port "$PORT" || {
    echo "  - Warning: Model discovery failed during installation. Using cached/fallback models."
    PROXY_DIR="$DIR" TARGET_PORT="$PORT" python3 - <<'EOF'
import sys, os
sys.path.insert(0, os.environ['PROXY_DIR'])
from models import get_models
from refresh_models import update_aside_models_json
update_aside_models_json(get_models(), port=os.environ['TARGET_PORT'])
print("  - Updated models.json with fallback models.")
EOF
}

# 5. launchd 등록
echo "[5/5] Registering launchd agent..."
mkdir -p "$HOME/Library/LaunchAgents"

# 기존 구 라벨 정리
UID_VAL="$(id -u)"
if [ -f "$OLD_PLIST_PATH" ]; then
    echo "  - Cleaning up legacy launchd agent ($OLD_PLIST_LABEL)..."
    launchctl bootout "gui/$UID_VAL" "$OLD_PLIST_PATH" 2>/dev/null || launchctl unload "$OLD_PLIST_PATH" 2>/dev/null || true
    rm -f "$OLD_PLIST_PATH"
fi

cat <<EOF > "$PLIST_PATH"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${DIR}/aside-antigravity-proxy</string>
        <string>${PORT}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>WorkingDirectory</key>
    <string>${DIR}</string>
    <key>StandardOutPath</key>
    <string>${DIR}/proxy.log</string>
    <key>StandardErrorPath</key>
    <string>${DIR}/proxy.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PORT</key>
        <string>${PORT}</string>
    </dict>
</dict>
</plist>
EOF

echo "  - Created plist at $PLIST_PATH"

launchctl bootout "gui/$UID_VAL" "$PLIST_PATH" 2>/dev/null || launchctl unload "$PLIST_PATH" 2>/dev/null || true

if launchctl bootstrap "gui/$UID_VAL" "$PLIST_PATH" 2>/dev/null; then
    echo "  - Successfully bootstrapped launchd agent."
elif launchctl load -w "$PLIST_PATH" 2>/dev/null; then
    echo "  - Successfully loaded launchd agent (fallback)."
else
    echo "  - Warning: Could not auto-register with launchctl."
fi

# Daily model refresh. The probe is what keeps the picker honest: Antigravity
# adds and drops models without warning, and a stale cache looks exactly like a
# broken proxy. launchd runs it at the next wake if the Mac was asleep at 10:00.
# PATH is set explicitly because launchd agents get a bare one and the probe
# shells out to `agy`.
cat <<EOF > "$REFRESH_PLIST_PATH"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${REFRESH_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>date; "${DIR}/rotate-logs.sh"; exec "${DIR}/refresh-models.sh" --port ${PORT}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>10</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>WorkingDirectory</key>
    <string>${DIR}</string>
    <key>StandardOutPath</key>
    <string>${DIR}/refresh.log</string>
    <key>StandardErrorPath</key>
    <string>${DIR}/refresh.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
EOF

echo "  - Created refresh plist at $REFRESH_PLIST_PATH"

launchctl bootout "gui/$UID_VAL" "$REFRESH_PLIST_PATH" 2>/dev/null || launchctl unload "$REFRESH_PLIST_PATH" 2>/dev/null || true

if launchctl bootstrap "gui/$UID_VAL" "$REFRESH_PLIST_PATH" 2>/dev/null; then
    echo "  - Scheduled daily model refresh at 10:00."
elif launchctl load -w "$REFRESH_PLIST_PATH" 2>/dev/null; then
    echo "  - Scheduled daily model refresh at 10:00 (fallback)."
else
    echo "  - Warning: Could not schedule the daily model refresh."
fi

# 6. 검증
echo ""
echo "[*] Verifying proxy service status..."
sleep 2

HEALTH_RES=$(curl -s "http://127.0.0.1:${PORT}/health" || echo "")
if [[ "$HEALTH_RES" == *"status"* && "$HEALTH_RES" == *"ok"* ]]; then
    echo "  [SUCCESS] Proxy is running and healthy on http://127.0.0.1:${PORT}"
else
    echo "  [WARNING] Could not verify http://127.0.0.1:${PORT}/health."
    echo "  Please check ${DIR}/proxy.err.log for details."
fi

# Record what's installed so update.sh knows which migrations to run.
cat "$DIR/VERSION" > "$DIR/.installed-version" 2>/dev/null || true

echo ""
echo "=== Setup Completed ==="
echo ""
echo "Next Steps:"
echo "1. Aside App Usage:"
echo "   Open Aside -> Settings -> AI -> Providers. 'Antigravity' is registered."
echo "   Select Antigravity models directly in the model picker."
echo ""
echo "2. Aside CLI Usage:"
echo "   aside exec --provider antigravity --model gemini-3.6-flash-high \"Reply with PONG\""
echo ""
echo "3. Model refresh:"
echo "   Reprobes daily at 10:00 (launchd: ${REFRESH_LABEL})."
echo "   Run ./refresh-models.sh yourself to reprobe right now."
echo ""
echo "4. Logs location:"
echo "   - Output log:  ${DIR}/proxy.log"
echo "   - Error log:   ${DIR}/proxy.err.log"
echo "   - Refresh log: ${DIR}/refresh.log"
echo ""
