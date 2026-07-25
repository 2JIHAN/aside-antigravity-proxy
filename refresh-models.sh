#!/usr/bin/env bash
set -e

# ponytail: manual refresh script for discovery probe and aside models.json update
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
exec python3 "$DIR/refresh_models.py" "$@"
