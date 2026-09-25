import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("write_env", ROOT / "scripts/write-deploy-env.py")
writer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(writer)


class EnvTests(unittest.TestCase):
    def test_missing_values_fail_without_secret_output(self):
        with self.assertRaisesRegex(writer.ProvisionError, "ENTRA_TENANT_ID"):
            writer.render({})

    def test_literal_values_and_callback_validation(self):
        env = dict.fromkeys(writer.KEYS, "dummy")
        env.update(WEBUI_URL="https://chat.example.com", OPENID_REDIRECT_URI="https://chat.example.com/oauth/oidc/callback", WEBUI_CLIENT_SECRET="dollar$hash#quote'back\\slash")
        rendered = writer.render(env)
        self.assertIn('WEBUI_CLIENT_SECRET=' + json.dumps(env['WEBUI_CLIENT_SECRET']).replace('$', '$$'), rendered)
        env["OPENID_REDIRECT_URI"] = "http://localhost/callback"
        with self.assertRaisesRegex(writer.ProvisionError, "callback"):
            writer.render(env)


class RemoteDeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "app"
        (self.root / "agentgateway-data").mkdir(parents=True)
        (self.root / "agentgateway-data/log.db").write_text("persistent-data")
        self.bin = Path(self.temp.name) / "bin"
        self.bin.mkdir()
        self.log = Path(self.temp.name) / "commands.jsonl"
        for name in ("docker", "curl"):
            executable = self.bin / name
            executable.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
with open(os.environ['COMMAND_LOG'], 'a') as stream:
    stream.write(json.dumps(sys.argv) + '\\n')
mode = os.environ.get('FAIL_MODE')
if pathlib.Path(sys.argv[0]).name == 'docker':
    if mode == 'config' and 'config' in sys.argv:
        sys.exit(1)
    marker = pathlib.Path(os.environ['DEPLOY_ROOT']) / 'failed-once'
    if mode == 'up' and 'up' in sys.argv and not marker.exists():
        marker.touch()
        sys.exit(1)
else:
    if mode == 'health' and '/health' in sys.argv[-1]:
        sys.exit(22)
    if '/v1/models' in sys.argv[-1]:
        print('401', end='')
''')
            executable.chmod(0o755)
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"], "DEPLOY_ROOT": str(self.root), "COMMAND_LOG": str(self.log)}

    def stage(self):
        directory = Path(tempfile.mkdtemp(prefix="llm-oauth-deploy.", dir="/tmp"))
        self.addCleanup(lambda: __import__('shutil').rmtree(directory, ignore_errors=True))
        for filename in ("compose.yaml", "compose.production.yaml", ".env"):
            (directory / filename).write_text("new-" + filename)
        return directory

    def previous(self):
        for filename in ("compose.yaml", "compose.production.yaml", ".env", "revision"):
            (self.root / filename).write_text("old-" + filename)

    def deploy(self, stage, release="123-1"):
        return subprocess.run(["bash", str(ROOT / "scripts/deploy-remote.sh"), str(stage), "a" * 40, release], env=self.env, capture_output=True, text=True)

    def test_success_and_repeat_keep_separate_backups_and_data(self):
        self.previous()
        stage = self.stage()
        result = self.deploy(stage)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stage.exists())
        self.assertEqual((self.root / "releases/123-1-previous/.env").read_text(), "old-.env")
        self.assertEqual((self.root / "revision").read_text().strip(), "a" * 40)
        result = self.deploy(self.stage(), "123-2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "releases/123-1-previous/.env").read_text(), "old-.env")
        self.assertEqual((self.root / "agentgateway-data/log.db").read_text(), "persistent-data")

    def test_config_failure_does_not_replace_current_configuration(self):
        self.previous()
        self.env["FAIL_MODE"] = "config"
        stage = self.stage()
        result = self.deploy(stage)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / ".env").read_text(), "old-.env")
        self.assertFalse(stage.exists())
        self.assertFalse((self.root / "releases").exists())

    def test_startup_or_health_failure_restores_previous_configuration(self):
        for failure in ("up", "health"):
            with self.subTest(failure=failure):
                self.previous()
                self.env["FAIL_MODE"] = failure
                result = self.deploy(self.stage(), "123-3" if failure == "up" else "123-4")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.root / ".env").read_text(), "old-.env")
                self.assertEqual((self.root / "revision").read_text(), "old-revision")
                self.assertEqual((self.root / "agentgateway-data/log.db").read_text(), "persistent-data")


if __name__ == "__main__":
    unittest.main()

class ComposeIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(__import__('shutil').which('docker'), 'Docker Compose not installed')
    def test_special_characters_survive_real_compose_interpolation(self):
        env = dict.fromkeys(writer.KEYS, 'dummy')
        env.update(WEBUI_URL='https://chat.example.com', OPENID_REDIRECT_URI='https://chat.example.com/oauth/oidc/callback', UPSTREAM_BASE_URL='https://example.com/v1')
        for value in ["dollar${HOME}$hash#quote'double\"back\\slash\\", 'spaces and tabs\tremain']:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                env['WEBUI_CLIENT_SECRET'] = value
                path = Path(directory) / '.env'
                path.write_text(writer.render(env))
                command = ['docker', 'compose', '--env-file', str(path), '-f', str(ROOT / 'compose.yaml'), '-f', str(ROOT / 'compose.production.yaml'), 'config']
                process_env = {k: v for k, v in os.environ.items() if k not in writer.KEYS}
                result = subprocess.run(command + ['--environment'], env=process_env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, 'Compose could not read the synthetic env file')
                actual = next(line.split('=', 1)[1] for line in result.stdout.splitlines() if line.startswith('WEBUI_CLIENT_SECRET='))
                self.assertEqual(actual, value)
                result = subprocess.run(command + ['--format', 'json'], env=process_env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, 'Production Compose validation failed')
                rendered = json.loads(result.stdout)
                self.assertEqual(rendered['services']['openwebui']['environment']['WEBUI_AUTH_COOKIE_SECURE'], 'true')
                for service in rendered['services'].values():
                    for port in service.get('ports', []):
                        self.assertEqual(port['host_ip'], '127.0.0.1')
