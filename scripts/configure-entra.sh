#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/configure-entra.sh [--production-url URL]

Reconciles the LLM Gateway and Open WebUI registrations in the selected
Entra tenant. Object ids and the client secret are stored in .deploy-state/.
EOF
}

production_url=${PRODUCTION_WEBUI_URL:-https://chat.johancarlin.com}
rotate_secret=${ROTATE_WEBUI_SECRET:-0}
while (($#)); do
  case "$1" in
    --production-url) production_url=${2:?missing URL}; shift 2 ;;
    --rotate-secret) rotate_secret=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

command -v az >/dev/null || { echo "az is required" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }
command -v uuidgen >/dev/null || { echo "uuidgen is required" >&2; exit 1; }

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
state_dir=${DEPLOY_STATE_DIR:-"$repo_root/.deploy-state"}
mkdir -p "$state_dir"
chmod 700 "$state_dir"
state_file="$state_dir/entra.json"
env_file="$state_dir/entra.env"

graph() { az rest --only-show-errors "$@"; }

tenant_id=${ENTRA_TENANT_ID:-$(az account show --query tenantId -o tsv)}
user_json=$(az ad signed-in-user show -o json)
user_id=$(jq -r '.id' <<<"$user_json")
user_email=${ENTRA_ASSIGNMENT_EMAIL:-$(jq -r '.mail // .userPrincipalName' <<<"$user_json")}

app_by_name() {
  local name=$1
  graph --method GET --url "https://graph.microsoft.com/v1.0/applications?\$filter=displayName%20eq%20'${name// /%20}'&\$select=id,appId,api,web,appRoles,optionalClaims,requiredResourceAccess" |
    jq -c '.value[0] // empty'
}

create_app() {
  local body=$1
  graph --method POST --url https://graph.microsoft.com/v1.0/applications --body "$body" | jq -c .
}

gateway=$(app_by_name 'LLM Gateway')
if [[ -z $gateway ]]; then
  gateway=$(create_app "$(jq -nc '{displayName:"LLM Gateway",signInAudience:"AzureADMyOrg",api:{requestedAccessTokenVersion:2}}')")
fi
gateway_object_id=$(jq -r '.id' <<<"$gateway")
gateway_app_id=$(jq -r '.appId' <<<"$gateway")

scope_id=$(jq -r '.api.oauth2PermissionScopes[]? | select(.value=="llm.invoke") | .id' <<<"$gateway" | head -n1)
scope_id=${scope_id:-$(uuidgen | tr '[:upper:]' '[:lower:]')}
gateway_scopes=$(jq -c --arg id "$scope_id" '(.api.oauth2PermissionScopes // []) | if any(.[]; .value=="llm.invoke") then . else . + [{id:$id,adminConsentDescription:"Allow Open WebUI to invoke the LLM gateway",adminConsentDisplayName:"Invoke the LLM gateway",isEnabled:true,type:"Admin",value:"llm.invoke",userConsentDescription:"Allow Open WebUI to invoke the LLM gateway",userConsentDisplayName:"Invoke the LLM gateway"}] end' <<<"$gateway")
gateway_optional_claims=$(jq -c '(.optionalClaims // {}) | .accessToken = (((.accessToken // []) | map(select(.name != "email"))) + [{name:"email",essential:false,additionalProperties:[]}])' <<<"$gateway")
graph --method PATCH --url "https://graph.microsoft.com/v1.0/applications/$gateway_object_id" --body "$(jq -nc --arg uri "api://$gateway_app_id" --argjson scopes "$gateway_scopes" --argjson optionalClaims "$gateway_optional_claims" '{identifierUris:[$uri],api:{requestedAccessTokenVersion:2,oauth2PermissionScopes:$scopes},optionalClaims:$optionalClaims}')" >/dev/null

gateway_sp=$(graph --method GET --url "https://graph.microsoft.com/v1.0/servicePrincipals?\$filter=appId%20eq%20'$gateway_app_id'&\$select=id" | jq -r '.value[0].id // empty')
if [[ -z $gateway_sp ]]; then
  gateway_sp=$(graph --method POST --url https://graph.microsoft.com/v1.0/servicePrincipals --body "$(jq -nc --arg a "$gateway_app_id" '{appId:$a}')" | jq -r '.id')
fi

web=$(app_by_name 'Open WebUI')
if [[ -z $web ]]; then
  web=$(create_app "$(jq -nc '{displayName:"Open WebUI",signInAudience:"AzureADMyOrg",web:{redirectUris:[]}}')")
fi
web_object_id=$(jq -r '.id' <<<"$web")
web_app_id=$(jq -r '.appId' <<<"$web")
web_redirects=$(jq -nc --arg local 'http://localhost:3000/oauth/oidc/callback' --arg prod "${production_url%/}/oauth/oidc/callback" '[ $local, $prod ] | unique')
role_id=$(jq -r '.appRoles[]? | select(.value=="user") | .id' <<<"$web" | head -n1)
role_id=${role_id:-$(uuidgen | tr '[:upper:]' '[:lower:]')}
web_roles=$(jq -c --arg id "$role_id" '(.appRoles // []) | if any(.[]; .value=="user") then . else . + [{allowedMemberTypes:["User"],description:"Assigned Open WebUI user",displayName:"User",id:$id,isEnabled:true,value:"user"}] end' <<<"$web")
graph --method PATCH --url "https://graph.microsoft.com/v1.0/applications/$web_object_id" --body "$(jq -nc --argjson redirects "$web_redirects" --argjson roles "$web_roles" '{web:{redirectUris:$redirects,implicitGrantSettings:{enableAccessTokenIssuance:false,enableIdTokenIssuance:false}},appRoles:$roles,optionalClaims:{idToken:[{name:"email",essential:false,additionalProperties:[]}]}}')" >/dev/null

web_sp=$(graph --method GET --url "https://graph.microsoft.com/v1.0/servicePrincipals?\$filter=appId%20eq%20'$web_app_id'&\$select=id" | jq -r '.value[0].id // empty')
if [[ -z $web_sp ]]; then
  web_sp=$(graph --method POST --url https://graph.microsoft.com/v1.0/servicePrincipals --body "$(jq -nc --arg a "$web_app_id" '{appId:$a}')" | jq -r '.id')
fi
graph --method PATCH --url "https://graph.microsoft.com/v1.0/servicePrincipals/$web_sp" --body '{"appRoleAssignmentRequired":true}' >/dev/null

required=$(jq -nc --arg rid "$gateway_app_id" --arg sid "$scope_id" '[{resourceAppId:$rid,resourceAccess:[{id:$sid,type:"Scope"}]}]')
graph --method PATCH --url "https://graph.microsoft.com/v1.0/applications/$web_object_id" --body "$(jq -nc --argjson r "$required" '{requiredResourceAccess:$r}')" >/dev/null

secret=''
secret_key_id=''
if [[ -f "$state_file" ]]; then
  secret=$(jq -r '.webuiClientSecret // empty' "$state_file")
  secret_key_id=$(jq -r '.webuiClientSecretKeyId // empty' "$state_file")
fi
if [[ $rotate_secret == 1 ]]; then
  credentials=$(graph --method GET --url "https://graph.microsoft.com/v1.0/applications/$web_object_id?\$select=passwordCredentials" | jq -c '.passwordCredentials // []')
  while read -r old_key; do
    [[ -n $old_key ]] || continue
    graph --method POST --url "https://graph.microsoft.com/v1.0/applications/$web_object_id/removePassword" --body "$(jq -nc --arg k "$old_key" '{keyId:$k}')" >/dev/null
  done < <(jq -r '.[].keyId' <<<"$credentials")
  secret=''
fi
if [[ -z $secret ]]; then
  secret_json=$(graph --method POST --url "https://graph.microsoft.com/v1.0/applications/$web_object_id/addPassword" --body "$(jq -nc '{passwordCredential:{displayName:"llm-oauth-managed",endDateTime:((now+63072000)|strftime("%Y-%m-%dT%H:%M:%SZ"))}}')")
  secret=$(jq -r '.secretText' <<<"$secret_json")
  secret_key_id=$(jq -r '.keyId' <<<"$secret_json")
fi

grant=$(graph --method GET --url "https://graph.microsoft.com/v1.0/oauth2PermissionGrants?\$filter=clientId%20eq%20'$web_sp'%20and%20resourceId%20eq%20'$gateway_sp'" | jq -c '.value[0] // empty')
if [[ -z $grant ]]; then
  graph --method POST --url https://graph.microsoft.com/v1.0/oauth2PermissionGrants --body "$(jq -nc --arg c "$web_sp" --arg r "$gateway_sp" '{clientId:$c,consentType:"AllPrincipals",resourceId:$r,scope:"llm.invoke"}')" >/dev/null
elif [[ $(jq -r '.scope' <<<"$grant") != *llm.invoke* ]]; then
  grant_id=$(jq -r '.id' <<<"$grant")
  graph --method PATCH --url "https://graph.microsoft.com/v1.0/oauth2PermissionGrants/$grant_id" --body "$(jq -nc --arg s "$(jq -r '.scope' <<<"$grant") llm.invoke" '{scope:$s}')" >/dev/null
fi
assignment=$(graph --method GET --url "https://graph.microsoft.com/v1.0/users/$user_id/appRoleAssignments?\$filter=resourceId%20eq%20$web_sp" | jq -r '.value[]?.appRoleId | select(.=="'"$role_id"'")')
if [[ -z $assignment ]]; then
  graph --method POST --url "https://graph.microsoft.com/v1.0/servicePrincipals/$web_sp/appRoleAssignedTo" --body "$(jq -nc --arg p "$user_id" --arg r "$web_sp" --arg a "$role_id" '{principalId:$p,resourceId:$r,appRoleId:$a}')" >/dev/null
fi

jq -n --arg tenant "$tenant_id" --arg go "$gateway_app_id" --arg wo "$web_app_id" --arg so "$scope_id" --arg ro "$role_id" --arg ws "$web_sp" --arg gs "$gateway_sp" --arg secret "$secret" --arg key "$secret_key_id" --arg email "$user_email" '{tenantId:$tenant,gatewayAppId:$go,webuiClientId:$wo,scopeId:$so,webuiRoleId:$ro,webuiServicePrincipalId:$ws,gatewayServicePrincipalId:$gs,webuiClientSecret:$secret,webuiClientSecretKeyId:$key,assignmentEmail:$email}' >"$state_file"
chmod 600 "$state_file"
{
  printf 'ENTRA_TENANT_ID=%s\n' "$tenant_id"
  printf 'GATEWAY_APP_ID=%s\n' "$gateway_app_id"
  printf 'WEBUI_CLIENT_ID=%s\n' "$web_app_id"
  printf 'WEBUI_CLIENT_SECRET=%s\n' "$secret"
} >"$env_file"
chmod 600 "$env_file"
printf 'Entra registrations reconciled for tenant %s.\n' "$tenant_id"
printf 'Open WebUI client id: %s\n' "$web_app_id"
printf 'Assigned identity: %s\n' "$user_email"
