#!/usr/bin/env bash
set -euo pipefail

usage() { cat <<'EOF'
Usage: scripts/provision.sh [plan|apply]

Environment:
  CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_ZONE_ID
  HCLOUD_TOKEN is optional when an authenticated hcloud context is active.
  CLOUDFLARE_ACCESS_EMAIL, DEPLOY_SSH_PUBLIC_KEY (optional when a key is generated)
  CLOUDFLARE_ZONE (default johancarlin.com)
EOF
}

mode=${1:-plan}
[[ $mode == plan || $mode == apply ]] || { usage >&2; exit 2; }
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
state_dir=${DEPLOY_STATE_DIR:-"$repo_root/.deploy-state"}
mkdir -p "$state_dir"
chmod 700 "$state_dir"
state_file="$state_dir/infra.json"
cf_state="$state_dir/cloudflare.json"
chat_host=${CHAT_HOSTNAME:-chat.johancarlin.com}
ssh_host=${SSH_HOSTNAME:-ssh.johancarlin.com}
tunnel_name=${CLOUDFLARE_TUNNEL_NAME:-llm-oauth}
server_name=${HCLOUD_SERVER_NAME:-llm-oauth}
firewall_name=${HCLOUD_FIREWALL_NAME:-llm-oauth}
ssh_key_name=${HCLOUD_SSH_KEY_NAME:-llm-oauth-deploy}
access_email=${CLOUDFLARE_ACCESS_EMAIL:-}

required=(CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_ZONE_ID)
for var in "${required[@]}"; do [[ -n ${!var:-} ]] || { echo "$var is required" >&2; exit 1; }; done
[[ -n $access_email ]] || { echo "CLOUDFLARE_ACCESS_EMAIL is required" >&2; exit 1; }
for bin in curl jq hcloud openssl ssh-keygen; do command -v "$bin" >/dev/null || { echo "$bin is required" >&2; exit 1; }; done

hcloud_cmd() {
  if [[ -n ${HCLOUD_TOKEN:-} ]]; then
    HCLOUD_TOKEN="$HCLOUD_TOKEN" hcloud "$@"
  else
    hcloud "$@"
  fi
}

cf_api() {
  local method=$1 path=$2 body=${3:-}
  if [[ -n $body ]]; then
    curl --fail --silent --show-error --request "$method" \
      "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID$path" \
      --header "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
      --header 'Content-Type: application/json' --data "$body"
  else
    curl --fail --silent --show-error --request "$method" \
      "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID$path" \
      --header "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
      --header 'Content-Type: application/json'
  fi
}

if [[ -f $cf_state ]]; then
  tunnel_id=$(jq -r '.tunnelId // empty' "$cf_state")
  tunnel_token=$(jq -r '.tunnelToken // empty' "$cf_state")
  service_token_id=$(jq -r '.serviceTokenId // empty' "$cf_state")
  service_token_client_id=$(jq -r '.serviceTokenClientId // empty' "$cf_state")
  service_token_client_secret=$(jq -r '.serviceTokenClientSecret // empty' "$cf_state")
else
  tunnel_id=''; tunnel_token=''; service_token_id=''; service_token_client_id=''; service_token_client_secret=''
fi

save_cf_state() {
  jq -n --arg tid "$tunnel_id" --arg tt "$tunnel_token" --arg st "$service_token_id" --arg cid "$service_token_client_id" --arg cs "$service_token_client_secret" '{tunnelId:$tid,tunnelToken:$tt,serviceTokenId:$st,serviceTokenClientId:$cid,serviceTokenClientSecret:$cs}' >"$cf_state"
  chmod 600 "$cf_state"
}

if [[ -z $tunnel_id ]]; then
  tunnels=$(cf_api GET /cfd_tunnel)
  tunnel_id=$(jq -r --arg n "$tunnel_name" '.result[] | select(.name==$n) | .id' <<<"$tunnels" | head -n1 || true)
fi
if [[ -z $tunnel_id && $mode == apply ]]; then
  tunnel_secret=$(openssl rand -base64 32 | tr -d '\n')
  created=$(cf_api POST /cfd_tunnel "$(jq -nc --arg n "$tunnel_name" --arg s "$tunnel_secret" '{name:$n,config_src:"cloudflare",tunnel_secret:$s}')")
  tunnel_id=$(jq -r '.result.id' <<<"$created")
  tunnel_token=$(cf_api GET "/cfd_tunnel/$tunnel_id/token" | jq -r '.result')
  save_cf_state
fi
if [[ -n $tunnel_id && -z $tunnel_token && $mode == apply ]]; then
  tunnel_token=$(cf_api GET "/cfd_tunnel/$tunnel_id/token" | jq -r '.result')
  save_cf_state
fi

if [[ -n $tunnel_id && $mode == apply ]]; then
  cf_api PUT "/cfd_tunnel/$tunnel_id/configurations" "$(jq -nc --arg chat "$chat_host" --arg ssh "$ssh_host" '{config:{ingress:[{hostname:$chat,service:"http://127.0.0.1:3000"},{hostname:$ssh,service:"ssh://127.0.0.1:22"},{service:"http_status:404"}]}}')" >/dev/null
  for hostname in "$chat_host" "$ssh_host"; do
    records=$(curl --fail --silent --show-error --request GET "https://api.cloudflare.com/client/v4/zones/$CLOUDFLARE_ZONE_ID/dns_records?type=CNAME&name=$hostname" --header "Authorization: Bearer $CLOUDFLARE_API_TOKEN")
    record_id=$(jq -r '.result[0].id // empty' <<<"$records")
    record_body=$(jq -nc --arg n "$hostname" --arg c "$tunnel_id.cfargotunnel.com" '{type:"CNAME",name:$n,content:$c,proxied:true,ttl:1}')
    if [[ -n $record_id ]]; then
      curl --fail --silent --show-error --request PUT "https://api.cloudflare.com/client/v4/zones/$CLOUDFLARE_ZONE_ID/dns_records/$record_id" --header "Authorization: Bearer $CLOUDFLARE_API_TOKEN" --header 'Content-Type: application/json' --data "$record_body" >/dev/null
    else
      curl --fail --silent --show-error --request POST "https://api.cloudflare.com/client/v4/zones/$CLOUDFLARE_ZONE_ID/dns_records" --header "Authorization: Bearer $CLOUDFLARE_API_TOKEN" --header 'Content-Type: application/json' --data "$record_body" >/dev/null
    fi
  done
fi

access_apps=$(cf_api GET /access/apps)
ssh_app_id=$(jq -r --arg h "$ssh_host" '.result[] | select(.domain==$h) | .id' <<<"$access_apps" | head -n1 || true)
if [[ -z $ssh_app_id && $mode == apply ]]; then
  ssh_app_id=$(cf_api POST /access/apps "$(jq -nc --arg n "llm-oauth SSH" --arg h "$ssh_host" '{name:$n,domain:$h,type:"self_hosted",session_duration:"24h"}')" | jq -r '.result.id')
fi

if [[ -z $service_token_id ]]; then
  tokens=$(cf_api GET /access/service_tokens)
  service_token_id=$(jq -r --arg n 'llm-oauth-github-actions' '.result[] | select(.name==$n) | .id' <<<"$tokens" | head -n1 || true)
fi
if [[ -z $service_token_id && $mode == apply ]]; then
  token_json=$(cf_api POST /access/service_tokens "$(jq -nc '{name:"llm-oauth-github-actions",duration:"8760h"}')")
  service_token_id=$(jq -r '.result.id' <<<"$token_json")
  service_token_client_id=$(jq -r '.result.client_id' <<<"$token_json")
  service_token_client_secret=$(jq -r '.result.client_secret' <<<"$token_json")
  save_cf_state
fi
if [[ $mode == apply && -n $service_token_id && ( -z $service_token_client_id || -z $service_token_client_secret ) ]]; then
  echo "The existing Cloudflare service token is not in local state, so its secret cannot be recovered. Supply its credentials in .deploy-state/cloudflare.json or rotate it deliberately before retrying." >&2
  exit 1
fi

if [[ -n $ssh_app_id && $mode == apply ]]; then
  policies=$(cf_api GET "/access/apps/$ssh_app_id/policies")
  if ! jq -e --arg e "$access_email" '.result[] | select(.decision=="allow" and (.include[]?.email.email? == $e))' <<<"$policies" >/dev/null; then
    cf_api POST "/access/apps/$ssh_app_id/policies" "$(jq -nc --arg e "$access_email" '{name:"interactive-user",decision:"allow",precedence:1,include:[{email:{email:$e}}]}')" >/dev/null
  fi
  if [[ -n $service_token_id ]] && ! jq -e --arg id "$service_token_id" '.result[] | select(.decision=="service_auth" and (.include[]?.service_token.token_id? == $id))' <<<"$policies" >/dev/null; then
    cf_api POST "/access/apps/$ssh_app_id/policies" "$(jq -nc --arg id "$service_token_id" '{name:"github-actions",decision:"service_auth",precedence:2,include:[{service_token:{token_id:$id}}]}')" >/dev/null
  fi
fi

if [[ $mode == plan ]]; then
  printf 'Plan: reconcile Cloudflare tunnel/DNS/SSH Access and a Hetzner CX23 in hel1.\n'
  printf 'Plan: no resources will be changed.\n'
  exit 0
fi

ssh_private_key=${DEPLOY_SSH_KEY_PATH:-$state_dir/deploy.key}
if [[ -z ${DEPLOY_SSH_PUBLIC_KEY:-} && ! -f $ssh_private_key ]]; then
  ssh-keygen -t ed25519 -N '' -C llm-oauth-deploy -f "$ssh_private_key" >/dev/null
fi
ssh_public_key=${DEPLOY_SSH_PUBLIC_KEY:-$(<"$ssh_private_key.pub")}

ssh_keys=$(hcloud_cmd ssh-key list -o json)
ssh_key_id=$(jq -r --arg n "$ssh_key_name" '.[] | select(.name==$n) | .id' <<<"$ssh_keys" | head -n1 || true)
if [[ -z $ssh_key_id ]]; then
  ssh_key_id=$(hcloud_cmd ssh-key create --name "$ssh_key_name" --public-key "$ssh_public_key" --label owner=llm-oauth -o json | jq -r '.ssh_key.id')
fi

rules_file=$(mktemp)
trap 'rm -f "$rules_file"' EXIT
printf '{"rules":[]}' >"$rules_file"
firewalls=$(hcloud_cmd firewall list -o json)
firewall_id=$(jq -r --arg n "$firewall_name" '.[] | select(.name==$n) | .id' <<<"$firewalls" | head -n1 || true)
if [[ -z $firewall_id ]]; then
  firewall_id=$(hcloud_cmd firewall create --name "$firewall_name" --label owner=llm-oauth --rules-file "$rules_file" -o json | jq -r '.firewall.id')
else
  hcloud_cmd firewall replace-rules --rules-file "$rules_file" "$firewall_id" >/dev/null
fi

servers=$(hcloud_cmd server list -o json)
server_id=$(jq -r --arg n "$server_name" '.[] | select(.name==$n) | .id' <<<"$servers" | head -n1 || true)
if [[ -z $server_id ]]; then
  [[ -n $tunnel_token ]] || { echo "Tunnel token missing; rerun with apply after Cloudflare creation" >&2; exit 1; }
  rendered="$state_dir/cloud-init.rendered.yaml"
  sed -e "s|REPLACE_WITH_DEPLOY_SSH_PUBLIC_KEY|$ssh_public_key|g" -e "s|REPLACE_WITH_CLOUDFLARED_TUNNEL_TOKEN|$tunnel_token|g" "$repo_root/deploy/cloud-init.template.yaml" >"$rendered"
  chmod 600 "$rendered"
  server_id=$(hcloud_cmd server create --name "$server_name" --type cx23 --image ubuntu-24.04 --location hel1 --ssh-key "$ssh_key_id" --firewall "$firewall_id" --label owner=llm-oauth --user-data-from-file "$rendered" -o json | jq -r '.server.id')
else
  server_json=$(jq -c --argjson id "$server_id" '.[] | select(.id==$id)' <<<"$servers")
  server_type=$(jq -r '.server_type.name' <<<"$server_json")
  server_location=$(jq -r '.datacenter.location.name' <<<"$server_json")
  [[ $server_type == cx23 && $server_location == hel1 ]] || { echo "Existing $server_name is $server_type in $server_location; refusing to change it" >&2; exit 1; }
  hcloud_cmd firewall apply-to-resource --type server --server "$server_id" "$firewall_id" >/dev/null
fi

jq -n --arg sid "$server_id" --arg fid "$firewall_id" --arg kid "$ssh_key_id" --arg host "$ssh_host" '{serverId:$sid,firewallId:$fid,sshKeyId:$kid,sshHostname:$host}' >"$state_file"
chmod 600 "$state_file"
save_cf_state
printf 'Provisioned/reconciled infrastructure. Server id: %s\n' "$server_id"
printf 'Keep %s private; it contains the tunnel and service-token credentials.\n' "$state_dir"
