#!/usr/bin/env python3
"""Snapshot/delete or restore the dedicated VM. status is read-only; no automatic snapshot deletion."""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import uuid

from provision import ROOT, ProvisionError, private_write, read_state, require, unique


class HetznerAPI:
    def __init__(self):
        self.token = os.environ.get('HCLOUD_TOKEN')
        if not self.token:
            config = tomllib.loads(Path(os.environ.get('HCLOUD_CONFIG', Path.home() / '.config/hcloud/cli.toml')).read_text())
            name = os.environ.get('HCLOUD_CONTEXT', config.get('active_context'))
            context = unique([c for c in config.get('contexts', []) if c.get('name') == name], 'hcloud context')
            require(context and context.get('token'), 'Select an hcloud context or set HCLOUD_TOKEN')
            self.token = context['token']

    def request(self, method, path, body=None):
        req = urllib.request.Request('https://api.hetzner.cloud/v1' + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                data = response.read()
                return json.loads(data) if data else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and method == 'GET':
                return None
            raise ProvisionError(f'Hetzner {method} {path.split("?")[0]} failed: HTTP {exc.code}; rerun status before retrying') from None
        except (urllib.error.URLError, ValueError):
            raise ProvisionError('Hetzner response uncertain; rerun status before retrying') from None

    def get(self, kind, id):
        result = self.request('GET', f'/{kind}/{id}')
        return result.get({'servers': 'server', 'images': 'image', 'firewalls': 'firewall', 'primary_ips': 'primary_ip'}[kind]) if result else None

    def listing(self, kind, query=''):
        result, page = [], 1
        while True:
            data = self.request('GET', f'/{kind}?per_page=50&page={page}' + ('&' + query if query else ''))
            result.extend(data[kind])
            page = data.get('meta', {}).get('pagination', {}).get('next_page')
            if not page:
                return result

    def wait_action(self, action):
        deadline = time.monotonic() + 1800
        while action['status'] == 'running':
            require(time.monotonic() < deadline, 'Timed out waiting for Hetzner action; inspect status and retry')
            time.sleep(5)
            action = self.request('GET', '/actions/' + str(action['id']))['action']
        require(action['status'] == 'success', 'Hetzner action failed; source data retained, inspect status')


class Lifecycle:
    def __init__(self, api, state_dir=None):
        self.api = api
        self.dir = Path(state_dir or os.environ.get('DEPLOY_STATE_DIR', ROOT / '.deploy-state'))
        self.path = self.dir / 'lifecycle.json'
        self.state = read_state(self.path)
        self.infra_path = self.dir / 'infra.json'
        self.infra = read_state(self.infra_path)
        require(self.infra.get('serverId'), 'Provision the VM first; infra.json is missing')

    def save(self):
        private_write(self.path, json.dumps(self.state, indent=2) + '\n')

    def server(self):
        return self.api.get('servers', self.infra['serverId'])

    def validate_server(self, server):
        require(server and server.get('labels', {}).get('owner') == 'llm-oauth', 'VM identity/ownership mismatch')
        require(server['name'] == os.environ.get('HCLOUD_SERVER_NAME', 'llm-oauth'), 'VM name mismatch')
        require(not server.get('volumes') and not server['public_net'].get('floating_ips'),
                'Attached volumes or floating IPs require a separate backup/removal plan')
        fw = self.api.get('firewalls', self.infra['firewallId'])
        require(fw and fw.get('labels', {}).get('owner') == 'llm-oauth' and fw.get('rules') == [], 'Firewall differs')
        require([x['id'] for x in server['public_net'].get('firewalls', [])] == [int(self.infra['firewallId'])], 'VM firewall attachment differs')

    def image(self):
        id = self.state.get('snapshotId')
        image = self.api.get('images', id) if id else None
        require(image and image.get('type') == 'snapshot' and image.get('status') == 'available'
                and image.get('labels', {}).get('owner') == 'llm-oauth'
                and image.get('labels', {}).get('cycle') == self.state['cycle']
                and image.get('created_from', {}).get('id') == self.state['sourceServerId'],
                'No verified, available snapshot for this pause operation; refusing to proceed')
        return image

    def remote(self, command, data=None):
        env = {**os.environ, 'DEPLOY_STATE_DIR': str(self.dir)}
        result = subprocess.run([str(ROOT / 'scripts/vm-ssh.sh'), command], input=data, text=True, env=env)
        require(result.returncode == 0, 'VM command failed; server was not deleted')

    def prepare(self):
        # Do not overlap a manually dispatched deployment.
        proc = subprocess.run(['gh', 'run', 'list', '--repo', 'jooh/demo-llm-oauth', '--workflow', 'Deploy',
                               '--limit', '20', '--json', 'status'], capture_output=True, text=True)
        require(proc.returncode == 0 and all(r['status'] == 'completed' for r in json.loads(proc.stdout)),
                'Cannot confirm deployments are idle; finish/cancel running deployments before pausing')
        self.remote('cd /opt/llm-oauth && docker compose --env-file .env -f compose.yaml -f compose.production.yaml stop --timeout 60 && sudo python3 - capture && sudo sync',
                    (ROOT / 'scripts/snapshot-state.py').read_text())

    def pause(self):
        server = self.server()
        if self.state.get('phase') == 'paused':
            require(server is None, 'Paused state conflicts with an existing server')
            self.image()
            self.cleanup_ips()
            print('Already paused; snapshot retained.', flush=True)
            return
        if self.state.get('phase') not in ('pausing', 'snapshot-ready'):
            require(self.state.get('phase') in (None, 'active'), 'Restore is incomplete; finish restore before pausing')
            self.validate_server(server)
            require(server['status'] == 'running', 'VM must be running before a new pause operation')
            for name in ('deploy.key', 'known_hosts', 'known_hosts.expected', 'cloudflare.json', 'host_ed25519'):
                require((self.dir / name).is_file(), f'Missing recovery credential {name}')
            self.state = {'phase': 'pausing', 'cycle': uuid.uuid4().hex,
                          'sourceServerId': server['id'], 'serverName': server['name'],
                          'serverType': server['server_type']['name'],
                          'location': (server.get('location') or server['datacenter']['location'])['name'],
                          'primaryIpIds': [server['public_net'][v]['id'] for v in ('ipv4', 'ipv6') if server['public_net'].get(v)],
                          'snapshotId': None, 'history': self.state.get('history', [])}
            self.save()
        if server:
            self.validate_server(server)
            require(server['id'] == self.state['sourceServerId'], 'Source VM changed')
            require(not self.state.get('snapshotId') or server['status'] == 'off',
                    'Source VM restarted after snapshot creation; refusing to delete potentially newer data')
            if not self.state.get('prepared') or (server['status'] == 'running' and not self.state.get('snapshotId')):
                require(server['status'] == 'running', 'Preparation incomplete; power on source VM and retry')
                print('Stopping containers and verifying databases...', flush=True)
                self.prepare()
                self.state['prepared'] = True
                self.save()
            if server['status'] != 'off':
                print('Gracefully shutting down VM...', flush=True)
                self.api.wait_action(self.api.request('POST', f'/servers/{server["id"]}/actions/shutdown')['action'])
                deadline = time.monotonic() + 180
                while self.api.get('servers', server['id'])['status'] != 'off':
                    require(time.monotonic() < deadline, 'VM did not shut down; refusing live snapshot')
                    time.sleep(3)
            if not self.state.get('snapshotId'):
                matches = self.api.listing('images', 'type=snapshot&label_selector=cycle=' + self.state['cycle'])
                snapshot = unique(matches, 'pause snapshot')
                if snapshot is None:
                    print('Creating powered-off snapshot...', flush=True)
                    result = self.api.request('POST', f'/servers/{server["id"]}/actions/create_image', {
                        'type': 'snapshot', 'description': 'llm-oauth pause ' + datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        'labels': {'owner': 'llm-oauth', 'cycle': self.state['cycle']}})
                    snapshot = result['image']
                self.state['snapshotId'] = snapshot['id']
                self.save()  # Save before waiting, so a timeout can resume safely.
            deadline = time.monotonic() + 1800
            while self.api.get('images', self.state['snapshotId'])['status'] == 'creating':
                require(time.monotonic() < deadline, 'Snapshot still creating; rerun pause later')
                time.sleep(10)
            image = self.image()
            self.state['phase'] = 'snapshot-ready'
            self.save()
            # Protect the only recovery image from accidental Console deletion.
            self.api.wait_action(self.api.request('POST', f'/images/{image["id"]}/actions/change_protection', {'delete': True})['action'])
            require(self.api.get('servers', server['id'])['status'] == 'off', 'Source VM restarted; refusing deletion')
            print(f'Snapshot {image["id"]} available ({image["image_size"]} GB); deleting source VM...', flush=True)
            self.api.wait_action(self.api.request('DELETE', '/servers/' + str(server['id']))['action'])
        self.image()
        require(self.server() is None, 'VM deletion is not complete')
        self.cleanup_ips()
        self.state['phase'] = 'paused'
        self.save()
        print('Paused: VM and its Primary IPs removed; protected snapshot retained.', flush=True)

    def cleanup_ips(self):
        for id in self.state['primaryIpIds']:
            ip = self.api.get('primary_ips', id)
            if ip:
                require(not ip.get('assignee_id'), 'Saved Primary IP is assigned; refusing deletion')
                self.api.request('DELETE', '/primary_ips/' + str(id))
                require(self.api.get('primary_ips', id) is None, 'Primary IP deletion did not complete')

    def restore_userdata(self):
        # cloud-init must configure the replacement NIC but preserve the pinned host key.
        return '#cloud-config\n' + json.dumps({
            'ssh_deletekeys': False, 'ssh_pwauth': False, 'disable_root': True,
            'write_files': [{'path': '/usr/local/sbin/llm-oauth-verify-snapshot', 'permissions': '0700',
                             'content': (ROOT / 'scripts/snapshot-state.py').read_text()}],
            'runcmd': [['bash', '-ec', 'python3 /usr/local/sbin/llm-oauth-verify-snapshot verify; systemctl enable --now docker cloudflared; cd /opt/llm-oauth; docker compose --env-file .env -f compose.yaml -f compose.production.yaml up -d --wait --wait-timeout 300']]})

    def restore(self):
        require(self.state.get('phase') in ('paused', 'restoring', 'active'), 'No paused snapshot; finish pause first')
        image = self.image()
        if self.state['phase'] == 'active':
            self.validate_server(self.server())
            print('Already restored; no new VM created.', flush=True)
            return
        matches = self.api.listing('servers', 'name=' + self.state['serverName'])
        server = unique(matches, 'restore VM')
        if server:
            require(server.get('labels', {}).get('restore-cycle') == self.state['cycle'] and
                    (server.get('image') or {}).get('id') == image['id'], 'Same-name VM is not this restore; refusing adoption')
        else:
            require(self.server() is None, 'Source VM still exists')
            fw = self.api.get('firewalls', self.infra['firewallId'])
            require(fw and fw.get('rules') == [] and fw.get('labels', {}).get('owner') == 'llm-oauth', 'Firewall differs')
            self.state['phase'] = 'restoring'
            self.save()
            print('Creating replacement VM from snapshot...', flush=True)
            result = self.api.request('POST', '/servers', {
                'name': self.state['serverName'], 'server_type': self.state['serverType'], 'location': self.state['location'],
                'image': image['id'], 'firewalls': [{'firewall': int(self.infra['firewallId'])}],
                'ssh_keys': [int(self.infra['sshKeyId'])],
                'labels': {'owner': 'llm-oauth', 'restore-cycle': self.state['cycle']},
                'user_data': self.restore_userdata(), 'start_after_create': True})
            server = result['server']
            self.state['restoredServerId'] = server['id']
            self.save()
            self.api.wait_action(result['action'])
        self.infra.update(serverId=str(server['id']), snapshotId=str(image['id']))
        private_write(self.infra_path, json.dumps(self.infra, indent=2) + '\n')
        print('Waiting for pinned SSH identity, cloud-init and application health...', flush=True)
        env = {**os.environ, 'DEPLOY_STATE_DIR': str(self.dir)}
        result = subprocess.run([str(ROOT / 'scripts/record-host-key.sh')], env=env)
        require(result.returncode == 0, 'Restore not verified; VM retained for recovery, rerun restore after inspecting it')
        self.remote('sudo test -f /opt/llm-oauth/snapshot-restore-verified && curl -fsS http://127.0.0.1:3000/health && test "$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:4000/v1/models)" = 401')
        self.state['phase'] = 'active'
        self.state['restoredServerId'] = server['id']
        if image['id'] not in self.state['history']:
            self.state['history'].append(image['id'])
        self.save()
        print('Restored and verified. Existing hostnames and GitHub secrets remain valid.', flush=True)

    def status(self):
        server = self.server()
        print('Lifecycle:', self.state.get('phase', 'active (not paused before)'))
        print('VM:', f'{server["id"]} {server["status"]}' if server else 'absent')
        if self.state.get('snapshotId'):
            image = self.image()
            pricing = self.api.request('GET', '/pricing')['pricing']
            rate = pricing['image']['price_per_gb_month']
            size = image['image_size']
            print(f'Snapshot: {image["id"]}, {size} GB; monthly storage ~{float(rate["net"])*size:.4f} {pricing["currency"]} before VAT, ~{float(rate["gross"])*size:.4f} including VAT')
            older = [self.api.get('images', id) for id in self.state.get('history', []) if id != image['id']]
            total = size + sum(i['image_size'] for i in older if i and i.get('image_size'))
            print(f'All recorded retained snapshots: {total:.3f} GB, ~{float(rate["gross"])*total:.4f} {pricing["currency"]}/month including VAT')
        print('Older retained snapshots:', [i for i in self.state.get('history', []) if i != self.state.get('snapshotId')])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('status', 'pause', 'restore'), nargs='?', default='status')
    args = parser.parse_args()
    try:
        lifecycle = Lifecycle(HetznerAPI())
        with (lifecycle.dir / 'lifecycle.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            getattr(lifecycle, args.operation)()
    except (ProvisionError, OSError, KeyError, ValueError) as exc:
        print(str(exc) if isinstance(exc, ProvisionError) else 'Lifecycle failed; inspect saved state and provider status before retrying.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
