#!/usr/bin/env bash
# Trim the launchd logs so they don't grow without bound.
#
# These files are truncated in place rather than rotated. launchd holds them open
# with O_APPEND, so a fresh write lands at the new end of the same inode; renaming
# the file instead would leave the proxy writing into something unlinked and
# invisible. Keeping the tail means the recent history you'd actually debug from
# survives.
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
KEEP="${KEEP_LINES:-2000}"

for name in proxy.log proxy.err.log refresh.log; do
    f="$DIR/$name"
    [ -f "$f" ] || continue
    lines=$(wc -l < "$f" | tr -d ' ')
    if [ "$lines" -gt "$KEEP" ]; then
        tmp="$f.trim"
        tail -n "$KEEP" "$f" > "$tmp"
        cat "$tmp" > "$f"   # same inode, so the open descriptors keep working
        rm -f "$tmp"
        echo "[*] Trimmed $name: $lines -> $KEEP lines"
    fi
done
