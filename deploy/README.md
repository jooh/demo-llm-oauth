# Hetzner deployment

Run provisioning from the repository root. It is idempotent and keeps
credentials and resource ids in the ignored `.deploy-state/` directory:

```bash
cp deploy/.env.infrastructure.example deploy/.env.infrastructure
# Fill in the five values, then:
set -a; source deploy/.env.infrastructure; set +a
scripts/provision.sh plan
scripts/provision.sh apply
scripts/record-host-key.sh
scripts/configure-github.sh
```

The active `hcloud` context (or `HCLOUD_TOKEN`) needs project access. The
Cloudflare token needs account Tunnel Edit, Access Apps and Policies Edit,
Access Service Tokens Edit, Access Identity Providers Edit, Access Organizations
Read, and zone DNS Edit/Read. Do not commit the shell environment, state files,
rendered cloud-init, SSH keys, or `.env` files.

The demo uses one CX23 (2 vCPU, 4 GB RAM, 40 GB disk), Ubuntu 24.04,
and a Hetzner firewall with no rules. This blocks all unsolicited public
inbound traffic and allows outbound connections. Cloudflare Tunnel carries
both web and SSH access. Keep the Cloudflare Access applications enabled.

`cloud-init.template.yaml` installs Docker, Compose, and a checksum-verified
Cloudflare connector. The provisioning script renders its placeholders and
passes the document to Hetzner. Application files are copied later by GitHub
Actions, so first boot never depends on files that do not yet exist.

The `deploy` user has SSH key authentication and administrative access.
Public root and password login are disabled. Configure the tunnel routes:

| Hostname | Origin |
| --- | --- |
| `chat.johancarlin.com` | `http://127.0.0.1:3000` |
| `ssh.johancarlin.com` | `ssh://127.0.0.1:22` |
| All other requests | `http_status:404` |

On a client with cloudflared installed, use:

```sshconfig
Host ssh.johancarlin.com
    User deploy
    IdentityFile ~/.ssh/llm-oauth-deploy
    ProxyCommand cloudflared access ssh --hostname %h
```

Use the appropriate cloudflared and SSH key paths on that client. Cloudflare
Access authentication and a matching server-authorized SSH key are both
required. The Hetzner API token is only needed for infrastructure changes;
it is not installed on the VM.

After configuring Entra and the upstream LLM, the manual GitHub Actions
workflow starts the application:

```bash
gh workflow run Deploy --ref main
```

The workflow validates production Compose configuration, transfers the private
`.env`, pulls pinned images, and starts the stack. Startup is deliberately
separate from provisioning so placeholder identities and missing model settings
cannot be deployed as if they were complete.

For teardown, delete the server and its Primary IPv4. Turning the server off
does not stop billing. Back up the `openwebui-data` volume first if you want
to retain accounts and conversations. Revoking the provisioning API token
does not terminate running resources.
