import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("provision", ROOT / "scripts/provision.py")
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
TUNNEL = "7f74c6f3-49cb-4195-96e5-dcbd3a861b87"


class CloudflareFake:
    def __init__(self):
        self.writes = []
        self.routes = [{"hostname": f"{h}.johancarlin.com", "service": s} for h, s in
                       (("chat", "http://127.0.0.1:3000"), ("ssh", "ssh://127.0.0.1:22"))]
        self.routes.append({"hostname": "unrelated.example.com", "service": "http://localhost:9999"})
        self.apps = [{"id": h, "domain": f"{h}.johancarlin.com", "type": "self_hosted"} for h in ("chat", "ssh")]
        self.policies = {h: [{"id": "email", "name": "existing-email", "reusable": True, "precedence": 1, "decision": "allow",
                             "include": [{"email": {"email": "user@example.com"}}]}] for h in ("chat", "ssh")}
        self.tokens = []
        self.shared_policies = []
        self.fail_app_update = False
        self.dns_target = TUNNEL + ".cfargotunnel.com"

    def get(self, path):
        if path == "/zones/zone":
            return {"name": "johancarlin.com", "account": {"id": "account"}}
        if path.endswith("/access/organizations"):
            return {"name": "existing"}
        if path.endswith("/configurations"):
            return {"config": {"ingress": copy.deepcopy(self.routes)}}
        if path.endswith('/access/apps/ssh'):
            return {**copy.deepcopy(self.apps[1]), 'policies': copy.deepcopy(self.policies['ssh']), 'session_duration': '24h'}
        if path.endswith("/token"):
            return "test-connector-token"
        if path.endswith("/cfd_tunnel/" + TUNNEL):
            return {"name": "demo-llm-oauth", "config_src": "cloudflare"}
        raise AssertionError(path)

    def listing(self, path):
        if "/dns_records?" in path:
            return [{"id": "dns", "type": "CNAME", "proxied": True, "content": self.dns_target}]
        if path.endswith("/identity_providers"):
            return [{"id": "otp", "type": "onetimepin"}]
        if path.endswith("/apps"):
            return copy.deepcopy(self.apps)
        if path.endswith('/access/policies'):
            return copy.deepcopy(self.shared_policies)
        if path.endswith("/policies"):
            return copy.deepcopy(self.policies[path.split("/")[-2]])
        if path.endswith("/service_tokens"):
            return copy.deepcopy(self.tokens)
        raise AssertionError(path)

    def request(self, method, path, body):
        self.writes.append((method, path, body))
        if path.endswith("/service_tokens"):
            token = {"id": "token-1", "name": body["name"], "client_id": "test-client", "client_secret": "test-secret", "expires_at": "2099-01-01T00:00:00Z"}
            self.tokens.append(token)
            return {"result": token}
        if path.endswith('/access/policies'):
            assert body['decision'] in ('allow', 'deny', 'non_identity', 'bypass')
            policy = {'id': 'policy-1', 'reusable': True, **body}
            self.shared_policies.append(policy)
            return {'result': policy}
        if method == 'PUT' and path.endswith('/access/apps/ssh'):
            if self.fail_app_update:
                raise p.ProvisionError('injected app update failure')
            assert body['session_duration'] == '24h'
            old = {p['id']: p for p in self.policies['ssh'] + self.shared_policies}
            self.policies['ssh'] = [{**old[p['id']], 'precedence': p['precedence']} for p in body['policies']]
            return {'result': body}
        raise AssertionError((method, path))


class HetznerFake:
    def __init__(self):
        self.resources = {"ssh-key": [], "firewall": [], "server": []}
        self.writes = []
        self.fail_server = False
        self.available = True

    def listing(self, kind):
        if kind == "server-type":
            return [{"name": "cpx22", "architecture": "x86", "locations": [{"name": "hel1", "available": self.available}]}]
        return copy.deepcopy(self.resources[kind])

    def command(self, *args):
        self.writes.append(args)
        kind = args[0]
        value = {"id": len(self.writes), "name": args[args.index("--name") + 1], "labels": {"owner": "llm-oauth"}}
        if kind == "ssh-key":
            value["public_key"] = args[args.index("--public-key") + 1]
        elif kind == "firewall":
            rules = json.loads(Path(args[args.index("--rules-file") + 1]).read_text())
            assert rules == [], "hcloud expects a bare array"
            value["rules"] = rules
        else:
            if self.fail_server:
                raise p.ProvisionError("injected server failure")
            rendered = Path(args[args.index("--user-data-from-file") + 1]).read_text()
            assert "REPLACE_WITH_" not in rendered
            assert "ed25519_private:" in rendered
            value.update(status="running", server_type={"name": "cpx22"}, location={"name": "hel1"}, image={"name": "ubuntu-24.04"},
                         public_net={"firewalls": [{"id": self.resources["firewall"][0]["id"], "status": "applied"}]})
        self.resources[kind].append(value)
        return {kind.replace("-", "_"): value}


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.env = {"DEPLOY_STATE_DIR": str(self.state), "CLOUDFLARE_ACCOUNT_ID": "account", "CLOUDFLARE_ZONE_ID": "zone", "CLOUDFLARE_ACCESS_EMAIL": "user@example.com"}
        self.cf, self.hc = CloudflareFake(), HetznerFake()

    def provisioner(self):
        return p.Provisioner(self.env, self.cf, self.hc)

    def test_plan_is_read_only_and_adopts_existing_cloudflare(self):
        instance = self.provisioner()
        instance.discover()
        self.assertFalse(self.state.exists())
        self.assertEqual(self.cf.writes, [])
        self.assertEqual(self.hc.writes, [])
        self.assertTrue(any("demo-llm-oauth" in a for a in instance.actions))

    def test_apply_then_reapply_preserves_resources_and_credentials(self):
        before = copy.deepcopy((self.cf.routes, self.cf.apps, self.cf.policies["chat"]))
        instance = self.provisioner()
        instance.discover()
        instance.apply()
        secret = (self.state / "cloudflare.json").read_bytes()
        host_key = (self.state / "host_ed25519").read_bytes()
        writes = (len(self.cf.writes), len(self.hc.writes))
        again = self.provisioner()
        again.discover()
        self.assertFalse(any(a.startswith("CREATE") for a in again.actions))
        again.apply()
        self.assertEqual(writes, (len(self.cf.writes), len(self.hc.writes)))
        self.assertEqual(before, (self.cf.routes, self.cf.apps, self.cf.policies["chat"]))
        self.assertEqual(secret, (self.state / "cloudflare.json").read_bytes())
        self.assertEqual(host_key, (self.state / "host_ed25519").read_bytes())
        self.assertEqual((self.state / "cloud-init.rendered.yaml").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.cf.policies["ssh"][0]["name"], "existing-email")

    def test_partial_failure_recovers_without_duplicate_tokens_or_keys(self):
        self.hc.fail_server = True
        instance = self.provisioner()
        instance.discover()
        with self.assertRaises(p.ProvisionError):
            instance.apply()
        self.hc.fail_server = False
        retry = self.provisioner()
        retry.discover()
        retry.apply()
        self.assertEqual(len(self.cf.tokens), 1)
        self.assertTrue(all(len(v) == 1 for v in self.hc.resources.values()))

    def test_unknown_service_token_secret_stops_before_mutation(self):
        self.cf.tokens = [{"id": "lost", "name": "llm-oauth-github-actions"}]
        with self.assertRaisesRegex(p.ProvisionError, "no matching local credentials"):
            self.provisioner().discover()
        self.assertEqual(self.cf.writes + self.hc.writes, [])

    def test_unavailable_capacity_stops_before_cloudflare_changes(self):
        self.hc.available = False
        with self.assertRaisesRegex(p.ProvisionError, "unavailable in hel1"):
            self.provisioner().discover()
        self.assertEqual(self.cf.writes + self.hc.writes, [])

    def test_policy_attachment_retry_reuses_unattached_policy(self):
        self.cf.fail_app_update = True
        instance = self.provisioner()
        instance.discover()
        with self.assertRaisesRegex(p.ProvisionError, "app update"):
            instance.apply()
        self.cf.fail_app_update = False
        retry = self.provisioner()
        retry.discover()
        retry.apply()
        self.assertEqual(len(self.cf.shared_policies), 1)
        self.assertEqual(len(self.cf.tokens), 1)
        self.assertEqual(self.cf.policies['ssh'][0]['name'], 'existing-email')

    def test_conflicts_stop_before_mutation(self):
        cases = [
            lambda: setattr(self.cf, "dns_target", "other.cfargotunnel.com"),
            lambda: self.cf.routes[0].update(service="http://localhost:9999"),
            lambda: self.cf.apps.append(copy.deepcopy(self.cf.apps[0])),
            lambda: self.hc.resources["firewall"].append({"id": 9, "name": "llm-oauth", "labels": {"owner": "another-project"}}),
            lambda: self.hc.resources["firewall"].append({"id": 9, "name": "llm-oauth", "labels": {"owner": "llm-oauth"}, "rules": [{"direction": "in"}]}),
        ]
        for conflict in cases:
            with self.subTest(conflict=conflict):
                self.cf, self.hc = CloudflareFake(), HetznerFake()
                conflict()
                with self.assertRaises(p.ProvisionError):
                    self.provisioner().discover()
                self.assertEqual(self.cf.writes + self.hc.writes, [])

    def test_pagination(self):
        client = p.Cloudflare("test")
        pages = []
        def request(method, path):
            pages.append(path)
            return {"result": [len(pages)], "result_info": {"total_pages": 2}}
        client.request = request
        self.assertEqual(client.listing("/example?name=test"), [1, 2])
        self.assertIn("&page=2", pages[1])

    def test_recorded_snapshot_is_adopted_and_preserved(self):
        instance = self.provisioner()
        instance.discover()
        instance.apply()
        image = {"id": 400, "type": "snapshot", "os_flavor": "ubuntu", "os_version": "24.04"}
        self.hc.resources['server'][0]['image'] = image
        path = self.state / 'infra.json'
        infra = json.loads(path.read_text())
        infra['snapshotId'] = '400'
        p.private_write(path, json.dumps(infra))
        again = self.provisioner()
        again.discover()
        again.apply()
        self.assertEqual(json.loads(path.read_text())['snapshotId'], '400')
        image['id'] = 401
        with self.assertRaisesRegex(p.ProvisionError, 'recorded restore snapshot'):
            self.provisioner().discover()

    def test_paused_vm_cannot_be_replaced_by_blank_provision(self):
        instance = self.provisioner()
        instance.discover()
        instance.apply()
        self.hc.resources['server'] = []
        p.private_write(self.state / 'lifecycle.json', '{"phase":"paused"}')
        with self.assertRaisesRegex(p.ProvisionError, 'vm-lifecycle.sh restore'):
            self.provisioner().discover()


if __name__ == "__main__":
    unittest.main()
