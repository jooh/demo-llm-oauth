#!/usr/bin/env bash
set -euo pipefail
umask 077
export GH_REPO=jooh/demo-llm-oauth

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
state_dir=${DEPLOY_STATE_DIR:-"$repo_root/.deploy-state"}
state_file="$state_dir/infra.json"
cf_state="$state_dir/cloudflare.json"
entra_env="$state_dir/entra.env"
[[ -f $state_file && -f $cf_state && -f $entra_env ]] || { echo "Run configure-entra.sh and provision.sh apply first" >&2; exit 1; }
for bin in gh jq openssl ssh-keygen python3; do command -v "$bin" >/dev/null || { echo "$bin is required" >&2; exit 1; }; done
# shellcheck source=/dev/null
source "$entra_env"
server_id=$(jq -er '.serverId | select(length > 0)' "$state_file")
ssh_host=$(jq -er '.sshHostname | select(length > 0)' "$state_file")
client_id=$(jq -er '.serviceTokenClientId | select(length > 0)' "$cf_state")
client_secret=$(jq -er '.serviceTokenClientSecret | select(length > 0)' "$cf_state")
private_key=${DEPLOY_SSH_KEY_PATH:-$state_dir/deploy.key}
auth_file=${OPENCODE_AUTH_FILE:-$HOME/.pi/agent/auth.json}
opencode_key=$(jq -er '.["opencode-go"].key | select(type == "string" and length > 0)' "$auth_file")
webui_secret=$(jq -er '.webuiClientSecret | select(length > 0)' "$state_dir/entra.json")
[[ -s $private_key ]] || { echo 'Deployment private key missing' >&2; exit 1; }
[[ -s "$state_dir/known_hosts" ]] || { echo "Run record-host-key.sh before configuring GitHub" >&2; exit 1; }
ssh-keygen -y -f "$private_key" >/dev/null
ssh-keygen -lf "$state_dir/known_hosts" >/dev/null
for var in ENTRA_TENANT_ID GATEWAY_APP_ID WEBUI_CLIENT_ID; do
  [[ -n ${!var:-} ]] || { echo "$var is required" >&2; exit 1; }
done
gh api repos/jooh/demo-llm-oauth --jq '.permissions.admin' | grep -qx true || { echo 'Repository admin access is required' >&2; exit 1; }
key_file="$state_dir/webui-secret-key"
if [[ -s $key_file ]]; then
  webui_key=$(<"$key_file")
  [[ -z ${WEBUI_SECRET_KEY:-} || $WEBUI_SECRET_KEY == "$webui_key" ]] || { echo 'WEBUI_SECRET_KEY differs from saved production key; rotate deliberately.' >&2; exit 1; }
else
  # Never silently replace a key whose value GitHub can no longer return.
  existing_secrets=$(gh secret list --repo jooh/demo-llm-oauth --json name --jq '.[].name')
  if grep -qx WEBUI_SECRET_KEY <<<"$existing_secrets"; then
    [[ -n ${WEBUI_SECRET_KEY:-} ]] || { echo 'Restore the existing WEBUI_SECRET_KEY to private state before rerunning.' >&2; exit 1; }
  fi
  webui_key=${WEBUI_SECRET_KEY:-$(openssl rand -hex 32)}
  printf '%s\n' "$webui_key" >"$key_file"
fi
chmod 600 "$key_file"

# Reject incomplete or conflicting production values before the first write to
# GitHub. This temporary file is private and never printed.
validation_env=$(mktemp "$state_dir/production-env-check.XXXXXX")
trap 'rm -f "$validation_env"' EXIT
ENTRA_TENANT_ID="$ENTRA_TENANT_ID" GATEWAY_APP_ID="$GATEWAY_APP_ID" \
WEBUI_CLIENT_ID="$WEBUI_CLIENT_ID" WEBUI_CLIENT_SECRET="$webui_secret" \
WEBUI_SECRET_KEY="$webui_key" UPSTREAM_API_KEY="$opencode_key" \
UPSTREAM_MODEL="${UPSTREAM_MODEL:-glm-5.3-flash}" \
UPSTREAM_BASE_URL="${UPSTREAM_BASE_URL:-https://opencode.ai/zen/go/v1}" \
WEBUI_URL="${WEBUI_URL:-https://chat.johancarlin.com}" \
OPENID_REDIRECT_URI="${OPENID_REDIRECT_URI:-https://chat.johancarlin.com/oauth/oidc/callback}" \
python3 "$repo_root/scripts/write-deploy-env.py" "$validation_env"

set_secret() { printf '%s' "$2" | gh secret set "$1" --repo jooh/demo-llm-oauth; }
set_secret DEPLOY_SSH_PRIVATE_KEY "$(<"$private_key")"
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
