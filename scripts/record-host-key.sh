#!/usr/bin/env bash
set -euo pipefail
umask 077
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
state_dir=${DEPLOY_STATE_DIR:-$repo_root/.deploy-state}
for bin in jq cloudflared ssh-keyscan ssh-keygen ssh; do command -v "$bin" >/dev/null || { echo "$bin is required" >&2; exit 1; }; done
for file in cloudflare.json infra.json known_hosts.expected; do
  [[ -s $state_dir/$file ]] || { echo "Missing $file; run provision.sh apply first" >&2; exit 1; }
done
client_id=$(jq -er '.serviceTokenClientId | select(type == "string" and length > 0)' "$state_dir/cloudflare.json")
client_secret=$(jq -er '.serviceTokenClientSecret | select(type == "string" and length > 0)' "$state_dir/cloudflare.json")
hostname=$(jq -er '.sshHostname' "$state_dir/infra.json")
private_key=${DEPLOY_SSH_KEY_PATH:-$state_dir/deploy.key}
known_hosts="$state_dir/known_hosts"
expected="$state_dir/known_hosts.expected"
scanned=$(mktemp "$state_dir/host-key-scan.XXXXXX")
pid=''
cleanup() {
  if [[ -n $pid ]]; then kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; fi
  rm -f "$scanned"
}
trap cleanup EXIT
if (echo >/dev/tcp/127.0.0.1/2222) 2>/dev/null; then
  echo 'Local port 2222 is already occupied; stop that listener before retrying.' >&2
  exit 1
fi
cloudflared access tcp --hostname "$hostname" --id "$client_id" --secret "$client_secret" --url 127.0.0.1:2222 >"$state_dir/cloudflared-host-key.log" 2>&1 &
pid=$!
for _ in $(seq 1 60); do
  kill -0 "$pid" 2>/dev/null || { echo 'Cloudflare Access tunnel exited; inspect its private log.' >&2; exit 1; }
  if ssh-keyscan -T 5 -p 2222 -t ed25519 127.0.0.1 >"$scanned" 2>/dev/null && [[ -s $scanned ]]; then break; fi
  sleep 5
done
[[ -s $scanned ]] || { echo 'SSH did not become available through Cloudflare Access.' >&2; exit 1; }
key_material() { awk '$1 !~ /^#/ && $2 == "ssh-ed25519" && NF >= 3 {print $2 " " $3}' "$1" | sort -u; }
expected_key=$(key_material "$expected")
scanned_key=$(key_material "$scanned")
[[ -n $expected_key && -n $scanned_key ]] || { echo 'No Ed25519 host key returned.' >&2; exit 1; }
[[ $expected_key == "$scanned_key" ]] || { echo 'SSH host key differs from the key supplied at provisioning. Refusing to trust it.' >&2; exit 1; }
if [[ -s $known_hosts ]]; then
  recorded_key=$(key_material "$known_hosts")
  [[ $recorded_key == "$scanned_key" ]] || { echo 'Recorded SSH host key changed. Refusing to overwrite it.' >&2; exit 1; }
fi
ssh_options=(-p 2222 -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$expected" -i "$private_key")
ssh "${ssh_options[@]}" deploy@127.0.0.1 'sudo timeout 600 cloud-init status --wait >/dev/null && sudo test -f /var/lib/llm-oauth-bootstrap-complete && sudo systemctl is-active --quiet docker cloudflared && docker compose version >/dev/null'
install -m 0600 "$expected" "$known_hosts"
ssh-keygen -lf "$known_hosts"
printf 'Verified bootstrap and SSH host key through Cloudflare Access; saved %s.\n' "$known_hosts"
