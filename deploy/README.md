# Hetzner deployment

The demo uses one CX23 (2 vCPU, 4 GB RAM, 40 GB disk), Ubuntu 24.04,
and a Hetzner firewall with no rules. This blocks all unsolicited public
inbound traffic and allows outbound connections. Cloudflare Tunnel carries
both web and SSH access. Keep the Cloudflare Access applications enabled.

`cloud-init.template.yaml` installs Docker, Compose, and a checksum-verified
Cloudflare connector. Replace the two placeholders before using it. Include
the repository's `compose.yaml`, `compose.production.yaml`, and a private
`.env` as additional cloud-init `write_files` entries under `/opt/llm-oauth`.
The `.env` must have mode `0600`. Treat the rendered cloud-init document as
a secret because it contains the connector token. Never commit it.

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

After configuring Entra and the upstream LLM in the private `.env`, start:

```bash
cd /opt/llm-oauth
docker compose -f compose.yaml -f compose.production.yaml config --quiet
docker compose -f compose.yaml -f compose.production.yaml up -d
```

Register `https://chat.johancarlin.com/oauth/oidc/callback` as the Entra Web
redirect URI. Startup is deliberately separate from provisioning so that
placeholder identities and missing model settings are not deployed as if
they were complete. See the main README for the OAuth configuration.

For teardown, delete the server and its Primary IPv4. Turning the server off
does not stop billing. Back up the `openwebui-data` volume first if you want
to retain accounts and conversations. Revoking the provisioning API token
does not terminate running resources.
