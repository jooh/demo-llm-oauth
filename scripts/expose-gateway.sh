#!/usr/bin/env bash
set -euo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
infrastructure_env=${INFRASTRUCTURE_ENV_FILE:-$repo_root/deploy/.env.infrastructure}
if [[ -f $infrastructure_env ]]; then
  python3 - "$infrastructure_env" <<'PY'
import os, sys
if os.stat(sys.argv[1]).st_mode & 0o077:
    sys.exit('Restrict the infrastructure env file to mode 0600 before continuing')
PY
  set -a
  # shellcheck source=/dev/null
  source "$infrastructure_env"
  set +a
fi
exec python3 "$repo_root/scripts/expose-gateway.py" "${1:-plan}"
