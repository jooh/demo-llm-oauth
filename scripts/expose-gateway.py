#!/usr/bin/env python3
"""Publish the gateway UI through an email-protected Cloudflare Tunnel route."""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import sys

from provision import Cloudflare, ProvisionError, ROOT, private_write, require, unique


class GatewayUI:
    def __init__(self, env, cf):
        self.env, self.cf = env, cf
        for name in ('CLOUDFLARE_ACCOUNT_ID', 'CLOUDFLARE_ZONE_ID', 'CLOUDFLARE_ACCESS_EMAIL'):
            require(env.get(name), f'{name} is required')
        self.account = '/accounts/' + env['CLOUDFLARE_ACCOUNT_ID']
        self.zone = '/zones/' + env['CLOUDFLARE_ZONE_ID']
        self.hostname = env.get('GATEWAY_UI_HOSTNAME', 'gateway.johancarlin.com')
        self.tunnel_id = env.get('CLOUDFLARE_TUNNEL_ID', '7f74c6f3-49cb-4195-96e5-dcbd3a861b87')
        self.config_path = self.account + '/cfd_tunnel/' + self.tunnel_id + '/configurations'
        self.state_dir = Path(env.get('DEPLOY_STATE_DIR', ROOT / '.deploy-state'))

    def email_policy(self, policy):
        return (policy.get('decision') == 'allow'
                and policy.get('include') == [{'email': {'email': self.env['CLOUDFLARE_ACCESS_EMAIL']}}])

    def check_app(self, app):
        require(app.get('type') == 'self_hosted' and app.get('domain') == self.hostname,
                'Existing gateway Access application differs')
        require(not app.get('allowed_idps') or self.otp_ids.intersection(app['allowed_idps']),
                'Gateway Access application does not allow OTP')
        policies = self.cf.listing(self.account + '/access/apps/' + app['id'] + '/policies')
        require(len(policies) == 1 and self.email_policy(policies[0]),
                'Gateway UI must have exactly one email-only Allow policy; review existing policies')
        require(app.get('aud'), 'Gateway Access application audience is missing')

    def expected_route(self, app):
        return {'hostname': self.hostname, 'service': 'http://127.0.0.1:4001',
                'originRequest': {'access': {'required': True, 'teamName': self.team,
                                             'audTag': [app['aud']]}}}

    def discover(self):
        zone = self.cf.get(self.zone)
        require(zone['account']['id'] == self.env['CLOUDFLARE_ACCOUNT_ID'], 'Zone account mismatch')
        require(zone['name'] == self.env.get('CLOUDFLARE_ZONE', 'johancarlin.com'), 'Zone name mismatch')
        require(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.' + re.escape(zone['name']), self.hostname),
                'Gateway UI hostname must be one subdomain in the configured zone')
        require(self.hostname not in (self.env.get('CHAT_HOSTNAME', 'chat.johancarlin.com'),
                                      self.env.get('SSH_HOSTNAME', 'ssh.johancarlin.com')),
                'Gateway hostname must differ from chat and SSH')
        tunnel = self.cf.get(self.account + '/cfd_tunnel/' + self.tunnel_id)
        require(tunnel.get('config_src') == 'cloudflare' and not tunnel.get('deleted_at')
                and tunnel.get('name') == self.env.get('CLOUDFLARE_TUNNEL_NAME', 'demo-llm-oauth'),
                'Unexpected tunnel identity or configuration source')
        org = self.cf.get(self.account + '/access/organizations')
        domain = org.get('auth_domain', '')
        require(domain.endswith('.cloudflareaccess.com'), 'Access team domain is missing')
        self.team = domain.removesuffix('.cloudflareaccess.com')
        self.otp_ids = {i['id'] for i in self.cf.listing(self.account + '/access/identity_providers')
                        if i.get('type') == 'onetimepin'}
        require(self.otp_ids, 'OTP identity provider is missing')
        apps = self.cf.listing(self.account + '/access/apps')
        self.app = unique([a for a in apps if a.get('domain') == self.hostname], 'gateway Access application')
        self.policy = None
        if self.app:
            self.app = self.cf.get(self.account + '/access/apps/' + self.app['id'])
            self.check_app(self.app)
        else:
            chat = unique([a for a in apps if a.get('domain') == self.env.get('CHAT_HOSTNAME', 'chat.johancarlin.com')], 'chat Access application')
            require(chat, 'Chat Access application is missing')
            self.policy = unique([p for p in self.cf.listing(self.account + '/access/apps/' + chat['id'] + '/policies')
                                  if self.email_policy(p) and p.get('reusable')], 'reusable email-only policy')
            require(self.policy, 'Chat has no reusable email-only policy')
        self.config = self.cf.get(self.config_path)['config']
        ingress = self.config.get('ingress', [])
        require(ingress and ingress[-1] == {'service': 'http_status:404'},
                'Expected a final 404 catch-all; review tunnel ingress before adding UI')
        self.route = unique([r for r in ingress if r.get('hostname') == self.hostname], 'gateway tunnel route')
        if self.route:
            require(self.app and self.route == self.expected_route(self.app),
                    'Existing gateway route differs or lacks Access validation; review it')
        self.dns = unique(self.cf.listing(self.zone + '/dns_records?name=' + self.hostname), 'gateway DNS record')
        if self.dns:
            require(self.dns.get('type') == 'CNAME' and self.dns.get('proxied') is True
                    and self.dns.get('content', '').rstrip('.') == self.tunnel_id + '.cfargotunnel.com',
                    'Existing gateway DNS differs; refusing to overwrite it')
        for value, label in [(self.app, 'email-only Access application'), (self.route, 'Access-validated tunnel route'),
                             (self.dns, 'proxied CNAME')]:
            print(('REUSE  ' if value else 'CREATE ') + self.hostname + ': ' + label)

    def apply(self):
        if not self.app:
            self.app = self.cf.request('POST', self.account + '/access/apps', {
                'name': 'Agentgateway UI', 'type': 'self_hosted', 'domain': self.hostname,
                'session_duration': '24h', 'allowed_idps': sorted(self.otp_ids),
                'policies': [{'id': self.policy['id'], 'precedence': 1}],
            })['result']
            private_write(self.state_dir / 'gateway-access.json', json.dumps(self.app, indent=2) + '\n')
        self.app = self.cf.get(self.account + '/access/apps/' + self.app['id'])
        self.check_app(self.app)  # Protection must exist before the origin is routed.
        if not self.route:
            current = self.cf.get(self.config_path)['config']
            require(current == self.config, 'Tunnel configuration changed; rerun plan before applying')
            private_write(self.state_dir / 'gateway-tunnel-before.json', json.dumps(current, indent=2) + '\n')
            updated = copy.deepcopy(current)
            updated['ingress'].insert(-1, self.expected_route(self.app))
            self.cf.request('PUT', self.config_path, {'config': updated})
            require(self.cf.get(self.config_path)['config'] == updated, 'Tunnel configuration verification failed')
        if not self.dns:
            # DNS is last: neither transient nor partial failure may expose an unprotected UI.
            self.cf.request('POST', self.zone + '/dns_records', {
                'type': 'CNAME', 'name': self.hostname, 'content': self.tunnel_id + '.cfargotunnel.com',
                'proxied': True, 'ttl': 1,
            })
        print('Gateway UI configured: https://' + self.hostname + '/ui/llm/logs')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('plan', 'apply'), nargs='?', default='plan')
    args = parser.parse_args()
    try:
        require(os.environ.get('CLOUDFLARE_API_TOKEN'), 'CLOUDFLARE_API_TOKEN is required')
        ui = GatewayUI(os.environ, Cloudflare(os.environ['CLOUDFLARE_API_TOKEN']))
        ui.discover()
        if args.mode == 'apply':
            ui.apply()
    except (ProvisionError, OSError, KeyError, ValueError):
        exc = sys.exc_info()[1]
        print(str(exc) if isinstance(exc, ProvisionError) else 'Invalid provider response or local state', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
