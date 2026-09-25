#!/usr/bin/env bash
set -euo pipefail
umask 077
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
state_dir=${DEPLOY_STATE_DIR:-$repo_root/.deploy-state}
client_id=$(jq -er .serviceTokenClientId "$state_dir/cloudflare.json")
client_secret=$(jq -er .serviceTokenClientSecret "$state_dir/cloudflare.json")
if (echo >/dev/tcp/127.0.0.1/2222) 2>/dev/null; then echo 'Local port 2222 is occupied' >&2; exit 1; fi
hostname=$(jq -er .sshHostname "$state_dir/infra.json")
cloudflared access tcp --hostname "$hostname" --id "$client_id" --secret "$client_secret" --url 127.0.0.1:2222 >"$state_dir/acceptance-tunnel.log" 2>&1 &
pid=$!
cleanup() { kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; }
trap cleanup EXIT
for _ in $(seq 1 10); do
  if (echo >/dev/tcp/127.0.0.1/2222) 2>/dev/null; then break; fi
  sleep 1
done
ssh -p 2222 -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$state_dir/known_hosts" -i "${DEPLOY_SSH_KEY_PATH:-$state_dir/deploy.key}" deploy@127.0.0.1 "$@"
