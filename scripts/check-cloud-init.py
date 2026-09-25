#!/usr/bin/env python3
"""Require completed cloud-init; tolerate only explicitly classified deprecations."""
import json
import subprocess
import sys


def healthy(status):
    warnings = status.get('recoverable_errors', {})
    return (status.get('status') == 'done' and status.get('errors') == []
            and isinstance(warnings, dict) and not (set(warnings) - {'DEPRECATED'}))


def main():
    wait = subprocess.run(['cloud-init', 'status', '--wait'], capture_output=True, timeout=600)
    status = subprocess.run(['cloud-init', 'status', '--format', 'json'], capture_output=True, text=True)
    payload = json.loads(status.stdout)
    if wait.returncode not in (0, 2) or status.returncode not in (0, 2) or not healthy(payload):
        sys.exit('cloud-init did not complete cleanly; inspect cloud-init status --long on the VM')
    if payload.get('recoverable_errors'):
        print('cloud-init completed with deprecation notices only; no failed modules.')


if __name__ == '__main__':
    main()
