#!/usr/bin/env python3
"""Run as root on the VM with containers stopped, to capture/verify disk data."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path('/opt/llm-oauth')
MANIFEST = ROOT / 'snapshot-manifest.json'
DATABASES = [Path('/var/lib/docker/volumes/llm-oauth_openwebui-data/_data/webui.db'),
             ROOT / 'agentgateway-data/data.db']
FILES = DATABASES + [ROOT / n for n in ('.env', 'compose.yaml', 'compose.production.yaml', 'revision')]


def inspect():
    result = {}
    for path in FILES:
        if not path.is_file():
            raise RuntimeError('Snapshot input is missing: ' + str(path))
        if path in DATABASES:
            wal = Path(str(path) + '-wal')
            if wal.exists() and wal.stat().st_size:
                raise RuntimeError('Nonempty SQLite WAL; stop containers and checkpoint first')
            with sqlite3.connect('file:' + str(path) + '?mode=ro&immutable=1', uri=True) as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('SQLite integrity check failed')
        with path.open('rb') as stream:
            result[str(path)] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return result


def main():
    if sys.argv[1] == 'capture':
        for path in DATABASES:
            if not path.is_file():
                raise RuntimeError('Database missing')
            with sqlite3.connect(path) as db:
                if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
                    raise RuntimeError('Database still busy; snapshot aborted')
    actual = inspect()
    if sys.argv[1] == 'capture':
        MANIFEST.write_text(json.dumps(actual, indent=2) + '\n')
        MANIFEST.chmod(0o600)
        print('Captured checksums and verified SQLite integrity for both databases.')
    elif sys.argv[1] == 'verify':
        if actual != json.loads(MANIFEST.read_text()):
            raise RuntimeError('Restored data does not match the pre-snapshot checksums')
        (ROOT / 'snapshot-restore-verified').write_text('Data checksums and SQLite integrity verified before application startup.\n')
        print('Restored data checksums and SQLite integrity match.')
    else:
        raise RuntimeError('Expected capture or verify')


if __name__ == '__main__':
    main()
