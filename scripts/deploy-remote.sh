#!/usr/bin/env bash
# Run on the VM via SSH after transferring the bundle to a private staging dir.
set -euo pipefail
umask 077
stage=${1:?staging directory required}
commit=${2:?deployed commit required}
release_id=${3:?unique release id required}
root=${DEPLOY_ROOT:-/opt/llm-oauth}
[[ $commit =~ ^[0-9a-f]{40}$ && $release_id =~ ^[0-9]+-[0-9]+$ ]] || { echo 'Invalid release identity' >&2; exit 1; }
[[ $stage == /tmp/llm-oauth-deploy.* && -d $stage && ! -L $stage ]] || { echo 'Invalid staging directory' >&2; exit 1; }
backup="$root/releases/$release_id-previous"
activated=0
had_previous=0
compose() { docker compose --project-directory "$root" --env-file "$1/.env" -f "$1/compose.yaml" -f "$1/compose.production.yaml" "${@:2}"; }
cleanup() {
  result=$?
  trap - EXIT
  if ((result != 0 && activated == 1 && had_previous == 1)); then
    echo 'Deployment failed; restoring the previous configuration.' >&2
    if cp "$backup/compose.yaml" "$backup/compose.production.yaml" "$backup/.env" "$root/"; then
      if [[ -f $backup/revision ]]; then cp "$backup/revision" "$root/revision"; else rm -f "$root/revision"; fi
      if ! compose "$root" up -d --remove-orphans --wait --wait-timeout 300; then
        echo 'Rollback health check failed; manual recovery required.' >&2
      fi
    else
      echo 'Could not restore the previous files; manual recovery required.' >&2
    fi
  fi
  rm -rf -- "$stage"
  exit "$result"
}
trap cleanup EXIT
[[ -d $root/agentgateway-data ]] || { echo 'Bootstrap data directory missing' >&2; exit 1; }
for file in compose.yaml compose.production.yaml .env; do
  [[ -s $stage/$file ]] || { echo "Staged $file missing" >&2; exit 1; }
done
# Validate and pull without replacing the active files or changing bind paths.
compose "$stage" config --quiet
compose "$stage" pull
if [[ -f $root/compose.yaml ]]; then
  [[ -s $root/compose.production.yaml && -s $root/.env ]] || { echo 'Active configuration incomplete; inspect before deploying.' >&2; exit 1; }
  [[ ! -e $backup ]] || { echo 'Release backup already exists; use a new release id.' >&2; exit 1; }
  mkdir -p "$backup"
  chmod 700 "$backup"
  cp -p "$root/compose.yaml" "$root/compose.production.yaml" "$root/.env" "$backup/"
  if [[ -f $root/revision ]]; then cp "$root/revision" "$backup/"; fi
  had_previous=1
fi
activated=1
install -m 0600 "$stage/.env" "$root/.env"
install -m 0644 "$stage/compose.yaml" "$root/compose.yaml"
install -m 0644 "$stage/compose.production.yaml" "$root/compose.production.yaml"
compose "$root" up -d --remove-orphans --wait --wait-timeout 300
curl --fail --silent --show-error --max-time 15 http://127.0.0.1:3000/health >/dev/null
status=$(curl --silent --show-error --max-time 15 --output /dev/null --write-out '%{http_code}' http://127.0.0.1:4000/v1/models)
[[ $status == 401 ]] || { echo "Gateway missing-token check returned $status, expected 401" >&2; exit 1; }
printf '%s\n' "$commit" >"$root/revision"
compose "$root" ps
printf 'Deployed revision %s; release %s.\n' "$commit" "$release_id"
