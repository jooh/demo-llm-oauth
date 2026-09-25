#!/usr/bin/env python3
"""Adopt existing Cloudflare resources and reconcile the dedicated Hetzner VM.

Plan performs reads only. Discover and validate all resources before mutation.
Existing DNS, tunnel routes, Access applications and email policies are preserved.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent


class ProvisionError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ProvisionError(message)


def private_write(path, text):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_state(path):
    if not path.exists():
        return {}
    require(path.stat().st_mode & 0o077 == 0, f"Restrict {path} to mode 0600")
    return json.loads(path.read_text())


def run(args):
    result = subprocess.run(args, text=True, capture_output=True, check=False, stdin=subprocess.DEVNULL)
    # Provider output can contain cloud-init or credentials. Never echo it.
    require(result.returncode == 0, f"{args[0]} {args[1]} failed (exit {result.returncode}); inspect provider state before retrying")
    return result.stdout.strip()


def unique(items, description):
    require(len(items) <= 1, f"Multiple matches for {description}; resolve the conflict first")
    return items[0] if items else None


class Cloudflare:
    def __init__(self, token):
        self.token = token

    def request(self, method, path, body=None):
        request = urllib.request.Request(
            "https://api.cloudflare.com/client/v4" + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                errors = json.load(exc).get("errors", [])
                codes = ",".join(str(e.get("code", "unknown")) for e in errors)
            except (ValueError, AttributeError):
                codes = "unknown"
            raise ProvisionError(f"Cloudflare {method} {path.split('?')[0]} returned HTTP {exc.code} (codes {codes}); inspect permissions or resource format") from None
        except (urllib.error.URLError, ValueError):
            raise ProvisionError("Cloudflare request failed; check connectivity and retry") from None
        require(payload.get("success") is True and payload.get("result") is not None,
                f"Cloudflare {method} {path.split('?')[0]} did not succeed")
        return payload

    def get(self, path):
        return self.request("GET", path)["result"]

    def listing(self, path):
        items, page = [], 1
        while True:
            separator = "&" if "?" in path else "?"
            payload = self.request("GET", f"{path}{separator}page={page}&per_page=100")
            require(isinstance(payload["result"], list), "Cloudflare returned an invalid resource list")
            items.extend(payload["result"])
            if page >= payload.get("result_info", {}).get("total_pages", 1):
                return items
            page += 1


class Hetzner:
    def listing(self, kind):
        return json.loads(run(["hcloud", kind, "list", "-o", "json"]))

    def command(self, *args):
        return json.loads(run(["hcloud", *args, "-o", "json"]))


class Provisioner:
    def __init__(self, env, cf, hc, root=ROOT):
        self.env, self.cf, self.hc, self.root = env, cf, hc, root
        self.state_dir = Path(env.get("DEPLOY_STATE_DIR", root / ".deploy-state"))
        self.state_path = self.state_dir / "cloudflare.json"
        self.state = read_state(self.state_path)
        self.infra = read_state(self.state_dir / "infra.json")
        for name in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ZONE_ID", "CLOUDFLARE_ACCESS_EMAIL"):
            require(env.get(name), f"{name} is required")
        self.account = "/accounts/" + env["CLOUDFLARE_ACCOUNT_ID"]
        self.zone = "/zones/" + env["CLOUDFLARE_ZONE_ID"]
        self.email = env["CLOUDFLARE_ACCESS_EMAIL"]
        self.chat = env.get("CHAT_HOSTNAME", "chat.johancarlin.com")
        self.ssh = env.get("SSH_HOSTNAME", "ssh.johancarlin.com")
        self.tunnel_id = env.get("CLOUDFLARE_TUNNEL_ID", "7f74c6f3-49cb-4195-96e5-dcbd3a861b87")
        self.tunnel_name = env.get("CLOUDFLARE_TUNNEL_NAME", "demo-llm-oauth")
        self.server_name = env.get("HCLOUD_SERVER_NAME", "llm-oauth")
        self.server_type = env.get("HCLOUD_SERVER_TYPE", "cpx22")
        self.location = env.get("HCLOUD_LOCATION", "hel1")
        self.firewall_name = env.get("HCLOUD_FIREWALL_NAME", "llm-oauth")
        self.key_name = env.get("HCLOUD_SSH_KEY_NAME", "llm-oauth-deploy")
        self.key_path = Path(env.get("DEPLOY_SSH_KEY_PATH", self.state_dir / "deploy.key"))
        self.host_key_path = self.state_dir / "host_ed25519"
        self.actions = []

    def note(self, action, resource):
        self.actions.append(f"{action:7} {resource}")

    def save(self):
        private_write(self.state_path, json.dumps(self.state, indent=2) + "\n")

    def discover(self):
        zone = self.cf.get(self.zone)
        require(zone["name"] == self.env.get("CLOUDFLARE_ZONE", "johancarlin.com"), "Cloudflare zone does not match")
        require(zone["account"]["id"] == self.env["CLOUDFLARE_ACCOUNT_ID"], "Zone belongs to a different account")
        self.cf.get(self.account + "/access/organizations")
        tunnel = self.cf.get(self.account + "/cfd_tunnel/" + self.tunnel_id)
        require(tunnel.get("name") == self.tunnel_name and not tunnel.get("deleted_at"), "Existing tunnel name/ID mismatch or tunnel deleted")
        require(tunnel.get("config_src") == "cloudflare", "Expected a remotely managed Cloudflare tunnel")
        require(self.state.get("tunnelId", self.tunnel_id) == self.tunnel_id, "Local state refers to a different tunnel")
        config = self.cf.get(self.account + f"/cfd_tunnel/{self.tunnel_id}/configurations")["config"]
        idps = self.cf.listing(self.account + "/access/identity_providers")
        otp_ids = {item["id"] for item in idps if item.get("type") == "onetimepin"}
        require(otp_ids, "One-time PIN identity provider is missing")
        apps = self.cf.listing(self.account + "/access/apps")
        self.note("REUSE", f"tunnel {self.tunnel_name} ({self.tunnel_id})")
        for hostname, service in ((self.chat, "http://127.0.0.1:3000"), (self.ssh, "ssh://127.0.0.1:22")):
            route = unique([item for item in config.get("ingress", []) if item.get("hostname") == hostname], f"route {hostname}")
            require(route and route.get("service") == service and not route.get("path"), f"Tunnel route for {hostname} is missing or differs from {service}")
            record = unique(self.cf.listing(self.zone + "/dns_records?name=" + urllib.parse.quote(hostname)), f"DNS {hostname}")
            require(record and record.get("type") == "CNAME" and record.get("proxied") is True
                    and record.get("content", "").rstrip(".") == self.tunnel_id + ".cfargotunnel.com", f"DNS conflict for {hostname}")
            app = unique([item for item in apps if item.get("domain") == hostname], f"Access application {hostname}")
            require(app and app.get("type") == "self_hosted", f"Self-hosted Access application missing for {hostname}")
            allowed = app.get("allowed_idps") or []
            require(not allowed or otp_ids.intersection(allowed), f"OTP is not enabled for {hostname}")
            policies = self.cf.listing(self.account + f'/access/apps/{app["id"]}/policies')
            require(any(p.get("decision") == "allow" and any(r.get("email", {}).get("email", "").lower() == self.email.lower()
                        for r in p.get("include", [])) for p in policies), f"Email allow policy missing for {hostname}")
            require(not any(p.get("decision") == "bypass" for p in policies), f"Unexpected bypass policy on {hostname}; review before proceeding")
            if hostname == self.ssh:
                self.ssh_app = self.cf.get(self.account + f'/access/apps/{app["id"]}')
                self.ssh_policies = policies
            self.note("REUSE", f"{hostname}: DNS, route and Access email policy")

        tokens = self.cf.listing(self.account + "/access/service_tokens")
        self.service_token = unique([t for t in tokens if t.get("name") == "llm-oauth-github-actions"], "Actions service token")
        if self.service_token:
            require(self.state.get("serviceTokenId") == self.service_token["id"]
                    and self.state.get("serviceTokenClientId") and self.state.get("serviceTokenClientSecret"),
                    "Existing Actions service token has no matching local credentials; restore private state or arrange deliberate rotation")
            expiry = self.service_token.get("expires_at")
            require(expiry and dt.datetime.fromisoformat(expiry.replace("Z", "+00:00")) > dt.datetime.now(dt.timezone.utc), "Actions service token has expired")
        else:
            require(not self.state.get("serviceTokenId"), "Recorded service token no longer exists; deliberate recovery is required")
        token_id = self.service_token["id"] if self.service_token else None
        self.service_policy = next((p for p in self.ssh_policies if token_id and p.get("decision") == "non_identity"
                                   and p.get("include") == [{"service_token": {"token_id": token_id}}]
                                   and not p.get("require") and not p.get("exclude")), None)
        require(self.service_policy or not any(p.get("name") == "github-actions" for p in self.ssh_policies), "Existing github-actions policy differs; review it before proceeding")
        self.shared_policy = unique([p for p in self.cf.listing(self.account + "/access/policies")
                                     if p.get("name") == "llm-oauth-github-actions"], "reusable Actions policy")
        if self.shared_policy:
            require(token_id and self.shared_policy.get("decision") == "non_identity"
                    and self.shared_policy.get("include") == [{"service_token": {"token_id": token_id}}]
                    and not self.shared_policy.get("require") and not self.shared_policy.get("exclude"),
                    "Reusable Actions policy differs; review it before proceeding")
        if not self.service_policy:
            require(all(p.get("reusable") is True for p in self.ssh_policies),
                    "SSH application has legacy policies; review their migration before attaching a reusable policy")
        self.note("REUSE" if self.service_token else "CREATE", "GitHub Actions service token")
        self.note("REUSE" if self.service_policy else "CREATE", "SSH Service Auth policy (email policies preserved)")

        self.key = unique([k for k in self.hc.listing("ssh-key") if k["name"] == self.key_name], "deployment SSH key")
        self.firewall = unique([f for f in self.hc.listing("firewall") if f["name"] == self.firewall_name], "firewall")
        self.server = unique([s for s in self.hc.listing("server") if s["name"] == self.server_name], "server")
        for item, label in ((self.key, "SSH key"), (self.firewall, "firewall"), (self.server, "server")):
            require(not item or item.get("labels", {}).get("owner") == "llm-oauth", f"Existing {label} is not owned by this deployment")
        self.public_key = None
        if self.key_path.exists():
            require(self.key_path.stat().st_mode & 0o077 == 0, "Deployment private key must have mode 0600")
            self.public_key = run(["ssh-keygen", "-y", "-f", str(self.key_path)])
        require(not self.env.get("DEPLOY_SSH_PUBLIC_KEY") or self.public_key
                and self.env["DEPLOY_SSH_PUBLIC_KEY"].split()[:2] == self.public_key.split()[:2], "A matching private deployment key is required")
        require(not self.key or self.public_key and self.key["public_key"].split()[:2] == self.public_key.split()[:2], "Hetzner SSH key does not match the local private key")
        require(not self.firewall or not self.firewall.get("rules"), "Existing firewall has rules; refusing to overwrite them")
        if self.server:
            location = self.server.get("location") or (self.server.get("datacenter") or {}).get("location", {})
            require(self.server["server_type"]["name"] == self.server_type and location.get("name") == self.location, "Existing server type/location differs from the requested configuration")
            image = self.server.get("image") or {}
            restored = self.infra.get("snapshotId") and str(image.get("id")) == self.infra["snapshotId"] and image.get("type") == "snapshot" and image.get("os_flavor") == "ubuntu" and image.get("os_version") == "24.04"
            require(image.get("name") == "ubuntu-24.04" or restored, "Existing server image is not Ubuntu 24.04 or the recorded restore snapshot")
            require(self.key and self.firewall and self.host_key_path.exists(), "Existing server is missing expected deployment state; recover it first")
            require(self.server.get("status") == "running", "Existing server is not running; inspect it before proceeding")
            require(self.infra.get("serverId", str(self.server["id"])) == str(self.server["id"]), "Local server ID differs from the existing server")
            attached = self.server.get("public_net", {}).get("firewalls", [])
            require(len(attached) == 1 and attached[0]["id"] == self.firewall["id"] and attached[0].get("status") == "applied", "Existing server firewall attachment differs; inspect it before proceeding")
        else:
            require(not (self.state_dir / "lifecycle.json").exists(), "VM lifecycle state exists; use scripts/vm-lifecycle.sh restore instead of provisioning a blank VM")
            require(not self.infra.get("serverId"), "Recorded VM no longer exists; reconcile state before rebuilding")
            server_type = unique([s for s in self.hc.listing("server-type") if s["name"] == self.server_type], "server type")
            require(server_type and server_type.get("architecture") == "x86", "Requested server type must support the pinned amd64 cloudflared package")
            require(any(loc.get("name") == self.location and loc.get("available") for loc in server_type.get("locations", [])),
                    f"{self.server_type} is unavailable in {self.location}; choose an alternative explicitly or retry when capacity returns")
        for item, label in ((self.key, "deployment SSH key"), (self.firewall, "empty-inbound firewall"), (self.server, f"Ubuntu 24.04 {self.server_type} in {self.location}")):
            self.note("REUSE" if item else "CREATE", label)
        self.note("VERIFY" if self.server else "INSTALL", "cloudflared connector, Docker and Compose; application containers remain separate")

    def keypair(self, path, comment):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists():
            require(not Path(str(path) + ".pub").exists(), f"Orphaned public key {path.name}.pub; recover the private key first")
            run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)])
        return run(["ssh-keygen", "-y", "-f", str(path)])

    def apply(self):
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        self.state["tunnelId"] = self.tunnel_id
        # Reading the connector token does not rotate it.
        self.state["tunnelToken"] = self.cf.get(self.account + f"/cfd_tunnel/{self.tunnel_id}/token")
        require(isinstance(self.state["tunnelToken"], str) and self.state["tunnelToken"], "Tunnel token unavailable")
        self.save()
        if not self.service_token:
            token = self.cf.request("POST", self.account + "/access/service_tokens", {"name": "llm-oauth-github-actions", "duration": "8760h"})["result"]
            require(all(token.get(k) for k in ("id", "client_id", "client_secret")), "Service-token creation returned incomplete credentials; inspect Cloudflare before retrying")
            self.state.update(serviceTokenId=token["id"], serviceTokenClientId=token["client_id"], serviceTokenClientSecret=token["client_secret"])
            self.save()  # The secret is only returned at creation.
        if not self.service_policy:
            precedence = max((p.get("precedence", 0) or 0 for p in self.ssh_policies), default=0) + 1
            if not self.shared_policy:
                self.shared_policy = self.cf.request("POST", self.account + "/access/policies", {
                    "name": "llm-oauth-github-actions", "decision": "non_identity",
                    "include": [{"service_token": {"token_id": self.state["serviceTokenId"]}}],
                })["result"]
            path = self.account + f'/access/apps/{self.ssh_app["id"]}'
            current = self.cf.get(path)
            require(current == self.ssh_app, "SSH application changed since discovery; rerun plan before applying")
            private_write(self.state_dir / "ssh-access-before.json", json.dumps(current, indent=2) + "\n")
            # PUT requires the application settings as well as policy links.
            # Preserve every existing writable setting and every policy link.
            readonly = {"id", "uid", "aud", "created_at", "updated_at"}
            body = {k: v for k, v in current.items() if k not in readonly}
            body["policies"] = [{"id": p["id"], "precedence": p.get("precedence", i + 1)}
                                for i, p in enumerate(self.ssh_policies)]
            body["policies"].append({"id": self.shared_policy["id"], "precedence": precedence})
            self.cf.request("PUT", path, body)
        self.public_key = self.keypair(self.key_path, "llm-oauth-deploy")
        host_public = self.keypair(self.host_key_path, "llm-oauth-host")
        private_write(self.state_dir / "known_hosts.expected", f"[127.0.0.1]:2222 {host_public}\n")
        if not self.key:
            self.key = self.hc.command("ssh-key", "create", "--name", self.key_name, "--public-key", self.public_key, "--label", "owner=llm-oauth")["ssh_key"]
        if not self.firewall:
            rules = self.state_dir / "firewall-rules.json"
            private_write(rules, "[]\n")
            self.firewall = self.hc.command("firewall", "create", "--name", self.firewall_name, "--label", "owner=llm-oauth", "--rules-file", str(rules))["firewall"]
        if not self.server:
            rendered = (self.root / "deploy/cloud-init.template.yaml").read_text()
            # JSON strings are YAML scalars and safely quote key material.
            for marker, value in {
                "REPLACE_WITH_DEPLOY_SSH_PUBLIC_KEY": self.public_key,
                "REPLACE_WITH_CLOUDFLARED_TUNNEL_TOKEN": self.state["tunnelToken"],
                "REPLACE_WITH_HOST_ED25519_PRIVATE_KEY": self.host_key_path.read_text(),
                "REPLACE_WITH_HOST_ED25519_PUBLIC_KEY": host_public,
            }.items():
                rendered = rendered.replace(marker, json.dumps(value))
            cloud_init = self.state_dir / "cloud-init.rendered.yaml"
            private_write(cloud_init, rendered)
            self.server = self.hc.command("server", "create", "--name", self.server_name, "--type", self.server_type, "--image", "ubuntu-24.04", "--location", self.location, "--ssh-key", str(self.key["id"]), "--firewall", str(self.firewall["id"]), "--label", "owner=llm-oauth", "--user-data-from-file", str(cloud_init))["server"]
        private_write(self.state_dir / "infra.json", json.dumps({
            **self.infra,
            "serverId": str(self.server["id"]), "firewallId": str(self.firewall["id"]),
            "sshKeyId": str(self.key["id"]), "sshHostname": self.ssh,
        }, indent=2) + "\n")
        print("Infrastructure reconciled. Wait for bootstrap, then run scripts/record-host-key.sh.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "apply"), default="plan", nargs="?")
    args = parser.parse_args()
    try:
        require(os.environ.get("CLOUDFLARE_API_TOKEN"), "CLOUDFLARE_API_TOKEN is required; populate deploy/.env.infrastructure")
        for binary in ("hcloud", "ssh-keygen"):
            require(shutil.which(binary), f"{binary} is required")
        provisioner = Provisioner(os.environ, Cloudflare(os.environ["CLOUDFLARE_API_TOKEN"]), Hetzner())
        provisioner.discover()
        print("\n".join(provisioner.actions), flush=True)
        if args.mode == "apply":
            provisioner.apply()
        else:
            print("Plan only: no resources, keys or state files changed.")
    except (ProvisionError, OSError, KeyError, ValueError) as exc:
        # Provider parse errors might contain credentials: only show our errors.
        message = str(exc) if isinstance(exc, ProvisionError) else "Invalid provider response or local state; inspect configuration before retrying"
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
