# Migrations

`update.sh` runs the scripts in here when someone upgrades past the version they name.

Most releases don't need one. `install.sh` rewrites the launchd agent and the Aside
provider entry from scratch on every run, so re-running it — which `update.sh` does
anyway — already covers new code, renamed ports, and changed model lists. Write a
migration only for a one-time change `install.sh` won't make on its own: removing a
file an old version left behind, renaming a key inside someone's config, undoing a
setting a previous release wrote.

## Naming

```
migrations/update-v<version>.sh
```

`update.sh` runs a script when the installed version is below `<version>` and the
version being installed is at or above it, in ascending version order. Someone
jumping from 1.0.0 straight to 1.0.3 gets 1.0.1, 1.0.2, and 1.0.3 in that order.

## Writing one

Assume it may run on a machine that skipped several releases, and assume someone
will run it twice. Check before you change anything.

```bash
#!/usr/bin/env bash
set -e

# 1.0.4 renamed the provider key. Drop the old one if it's still there.
python3 - <<'EOF'
import json, os
for u in ('0', '1'):
    path = os.path.expanduser(f'~/.aside/u/{u}/models.json')
    if not os.path.isfile(path):
        continue
    data = json.load(open(path))
    if data.get('providers', {}).pop('old-key', None) is not None:
        json.dump(data, open(path, 'w'), indent=2, ensure_ascii=False)
        print(f"  removed old-key from {path}")
EOF
```

Bump `VERSION` in the same commit, or the migration never runs.
