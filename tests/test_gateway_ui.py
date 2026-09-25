import copy
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('gateway_ui', ROOT / 'scripts/expose-gateway.py')
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)


class CloudflareFake:
    def __init__(self):
        self.policy = {'id': 'email', 'reusable': True, 'decision': 'allow',
                       'include': [{'email': {'email': 'owner@example.com'}}]}
        self.app = None
        self.dns = []
        self.config = {'ingress': [{'hostname': 'chat.johancarlin.com', 'service': 'http://127.0.0.1:3000'},
                                   {'service': 'http_status:404'}], 'originRequest': {'connectTimeout': 30}}
        self.writes = []
        self.dns_denied = False
        self.app_unprotected = False

    def get(self, path):
        if path == '/zones/zone':
            return {'name': 'johancarlin.com', 'account': {'id': 'account'}}
        if path.endswith('/access/organizations'):
            return {'auth_domain': 'myteam.cloudflareaccess.com'}
        if path.endswith('/configurations'):
            return {'config': copy.deepcopy(self.config)}
        if path.endswith('/access/apps/gateway'):
            return copy.deepcopy(self.app)
        return {'name': 'demo-llm-oauth', 'config_src': 'cloudflare'}

    def listing(self, path):
        if '/dns_records?' in path:
            return copy.deepcopy(self.dns)
        if path.endswith('/identity_providers'):
            return [{'id': 'otp', 'type': 'onetimepin'}]
        if path.endswith('/apps'):
            return [{'id': 'chat', 'domain': 'chat.johancarlin.com'}] + ([copy.deepcopy(self.app)] if self.app else [])
        if path.endswith('/policies'):
            return [] if '/gateway/' in path and self.app_unprotected else [copy.deepcopy(self.policy)]
        raise AssertionError(path)

    def request(self, method, path, body):
        self.writes.append((method, path, copy.deepcopy(body)))
        if path.endswith('/access/apps'):
            self.app = {**body, 'id': 'gateway', 'aud': 'gateway-aud'}
            return {'result': self.app}
        if path.endswith('/configurations'):
            assert self.app and not self.app_unprotected
            self.config = copy.deepcopy(body['config'])
            return {'result': {'config': self.config}}
        if path.endswith('/dns_records'):
            if self.dns_denied:
                raise g.ProvisionError('DNS permission denied')
            self.dns = [body]
            return {'result': body}
        raise AssertionError(path)


class GatewayUITests(unittest.TestCase):
    def setUp(self):
        self.cf = CloudflareFake()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = {'CLOUDFLARE_ACCOUNT_ID': 'account', 'CLOUDFLARE_ZONE_ID': 'zone',
                    'CLOUDFLARE_ACCESS_EMAIL': 'owner@example.com'}

    def ui(self):
        ui = g.GatewayUI(self.env, self.cf)
        ui.state_dir = Path(self.temp.name)
        return ui

    def test_plan_and_repeat_apply_preserve_existing_config(self):
        original = copy.deepcopy(self.cf.config)
        ui = self.ui()
        ui.discover()
        self.assertEqual(self.cf.writes, [])
        ui.apply()
        self.assertEqual(self.cf.config['ingress'][0], original['ingress'][0])
        self.assertEqual(self.cf.config['originRequest'], original['originRequest'])
        self.assertEqual(self.cf.config['ingress'][-1], original['ingress'][-1])
        access = self.cf.config['ingress'][-2]['originRequest']['access']
        self.assertEqual(access, {'required': True, 'teamName': 'myteam', 'audTag': ['gateway-aud']})
        writes = len(self.cf.writes)
        again = self.ui()
        again.discover()
        again.apply()
        self.assertEqual(len(self.cf.writes), writes)

    def test_missing_access_policy_never_routes_or_creates_dns(self):
        self.cf.app_unprotected = True
        ui = self.ui()
        ui.discover()
        with self.assertRaises(g.ProvisionError):
            ui.apply()
        self.assertEqual(len(self.cf.writes), 1)
        self.assertEqual(self.cf.writes[0][1], '/accounts/account/access/apps')

    def test_dns_permission_failure_can_resume_without_duplicates(self):
        self.cf.dns_denied = True
        ui = self.ui()
        ui.discover()
        with self.assertRaises(g.ProvisionError):
            ui.apply()
        self.cf.dns_denied = False
        again = self.ui()
        again.discover()
        again.apply()
        self.assertEqual(sum(path.endswith('/access/apps') for _, path, _ in self.cf.writes), 1)
        self.assertEqual(sum(path.endswith('/configurations') for _, path, _ in self.cf.writes), 1)

    def test_conflicting_dns_stops_before_any_writes(self):
        self.cf.dns = [{'type': 'A', 'content': '192.0.2.1'}]
        with self.assertRaises(g.ProvisionError):
            self.ui().discover()
        self.assertEqual(self.cf.writes, [])

    def test_concurrent_route_change_is_not_overwritten(self):
        ui = self.ui()
        ui.discover()
        self.cf.config['originRequest']['connectTimeout'] = 60
        with self.assertRaises(g.ProvisionError):
            ui.apply()
        self.assertEqual(self.cf.config['originRequest']['connectTimeout'], 60)
        self.assertEqual(len(self.cf.writes), 1)

    def test_unprotected_existing_route_stops_before_writes(self):
        self.cf.config['ingress'].insert(0, {'hostname': 'gateway.johancarlin.com', 'service': 'http://127.0.0.1:4001'})
        with self.assertRaises(g.ProvisionError):
            self.ui().discover()
        self.assertEqual(self.cf.writes, [])


if __name__ == '__main__':
    unittest.main()
