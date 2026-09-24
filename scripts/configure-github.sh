#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
state_dir=${DEPLOY_STATE_DIR:-"$repo_root/.deploy-state"}
state_file="$state_dir/infra.json"
cf_state="$state_dir/cloudflare.json"
entra_env="$state_dir/entra.env"
[[ -f $state_file && -f $cf_state && -f $entra_env ]] || { echo "Run configure-entra.sh and provision.sh apply first" >&2; exit 1; }
command -v gh >/dev/null || { echo "gh is required" >&2; exit 1; }
# shellcheck source=/dev/null
source "$entra_env"
server_id=$(jq -r .serverId "$state_file")
ssh_host=$(jq -r .sshHostname "$state_file")
client_id=$(jq -r .serviceTokenClientId "$cf_state")
client_secret=$(jq -r .serviceTokenClientSecret "$cf_state")
private_key=${DEPLOY_SSH_KEY_PATH:-$state_dir/deploy.key}
auth_file=${OPENCODE_AUTH_FILE:-$HOME/.pi/agent/auth.json}
opencode_key=$(jq -er '.["opencode-go"].key' "$auth_file")
webui_secret=$(jq -r '.webuiClientSecret' "$state_dir/entra.json")
webui_key=${WEBUI_SECRET_KEY:-$(openssl rand -hex 32)}

set_secret() { printf '%s' "$2" | gh secret set "$1"; }
set_secret DEPLOY_SSH_PRIVATE_KEY "$(<"$private_key")"
[[ -s "$state_dir/known_hosts" ]] || { echo "Run record-host-key.sh before configuring GitHub" >&2; exit 1; }
set_secret DEPLOY_SSH_HOST_KEY "$(<"$state_dir/known_hosts")"
set_secret CLOUDFLARE_ACCESS_CLIENT_ID "$client_id"
set_secret CLOUDFLARE_ACCESS_CLIENT_SECRET "$client_secret"
set_secret ENTRA_TENANT_ID "$ENTRA_TENANT_ID"
set_secret GATEWAY_APP_ID "$GATEWAY_APP_ID"
set_secret WEBUI_CLIENT_ID "$WEBUI_CLIENT_ID"
set_secret WEBUI_CLIENT_SECRET "$webui_secret"
set_secret WEBUI_SECRET_KEY "$webui_key"
set_secret UPSTREAM_API_KEY "$opencode_key"
gh variable set DEPLOY_HOST --body "$ssh_host"
gh variable set SERVER_ID --body "$server_id"
gh variable set UPSTREAM_MODEL --body "${UPSTREAM_MODEL:-glm-5.3-flash}"
gh variable set UPSTREAM_BASE_URL --body "${UPSTREAM_BASE_URL:-https://opencode.ai/zen/go/v1}"
gh variable set WEBUI_URL --body "${WEBUI_URL:-https://chat.johancarlin.com}"
gh variable set OPENID_REDIRECT_URI --body "${OPENID_REDIRECT_URI:-https://chat.johancarlin.com/oauth/oidc/callback}"
printf 'GitHub Actions secrets and variables configured.\n'
