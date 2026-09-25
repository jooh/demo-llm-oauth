import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.bin = self.state / 'bin'
        self.bin.mkdir()
        self.env = {**os.environ, 'DEPLOY_STATE_DIR': str(self.state), 'OPENCODE_AUTH_FILE': str(self.state / 'auth.json'),
                    'PATH': str(self.bin) + os.pathsep + os.environ['PATH']}
        for name in ('WEBUI_SECRET_KEY', 'DEPLOY_SSH_KEY_PATH', 'WEBUI_URL', 'OPENID_REDIRECT_URI'):
            self.env.pop(name, None)
        files = {
            'infra.json': {'serverId': '1', 'sshHostname': 'ssh.example.com'},
            'cloudflare.json': {'serviceTokenClientId': 'test-id', 'serviceTokenClientSecret': 'test-secret'},
            'entra.json': {'webuiClientSecret': 'test-entra-secret'},
            'auth.json': {'opencode-go': {'key': 'test-upstream'}},
        }
        for name, value in files.items():
            (self.state / name).write_text(json.dumps(value))
        (self.state / 'entra.env').write_text('ENTRA_TENANT_ID=test-tenant\nGATEWAY_APP_ID=test-gateway\nWEBUI_CLIENT_ID=test-client\n')
        subprocess.run(['ssh-keygen', '-t', 'ed25519', '-N', '', '-f', str(self.state / 'deploy.key')], check=True, capture_output=True)
        public = (self.state / 'deploy.key.pub').read_text()
        for name in ('known_hosts', 'known_hosts.expected'):
            (self.state / name).write_text('[127.0.0.1]:2222 ' + public)
        gh = self.bin / 'gh'
        gh.write_text(f'#!{sys.executable}\n' + '''
import os, pathlib, sys
state = pathlib.Path(os.environ['DEPLOY_STATE_DIR'])
args = sys.argv[1:]
if args[0] == 'api':
    print('true')
elif args[:2] == ['secret', 'list']:
    if os.environ.get('FAIL_SECRET_LIST'):
        sys.exit(1)
    if (state / 'remote-WEBUI_SECRET_KEY').exists():
        print('WEBUI_SECRET_KEY')
elif args[:2] == ['secret', 'set']:
    (state / ('remote-' + args[2])).write_text(sys.stdin.read())
elif args[:2] == ['variable', 'set']:
    pass
else:
    raise AssertionError(args)
''')
        gh.chmod(0o755)

    def configure(self):
        return subprocess.run(['bash', str(ROOT / 'scripts/configure-github.sh')], env=self.env, capture_output=True, text=True)

    def test_repeated_configuration_keeps_production_key(self):
        first = self.configure()
        self.assertEqual(first.returncode, 0, first.stderr)
        key = (self.state / 'remote-WEBUI_SECRET_KEY').read_bytes()
        second = self.configure()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(key, (self.state / 'remote-WEBUI_SECRET_KEY').read_bytes())
        self.assertEqual((self.state / 'webui-secret-key').stat().st_mode & 0o777, 0o600)

    def test_missing_host_key_stops_before_uploading_any_secrets(self):
        (self.state / 'known_hosts').unlink()
        self.assertNotEqual(self.configure().returncode, 0)
        self.assertEqual(list(self.state.glob('remote-*')), [])

    def test_missing_local_webui_key_does_not_replace_github_key(self):
        (self.state / 'remote-WEBUI_SECRET_KEY').write_text('existing-key')
        result = self.configure()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Restore the existing', result.stderr)
        self.assertEqual((self.state / 'remote-WEBUI_SECRET_KEY').read_text(), 'existing-key')

    def test_failed_secret_listing_does_not_create_or_upload_key(self):
        self.env['FAIL_SECRET_LIST'] = '1'
        self.assertNotEqual(self.configure().returncode, 0)
        self.assertFalse((self.state / 'webui-secret-key').exists())
        self.assertEqual(list(self.state.glob('remote-*')), [])

    def record_host(self):
        for name, script in {
            'cloudflared': '#!/bin/sh\nexec sleep 60\n',
            'ssh-keyscan': '#!/bin/sh\necho "# localhost SSH-2.0-OpenSSH test banner"\ncat "$SCAN_SOURCE"\n',
            'ssh': '#!/bin/sh\nexit 0\n',
        }.items():
            (self.bin / name).write_text(script)
            (self.bin / name).chmod(0o755)
        return subprocess.run(['bash', str(ROOT / 'scripts/record-host-key.sh')], env=self.env, capture_output=True, text=True, timeout=15)

    def test_host_key_matches_provisioned_key(self):
        self.env['SCAN_SOURCE'] = str(self.state / 'known_hosts.expected')
        (self.state / 'known_hosts').unlink()
        result = self.record_host()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.state / 'known_hosts').read_bytes(), (self.state / 'known_hosts.expected').read_bytes())

    def test_host_key_mismatch_preserves_old_record(self):
        old = (self.state / 'known_hosts').read_bytes()
        (self.state / 'mismatch').write_text('[127.0.0.1]:2222 ssh-ed25519 not-the-expected-key\n')
        self.env['SCAN_SOURCE'] = str(self.state / 'mismatch')
        result = self.record_host()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('differs from the key supplied', result.stderr)
        self.assertEqual(old, (self.state / 'known_hosts').read_bytes())
