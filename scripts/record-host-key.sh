#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
state_dir=${DEPLOY_STATE_DIR:-"$repo_root/.deploy-state"}
cf_state="$state_dir/cloudflare.json"
[[ -f $cf_state ]] || { echo "Run provision.sh apply first" >&2; exit 1; }
for bin in cloudflared ssh-keyscan; do command -v "$bin" >/dev/null || { echo "$bin is required" >&2; exit 1; }; done
client_id=$(jq -r .serviceTokenClientId "$cf_state")
client_secret=$(jq -r .serviceTokenClientSecret "$cf_state")
hostname=$(jq -r '.sshHostname // "ssh.johancarlin.com"' "$state_dir/infra.json")
known_hosts="$state_dir/known_hosts"
cloudflared access tcp --hostname "$hostname" --id "$client_id" --secret "$client_secret" --url 127.0.0.1:2222 >"$state_dir/cloudflared-host-key.log" 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true' EXIT
for _ in $(seq 1 20); do
  if (echo >/dev/tcp/127.0.0.1/2222) 2>/dev/null; then break; fi
  sleep 1
done
ssh-keyscan -p 2222 -t ed25519 127.0.0.1 2>/dev/null >"$known_hosts"
[[ -s $known_hosts ]] || { cat "$state_dir/cloudflared-host-key.log" >&2; exit 1; }
chmod 600 "$known_hosts"
printf 'Recorded the VM SSH host key in %s.\n' "$known_hosts"
