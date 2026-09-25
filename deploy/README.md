# Production deployment

The existing Cloudflare tunnel is `demo-llm-oauth`, ID
`7f74c6f3-49cb-4195-96e5-dcbd3a861b87`. Both hostnames already have proxied DNS,
published routes, Access applications and email allow policies. OTP is enabled.
The provisioner adopts and validates these resources. It preserves unrelated
routes and existing policies, and stops on conflicts instead of replacing them.

## Prerequisites

Local tools: Python 3 (standard library only), hcloud, cloudflared, jq, gh,
OpenSSH, and openssl. Use an authenticated hcloud context or `HCLOUD_TOKEN`.

Create a custom Cloudflare API token limited to this account and
`johancarlin.com`, with account Cloudflare Tunnel Edit, Access Apps and Policies
Edit, Access Service Tokens Edit, Access Organizations/Identity Providers/Groups
Read, and zone Zone Read + DNS Read. Tunnel Edit is required to read its connector
token even though the tunnel configuration is preserved.

```bash
cp deploy/.env.infrastructure.example deploy/.env.infrastructure
chmod 600 deploy/.env.infrastructure
# Fill the private file with the token, account/zone IDs and allowed email.
# provision.sh sources this file automatically; use shell quoting for values.
scripts/provision.sh plan
```

No resources, keys or state files change in plan mode. The plan checks both
providers, existing configuration, local credentials, resource ownership, and
VM capacity. Defaults are CPX22/hel1; only change `HCLOUD_SERVER_TYPE` or
`HCLOUD_LOCATION` after explicitly deciding to use a different VM.
The bootstrap currently requires an x86 server because cloudflared is amd64.

## Provision and verify

```bash
scripts/provision.sh apply
scripts/record-host-key.sh
scripts/provision.sh plan
```

Apply adds the Actions service token/SSH Service Auth policy, dedicated SSH key,
empty-inbound firewall, and Ubuntu 24.04 VM as needed. Token credentials are
saved immediately after creation so retrying a later failed step reuses them.
If an existing service token has no matching local secret, restore private state
or arrange deliberate rotation; no automatic replacement is performed.

Cloud-init installs Docker, Compose, swap, SSH hardening and a checksum-verified
cloudflared connector. It creates `/opt/llm-oauth` and the gateway data directory,
but starts no application containers. Its completion marker is
`/var/lib/llm-oauth-bootstrap-complete`.

Provisioning generates the VM's Ed25519 host key locally and supplies it through
cloud-init. `record-host-key.sh` compares the presented key with that expected
key, authenticates SSH, and checks bootstrap completion before saving
`known_hosts`. Reruns refuse changed keys. Port 2222 must be free locally.

Keep `.deploy-state/` (including both private keys and rendered cloud-init),
`.env` files and infrastructure credentials private. Back up this state securely;
losing it can prevent reuse of existing service credentials and host identity.

Expected routes:

| Hostname | Origin on VM |
| --- | --- |
| `chat.johancarlin.com` | `http://127.0.0.1:3000` |
| `ssh.johancarlin.com` | `ssh://127.0.0.1:22` |

The firewall has no inbound rules. Check public IPv4 and IPv6 independently for
blocked ports 22, 3000, 4000 and 4001. Verify interactive email/OTP SSH as well as
service-token SSH. Neither a DNS response nor a local cloudflared listener alone
proves end-to-end access.

## Configure GitHub and deploy

```bash
scripts/configure-github.sh
gh secret list
gh variable list
# After review and merge to main:
gh workflow run Deploy --ref main -f revision=main
```

The upstream key comes from `~/.pi/agent/auth.json`. The production WebUI session
key is generated once in `.deploy-state/webui-secret-key` and reused. If GitHub
already has that secret but local state is lost, restore the existing value;
the script refuses an implicit rotation. Local conversations are not migrated.

Deployment validates and pulls the staged configuration before replacing active
files. It records the checked-out SHA, snapshots the prior configuration under
`/opt/llm-oauth/releases/<run-id>-<attempt>-previous`, starts with bounded Compose
health checks, verifies WebUI `/health` and gateway missing-token rejection, and
cleans up staging credentials. Failed activation restores the previous
configuration and attempts to start it; the workflow remains failed even if
rollback succeeds. Data volumes and the gateway data directory remain in place.

A public HTTP check of chat is expected to reach Cloudflare Access. Application
health is therefore checked on VM loopback. Browser acceptance includes
Cloudflare email/OTP followed by Entra authentication. Reach the gateway UI via
an authorized SSH local forward to VM port 4001, never a public tunnel route.

## Recovery and data backups

For a manual configuration rollback, connect through Access, inspect the desired
`releases/<run-id>-<attempt>-previous` snapshot, and restore its `compose.yaml`,
`compose.production.yaml`, `.env` and, if present, `revision` into `/opt/llm-oauth`.
Then run from that directory:

```bash
docker compose --env-file .env -f compose.yaml -f compose.production.yaml config --quiet
docker compose --env-file .env -f compose.yaml -f compose.production.yaml up -d --wait --wait-timeout 300
```

Configuration snapshots do not back up databases or undo database migrations.
Before destructive changes or application upgrades, stop the application
containers briefly and take consistent backups of both the
`llm-oauth_openwebui-data` Docker volume and `/opt/llm-oauth/agentgateway-data`,
plus the active configuration and stable session key. Store backups privately
off the VM. Test restoration in an isolated environment before relying on them.
Do not use `docker compose down -v` during deployment or rollback.

For teardown, remember to delete the VM's Primary IPv4 as well as the VM;
powering off does not stop billing. Do not tear down before verifying backups.

## Validation

```bash
shellcheck scripts/*.sh
python3 -m unittest discover -s tests -v
```

Tests exercise adoption, repeat apply, partial failures, conflicts, staged
validation, rollback, data preservation and real Compose value escaping.
Production login, refresh, session headers and live usage attribution remain
manual acceptance checks in `PLAN.md`.
