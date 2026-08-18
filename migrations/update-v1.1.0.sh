#!/usr/bin/env bash
# v1.1.0: Remove daily refresh launchd agent in favor of dynamic model resolution.
set -e

REFRESH_PLIST="$HOME/Library/LaunchAgents/io.local.aside-antigravity-proxy.refresh.plist"
if [ -f "$REFRESH_PLIST" ]; then
    echo "  - Unloading legacy refresh launchd agent..."
    launchctl bootout "gui/$(id -u)" "$REFRESH_PLIST" 2>/dev/null || launchctl unload "$REFRESH_PLIST" 2>/dev/null || true
    rm -f "$REFRESH_PLIST"
fi
