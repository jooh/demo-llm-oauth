#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
state_dir=${DEPLOY_STATE_DIR:-"$repo_root/.deploy-state"}
mkdir -p "$state_dir"
chmod 700 "$state_dir"
# Docker Desktop may present bind-mounted files with the container's UID. Keep
# this local-only directory writable by the pinned non-root Agentgateway image.
mkdir -p "$repo_root/agentgateway-data"
chmod 0777 "$repo_root/agentgateway-data"

"$repo_root/scripts/configure-entra.sh" "$@"
# shellcheck source=/dev/null
source "$state_dir/entra.env"

auth_file=${OPENCODE_AUTH_FILE:-$HOME/.pi/agent/auth.json}
command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }
[[ -r "$auth_file" ]] || { echo "OpenCode auth file not found: $auth_file" >&2; exit 1; }
opencode_key=$(jq -er '.["opencode-go"].key' "$auth_file")

secret=${WEBUI_SECRET_KEY:-$(openssl rand -hex 32)}
cat >"$repo_root/.env.local" <<EOF
ENTRA_TENANT_ID=$ENTRA_TENANT_ID
GATEWAY_APP_ID=$GATEWAY_APP_ID
WEBUI_CLIENT_ID=$WEBUI_CLIENT_ID
WEBUI_CLIENT_SECRET=$WEBUI_CLIENT_SECRET
WEBUI_SECRET_KEY=$secret
UPSTREAM_MODEL=${UPSTREAM_MODEL:-glm-5.3-flash}
UPSTREAM_BASE_URL=${UPSTREAM_BASE_URL:-https://opencode.ai/zen/go/v1}
UPSTREAM_API_KEY=$opencode_key
WEBUI_URL=http://localhost:3000
OPENID_REDIRECT_URI=http://localhost:3000/oauth/oidc/callback
EOF
chmod 600 "$repo_root/.env.local"
printf 'Created .env.local with the OpenCode Go credential and Entra settings.\n'
printf 'Run: docker compose --env-file .env.local config --quiet && docker compose --env-file .env.local up -d\n'
