# Deployment plan

## Current state

- [x] Entra registrations reconciled for the current tenant: the gateway API
      and Open WebUI client, delegated `llm.invoke`, v2 access tokens, admin
      consent, production and localhost callbacks, email claims, and the
      current user assignment.
- [x] Local Open WebUI sign-in and OpenCode Go chat validated with the pinned
      images.
- [x] Agentgateway validates issuer, audience, expiry, and `llm.invoke` on
      every request.
- [x] Agentgateway request-log SQLite storage is persistent locally. New
      records include Entra tenant ID, object ID, and verified email when the
      access token contains it; the UI user label falls back to object ID and
      then subject ID.
- [x] Local request logs show token usage and user attribution at
      `http://localhost:4001/ui/llm/logs`.
- [x] Existing Cloudflare configuration confirmed by the account owner: proxied
      chat and SSH DNS, tunnel `demo-llm-oauth`
      (`7f74c6f3-49cb-4195-96e5-dcbd3a861b87`), the expected localhost routes,
      Access applications for both hostnames, email allow policies, and OTP.
- [x] Provisioner adopts those resources and preserves their configuration;
      plan validates resources before mutations, and deployment scripts include
      persistent credentials, pinned host-key verification, health checks and
      configuration rollback. Regression tests cover retries and failures.
- [x] Cloudflare API credentials configured locally (mode 0600) and read access verified.
- [x] Hetzner VM `llm-oauth` (167414166), CPX22 in hel1, created with an
      empty-inbound firewall. Docker/Compose and cloudflared bootstrapped;
      restart, service-token SSH, and the pinned host key verified.
- [ ] Production application containers remain to be deployed.
- [x] CX23 unavailable; the user approved CPX22 in hel1 on 2026-09-25
      at EUR 19.49/month before VAT and IPv4.
- [x] GitHub Actions service token created and a reusable SSH Service Auth
      policy attached; the existing email policy is preserved.
- [ ] Review and merge the branch to main before the first deployment.

## Phase 1: manual prerequisites

Complete these steps before running any provisioning command:

1. Use the existing Cloudflare account and Zero Trust organization.
2. Create a custom Cloudflare API token restricted to the account and
   `johancarlin.com` zone with:
   - account: Cloudflare Tunnel Edit (required to retrieve the connector token);
   - account: Access Apps and Policies Edit;
   - account: Access Service Tokens Edit;
   - account: Access Organizations, Identity Providers, and Groups Read;
   - zone: Zone Read and DNS Read.

   DNS, tunnel routes, Access applications, OTP and email policies are adopted
   and checked, never recreated or overwritten. An unexpected difference stops
   provisioning for review.
3. Copy `deploy/.env.infrastructure.example` to
   `deploy/.env.infrastructure`, fill in the token, account ID, zone ID, and
   access email, then restrict it to mode `0600`:

   ```bash
   cp deploy/.env.infrastructure.example deploy/.env.infrastructure
   chmod 600 deploy/.env.infrastructure
   $EDITOR deploy/.env.infrastructure
   ```

   Keep this file, `.deploy-state/`, rendered cloud-init, SSH keys, and
   service-token credentials private. Never paste credentials into chat or
   commit them.
4. Keep the current Entra identity available for the first production browser
   sign-in. The production callback is already registered by the Entra setup
   script.

## Phase 2: provision Cloudflare and Hetzner

Run these commands from the repository root after Phase 1:

```bash
# provision.sh loads the private infrastructure file automatically.
scripts/provision.sh plan
scripts/provision.sh apply
scripts/record-host-key.sh
```

Review the plan before applying it. The reconciliation must create or reuse,
without duplicates:

- one Ubuntu 24.04 CPX22 in `hel1`;
- an empty-inbound Hetzner firewall;
- a connector on the VM for the existing outbound Cloudflare Tunnel;
- the existing DNS and tunnel routes for both hostnames;
- the existing SSH Access application and email policy, plus a GitHub Actions
  Service Auth policy; preserve the existing chat Access application;
- the deployment SSH key and a host key generated before VM creation, supplied
  securely through cloud-init, then verified through the tunnel.

Before any writes, plan/apply checks the requested server type's current
location availability. The approved deployment uses CPX22/hel1. If an alternative is approved,
set `HCLOUD_SERVER_TYPE` and/or `HCLOUD_LOCATION` in the infrastructure file.
A provider capacity error during creation can still occur; rerunning discovers
resources already created and preserves their credentials.

The VM bootstrap installs Docker, Compose, cloudflared, SSH hardening, the
deployment directory, and the persistent Agentgateway data directory. It must
not start the application containers. A second `plan` run should report no
unexpected changes, secret rotations, or duplicate resources.

Before proceeding, verify that:

- `chat.johancarlin.com` and `ssh.johancarlin.com` resolve through Cloudflare;
- Cloudflare Access admits the configured email interactively;
- the service token can open the SSH tunnel;
- the recorded host key is non-empty and remains stable;
- the VM has no publicly reachable SSH, gateway, or Open WebUI port.

## Phase 3: configure and run the GitHub deployment

Configure the repository secrets and variables from this host:

```bash
scripts/configure-github.sh
gh secret list
gh variable list
```

The script reads the OpenCode Go key only from `~/.pi/agent/auth.json`; it does
not print the key. It configures the dedicated deployment SSH key, strict host
key, Cloudflare Access service credentials, Entra settings, OpenCode Go
settings, public URL, and production callback.

The workflow is a manual `workflow_dispatch`, serialized by a deployment
concurrency group, and intentionally runs only when dispatched from `main`.
This branch must therefore be reviewed and merged to `main` before the first
production deployment. Then run:

```bash
gh workflow run Deploy --ref main -f revision=main
gh run list --workflow Deploy --limit 1
gh run watch <run-id>
```

The workflow connects through Cloudflare Access, checks the recorded host key,
transfers the checked-out Compose files and private environment, validates the
rendered configuration without printing values, pulls pinned images, backs up
the previous configuration, and starts the stack. It must leave the
OpenWebUI and Agentgateway data volumes in place across redeployments.

## Production acceptance checks

- Pass Cloudflare email/OTP at `https://chat.johancarlin.com`, then sign in
  with Entra; verify the first user
  is an administrator and the production callback is correct.
- Send multiple turns in one conversation and a separate conversation. Verify
  streaming, stable `x-opencode-session` per conversation, and different
  session values between conversations.
- Reach the loopback-only Agentgateway UI through an authorized SSH forward;
  verify each successful request contains model,
  input/output usage, tenant ID, object ID, and verified email attribution.
- Confirm missing, malformed, wrong-audience, and missing-scope tokens fail;
  confirm a correctly scoped token succeeds.
- Confirm access-token renewal works and Open WebUI accounts,
  conversations, and Agentgateway logs survive container restarts.
- Run `scripts/provision.sh plan` again and confirm no duplicate resources or
  unexpected rotations.
- Verify public SSH and gateway ports remain inaccessible; authorized Actions
  SSH succeeds only through Cloudflare Access.
- Run a second GitHub deployment and verify the previous configuration is
  retained for rollback and the health check fails the workflow if Open WebUI
  is unhealthy.

## Operational follow-up

- Back up the Open WebUI and Agentgateway data directories before destructive
  infrastructure changes.
- Keep Cloudflare, Entra, GitHub, and deployment credentials in their intended
  stores only; rotate them deliberately and update the ignored state files.
- Do not migrate the local conversations into production unless that becomes a
  separate, explicit task.

## Repeatable validation

Run before provisioning or deployment changes:

```bash
shellcheck scripts/*.sh
python3 -m unittest discover -s tests -v
```

The tests use fake providers and temporary directories, plus real Compose
interpolation when Docker Compose is installed. They do not provision cloud
resources or modify the local application stack. Live sign-in, token renewal,
external port isolation and provider permissions still require acceptance checks.
