# Open WebUI + Agentgateway + Entra

Two stock containers, configured entirely in `compose.yaml`. Open WebUI signs
users into Entra, obtains an access token for the gateway, and sends that token
on LLM requests. Agentgateway validates the signature, issuer, audience and
expiry, requires the delegated `llm.invoke` scope, and calls your existing
OpenAI-compatible LLM endpoint. Open WebUI manages token renewal.

The deployment exposes Open WebUI at `http://localhost:3000` and the gateway at
`http://localhost:4000/v1`. Both host ports bind only to loopback. The containers
communicate over their private Compose network. This is a local evaluation
setup; see the remote deployment notes below before serving other machines.

## Requirements

- Docker Engine or Docker Desktop with Docker Compose 2.23.1 or later. The
  `configs.content` feature embeds the gateway configuration in the Compose file.
- An existing OpenAI-compatible LLM server, such as NVIDIA NIM, reachable from
  the gateway container. This setup does not start or download a model.
- Permission to create two single-tenant Entra app registrations and grant
  delegated API consent.
- Network access to Entra for login, token refresh and signing keys, and to the
  two image registries for the initial image pull.

Pinned images: `cr.agentgateway.dev/agentgateway:v1.5.0` and
`ghcr.io/open-webui/open-webui:v0.11.3`. No Dockerfile or initialization script
is required. Open WebUI uses its built-in SQLite database on a named volume.

## 1. Register the gateway API in Entra

1. Create a single-tenant app registration named `LLM Gateway`.
2. Record its **Application (client) ID** as `GATEWAY_APP_ID` and your
   **Directory (tenant) ID** as `ENTRA_TENANT_ID`.
3. Under **Expose an API**, set the Application ID URI to
   `api://<GATEWAY_APP_ID>`.
4. Add an enabled delegated scope named `llm.invoke`. Choose **Admins only**
   for consent and supply the requested display name and description.
5. In the app manifest, set `api.requestedAccessTokenVersion` to `2`, preserving
   the other fields. This makes the access token's audience the API's bare
   application-ID GUID and its issuer the tenant's `/v2.0` issuer.

The gateway registration needs no redirect URI and no client secret.

## 2. Register Open WebUI as the OAuth client

1. Create another single-tenant app registration named `Open WebUI`.
2. Add a **Web** platform redirect URI, exactly:
   `http://localhost:3000/oauth/oidc/callback`.
3. Record the Application (client) ID as `WEBUI_CLIENT_ID`.
4. Create a client secret and record its **Value**, not its Secret ID, as
   `WEBUI_CLIENT_SECRET`.
5. Under **API permissions > Add a permission > My APIs**, select the gateway,
   choose **Delegated permissions**, and add `llm.invoke`. Grant admin consent.
6. Under **Token configuration > Add optional claim > ID**, add `email`.
   The signed-in accounts must have an email address. The requested `profile`
   scope supplies `name`. Both claims must be available in the ID token.
7. In the Open WebUI **enterprise application**, set **Assignment required?**
   to **Yes** and assign the users or groups that should sign in.

Use authorization code flow; do not enable implicit grants or public-client
flows. This is a confidential web application, even when testing on localhost.
No Graph data permission such as `User.Read` is needed for this minimal setup.

Open WebUI uses the generic OIDC provider here. Its login handler reads `email`
and `name` from the verified ID token. If either claim is missing, it attempts
the UserInfo endpoint, which belongs to Microsoft Graph in Entra. A token
issued for the gateway cannot authenticate that call. Supplying those ID-token
claims avoids that mismatch; do not solve it by requesting a Graph token in
place of the gateway token.

## 3. Fill in compose.yaml

Replace the following literal placeholders everywhere they occur. Leave the
surrounding `${VARIABLE:-...}` syntax intact, so the file remains configurable
through environment variables as well.

| Placeholder | Replacement |
| --- | --- |
| `REPLACE_WITH_TENANT_ID` | Your Entra tenant GUID |
| `REPLACE_WITH_GATEWAY_APP_ID` | Gateway API application GUID |
| `REPLACE_WITH_WEBUI_CLIENT_ID` | Open WebUI client application GUID |
| `REPLACE_WITH_WEBUI_CLIENT_SECRET` | Open WebUI client secret value |
| `REPLACE_WITH_RANDOM_SECRET` | A persistent random secret for Open WebUI |
| `REPLACE_WITH_UPSTREAM_MODEL` | Exact model ID your LLM server accepts |
| `http://REPLACE_WITH_LLM_HOST:8000/v1` | LLM server's OpenAI-compatible base URL |

Generate the Open WebUI secret, for example, with `openssl rand -hex 32`.
Keep it stable across restarts: it protects sessions and stored OAuth tokens.
If a pasted value contains a literal dollar sign, escape it as `$$` for Compose.
Keep a Compose file containing real secrets private.

If the upstream LLM requires an API key, put its value after `:-` in
`apiKey: "${UPSTREAM_API_KEY:-}"`. Otherwise leave it empty. This
credential is used only for gateway-to-LLM authentication. The Open WebUI
connection has no shared API key; it uses each user's Entra access token.
Agentgateway removes the validated bearer token by default; the model's header
policy also removes browser cookies before forwarding to the LLM server.

Alternatively, export `ENTRA_TENANT_ID`, `GATEWAY_APP_ID`, `WEBUI_CLIENT_ID`,
`WEBUI_CLIENT_SECRET`, `WEBUI_SECRET_KEY`, `UPSTREAM_MODEL`, `UPSTREAM_BASE_URL`
and, if needed, `UPSTREAM_API_KEY` before running Compose. A separate `.env`
file is optional; all deployment configuration is already in `compose.yaml`.

The upstream address is resolved inside the gateway container. `localhost`
would mean that container itself, not your host. Use a reachable server address;
on Docker Desktop, `host.docker.internal` can reach a model on the host.

## 4. Start and sign in

```bash
docker compose config --quiet
docker compose up -d
docker compose ps
```

Open `http://localhost:3000`, sign in with Entra, select **llm**, and send a
message. The gateway maps the stable `llm` alias to `UPSTREAM_MODEL`.
Sign in yourself first: Open WebUI makes the first account an administrator.
Subsequent assigned users receive its normal `user` role.

The model connection is preconfigured with `auth_type: system_oauth` and a
static model ID, so no connection edits or model discovery are needed. The
gateway still checks authorization on every inference request. Open WebUI's
`ENABLE_PERSISTENT_CONFIG=false` makes the Compose settings authoritative at
startup, while conversations and accounts persist in the named volume.

## 5. Check the OAuth boundary

Without a token, the gateway must return **401**:

```bash
curl -i http://localhost:4000/v1/models
```

A malformed token must also return **401**:

```bash
curl -i http://localhost:4000/v1/models \
  -H 'Authorization: Bearer not-a-valid-token'
```

A valid, unexpired token for this tenant and gateway that lacks `llm.invoke`
must fail authorization. A valid token with the scope should allow a chat
request. Tokens intended for Graph or the Open WebUI client are not accepted
by the gateway. For a refresh check, keep the same browser session open past
the access-token lifetime and send another message; `offline_access` enables
renewal, subject to Entra session and Conditional Access policies.

For startup and request failures, inspect `docker compose logs agentgateway`
and `docker compose logs openwebui`. A 401 suggests a missing/expired token,
wrong audience/issuer, or unavailable signing keys. A 403 suggests a missing
scope. An upstream error after successful authorization usually means the
model ID, backend URL or backend credential needs correcting.

Stop with `docker compose down`. The data volume is retained. Running
`docker compose down -v` also deletes the stored accounts and conversations.

## Serving other machines

Put an HTTPS reverse proxy in front of Open WebUI, with WebSocket support.
Set `WEBUI_URL` to your public HTTPS URL and update `OPENID_REDIRECT_URI` plus
the Entra Web redirect URI to that URL followed by `/oauth/oidc/callback`.
Set `WEBUI_SESSION_COOKIE_SECURE` and `WEBUI_AUTH_COOKIE_SECURE` to `"true"`.
Keep the gateway on the internal network; Open WebUI calls it from its server,
so browsers do not need gateway access or gateway CORS configuration. Protect
the upstream LLM from direct user access if the gateway is its access boundary.
The default container-to-container hop is HTTP on one Docker host. Use HTTPS
for that hop too if it crosses a host or other untrusted network.

## Validation and sources

The configuration was rendered by Docker Compose and checked against the
Compose and pinned Agentgateway JSON schemas. It was also checked against the
pinned Open WebUI source for environment-based connection configuration,
OAuth bearer forwarding, ID-token profile handling and refresh support.
Container startup and a live Entra sign-in still need to be verified in your
environment; this workspace has no Docker daemon or your Entra registrations.

- [Agentgateway stock image and Compose](https://agentgateway.dev/docs/standalone/latest/documentation/setup/install/docker/)
- [Agentgateway JWT authentication](https://agentgateway.dev/docs/standalone/latest/documentation/configuration/security/jwt-authn/)
- [Agentgateway authorization](https://agentgateway.dev/docs/standalone/latest/documentation/configuration/security/http-authz/)
- [Open WebUI OAuth and OIDC](https://docs.openwebui.com/features/authentication-access/auth/sso/)
- [Open WebUI environment configuration](https://docs.openwebui.com/reference/env-configuration/)
- [Open WebUI v0.11.3 LLM authentication implementation](https://github.com/open-webui/open-webui/blob/v0.11.3/backend/open_webui/routers/openai.py)
- [Open WebUI v0.11.3 OAuth implementation](https://github.com/open-webui/open-webui/blob/v0.11.3/backend/open_webui/utils/oauth.py)
- [Entra ID token claims](https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference)
- [Entra exposing API scopes](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-protected-web-api-expose-scopes)
- [Compose embedded configuration content](https://docs.docker.com/reference/compose-file/configs/)
