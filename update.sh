#!/usr/bin/env bash
# Pull the latest code, run whatever one-time migrations you skipped, reinstall.
#
# Most releases need no migration at all: install.sh rewrites the launchd agent
# and the Aside provider entry from scratch every time, so re-running it is the
# upgrade. migrations/update-v<version>.sh exists only for the rare release that
# has to touch something install.sh won't fix on its own.
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

STATE_FILE="$DIR/.installed-version"
MIGRATIONS="$DIR/migrations"

installed_version() {
    [ -f "$STATE_FILE" ] && cat "$STATE_FILE" || echo "0.0.0"
}

# Sorts two versions and checks the smaller one isn't $2 — i.e. $1 < $2.
version_lt() {
    [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" = "$1" ]
}

echo "=== Aside Antigravity Proxy Updater ==="

FROM="$(installed_version)"
echo "[1/4] Installed version: $FROM"

echo "[2/4] Pulling latest code..."
if ! git diff --quiet 2>/dev/null; then
    echo "  [!] You have local changes. Stash or discard them first:"
    echo "      git stash        # keep them"
    echo "      git checkout --  .   # throw them away"
    exit 1
fi
git pull --ff-only
TO="$(cat "$DIR/VERSION" 2>/dev/null || echo '0.0.0')"
echo "  - Now on version: $TO"

echo "[3/4] Running migrations..."
ran=0
if [ -d "$MIGRATIONS" ]; then
    # Version order, not filename order: v1.0.10 must run after v1.0.9.
    for script in $(ls "$MIGRATIONS"/update-v*.sh 2>/dev/null | sed 's/.*update-v//; s/\.sh$//' | sort -V); do
        if version_lt "$FROM" "$script" && ! version_lt "$TO" "$script"; then
            echo "  - update-v$script.sh"
            bash "$MIGRATIONS/update-v$script.sh"
            ran=$((ran + 1))
        fi
    done
fi
[ "$ran" -eq 0 ] && echo "  - none pending"

echo "[4/4] Reinstalling..."
"$DIR/install.sh" "$@" | sed 's/^/  /'

echo "$TO" > "$STATE_FILE"
echo ""
echo "=== Updated $FROM -> $TO ==="
