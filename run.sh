#!/usr/bin/env bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
PORT="${1:-8317}"

echo "Starting Aside Antigravity Proxy on port $PORT..."
exec python3 "$DIR/server.py" "$PORT"
