import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('lifecycle', ROOT / 'scripts/vm-lifecycle.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class API:
    def __init__(self):
        self.server = {'id': 10, 'name': 'llm-oauth', 'status': 'running', 'labels': {'owner': 'llm-oauth'},
                       'volumes': [], 'public_net': {'firewalls': [{'id': 20}], 'ipv4': {'id': 30}, 'ipv6': {'id': 31}},
                       'server_type': {'name': 'cpx22'}, 'location': {'name': 'hel1'}}
        self.images = {}
        self.ips = {i: {'id': i, 'assignee_id': 10} for i in (30, 31)}
        self.writes = []
        self.bad_image = False
        self.fail_ip = False

    def get(self, kind, id):
        id = int(id)
        if kind == 'servers':
            return copy.deepcopy(self.server) if self.server and self.server['id'] == id else None
        if kind == 'images':
            return copy.deepcopy(self.images.get(id))
        if kind == 'primary_ips':
            return self.ips.get(id)
        if kind == 'firewalls':
            return {'id': 20, 'labels': {'owner': 'llm-oauth'}, 'rules': []}
        raise AssertionError(kind)

    def listing(self, kind, query):
        if kind == 'images':
            return list(self.images.values())
        return [self.server] if self.server else []

    def request(self, method, path, body=None):
        self.writes.append((method, path, body))
        action = {'id': 1, 'status': 'success'}
        if path.endswith('/shutdown'):
            self.server['status'] = 'off'
        elif path.endswith('/create_image'):
            assert self.server['status'] == 'off'
            self.images[40] = {'id': 40, 'status': 'unavailable' if self.bad_image else 'available', 'type': 'snapshot',
                               'labels': body['labels'], 'created_from': {'id': 10}, 'image_size': 5}
            return {'action': action, 'image': copy.deepcopy(self.images[40])}
        elif path.endswith('/change_protection'):
            self.images[40]['protection'] = {'delete': True}
        elif method == 'DELETE' and path.startswith('/servers/'):
            assert self.images[40]['status'] == 'available'
            assert self.images[40]['protection']['delete']
            self.server = None
            for ip in self.ips.values():
                ip['assignee_id'] = None
        elif method == 'DELETE' and path.startswith('/primary_ips/'):
            if self.fail_ip:
                raise m.ProvisionError('injected IP deletion failure')
            del self.ips[int(path.split('/')[-1])]
        elif method == 'POST' and path == '/servers':
            self.server = {'id': 11, 'name': body['name'], 'labels': body['labels'], 'image': {'id': 40},
                           'public_net': {'firewalls': [{'id': 20}]}, 'status': 'running'}
            return {'server': copy.deepcopy(self.server), 'action': action}
        else:
            raise AssertionError((method, path))
        return {'action': action}

    def wait_action(self, action):
        assert action['status'] == 'success'


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        m.private_write(self.dir / 'infra.json', json.dumps({'serverId': '10', 'firewallId': '20', 'sshKeyId': '21', 'sshHostname': 'ssh.example.com'}))
        for name in ('deploy.key', 'known_hosts', 'known_hosts.expected', 'cloudflare.json', 'host_ed25519'):
            m.private_write(self.dir / name, 'private fixture')
        self.api = API()
        self.prepared = 0

    def lifecycle(self):
        x = m.Lifecycle(self.api, self.dir)
        def prepare():
            self.prepared += 1
        x.prepare = prepare
        x.remote = lambda *args: None
        return x

    def test_pause_snapshots_before_delete_and_repeat_is_noop(self):
        self.lifecycle().pause()
        self.assertEqual(self.prepared, 1)
        self.assertIsNone(self.api.server)
        self.assertEqual(self.api.ips, {})
        writes = len(self.api.writes)
        self.lifecycle().pause()
        self.assertEqual(writes, len(self.api.writes))
        self.assertEqual(self.lifecycle().state['phase'], 'paused')

    def test_bad_snapshot_never_deletes_vm_or_ips(self):
        self.api.bad_image = True
        with self.assertRaises(m.ProvisionError):
            self.lifecycle().pause()
        self.assertIsNotNone(self.api.server)
        self.assertEqual(len(self.api.ips), 2)
        self.assertFalse(any(method == 'DELETE' for method, _, _ in self.api.writes))

    def test_ip_cleanup_resumes_after_vm_deletion(self):
        self.api.fail_ip = True
        with self.assertRaises(m.ProvisionError):
            self.lifecycle().pause()
        self.api.fail_ip = False
        self.lifecycle().pause()
        self.assertEqual(self.api.ips, {})
        self.assertEqual(sum(path.endswith('/create_image') for _, path, _ in self.api.writes), 1)

    def test_external_volume_blocks_all_mutations(self):
        self.api.server['volumes'] = [999]
        with self.assertRaises(m.ProvisionError):
            self.lifecycle().pause()
        self.assertEqual(self.api.writes, [])

    def test_restore_retries_without_duplicate_server_and_keeps_host_keys(self):
        self.lifecycle().pause()
        with patch.object(m.subprocess, 'run') as run:
            run.return_value.returncode = 1
            with self.assertRaises(m.ProvisionError):
                self.lifecycle().restore()
            self.assertEqual(self.lifecycle().infra['serverId'], '11')
            run.return_value.returncode = 0
            self.lifecycle().restore()
            self.lifecycle().restore()
        self.assertEqual(sum(path == '/servers' for _, path, _ in self.api.writes), 1)
        body = next(b for _, path, b in self.api.writes if path == '/servers')
        config = json.loads(body['user_data'].split('\n', 1)[1])
        self.assertFalse(config['ssh_deletekeys'])
        command = config['runcmd'][0][2]
        self.assertLess(command.index('verify'), command.index('docker compose'))
        self.assertEqual(self.lifecycle().state['phase'], 'active')

    def test_snapshot_cannot_delete_restarted_source(self):
        self.api.bad_image = True
        with self.assertRaises(m.ProvisionError):
            self.lifecycle().pause()
        self.api.server['status'] = 'running'
        self.api.images[40]['status'] = 'available'
        with self.assertRaisesRegex(m.ProvisionError, 'restarted'):
            self.lifecycle().pause()
        self.assertIsNotNone(self.api.server)

    def test_restore_wont_adopt_unrelated_same_name_server(self):
        self.lifecycle().pause()
        self.api.server = {'id': 50, 'name': 'llm-oauth', 'labels': {}, 'image': {'id': 40}}
        with self.assertRaises(m.ProvisionError):
            self.lifecycle().restore()
        self.assertEqual(self.api.server['id'], 50)


class SnapshotIntegrityTests(unittest.TestCase):
    def test_cloud_init_deprecations_do_not_hide_real_errors(self):
        spec = importlib.util.spec_from_file_location('check_cloud_init', ROOT / 'scripts/check-cloud-init.py')
        c = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(c)
        status = {'status': 'done', 'errors': [], 'recoverable_errors': {'DEPRECATED': ['provider uses old schema']}}
        self.assertTrue(c.healthy(status))
        self.assertFalse(c.healthy({**status, 'errors': ['failed module']}))
        self.assertFalse(c.healthy({**status, 'status': 'running'}))
        self.assertFalse(c.healthy({**status, 'recoverable_errors': {'WARNING': ['failed user setup']}}))

    def test_modified_database_fails_restore_check(self):
        import sqlite3
        spec = importlib.util.spec_from_file_location('snapshot_state', ROOT / 'scripts/snapshot-state.py')
        s = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(s)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'test.db'
            with sqlite3.connect(db) as c:
                c.execute('create table messages (body text)')
                c.execute("insert into messages values ('retained')")
            s.ROOT = Path(tmp)
            s.MANIFEST = Path(tmp) / 'manifest.json'
            s.DATABASES = s.FILES = [db]
            with patch.object(sys, 'argv', ['snapshot-state', 'capture']):
                s.main()
            with patch.object(sys, 'argv', ['snapshot-state', 'verify']):
                s.main()
            with sqlite3.connect(db) as c:
                c.execute("insert into messages values ('changed')")
            with patch.object(sys, 'argv', ['snapshot-state', 'verify']):
                with self.assertRaisesRegex(RuntimeError, 'does not match'):
                    s.main()


if __name__ == '__main__':
    unittest.main()
