"""Local credential storage and ACL-restricted Docker runtime projections.

Windows canonical files use current-user DPAPI. Docker must receive plaintext
inside /run/secrets; only those five files are materialized under runtime-secrets.
Legacy plaintext is readable until the explicit migration command is applied.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile

NAMES = ('postgres_url', 'postgres_password', 'worker_token', 'nord_username',
         'nord_password', 'gluetun-control.toml')
DOCKER_NAMES = tuple(name for name in NAMES if name != 'postgres_url')
HEADER = b'DPAPI1\n'
# Created by the tunnel bridge and verified by the backend, so only the bridge
# can name a public client for login rate limiting.
TUNNEL_BRIDGE_TOKEN = Path(__file__).resolve().parent.parent / 'data' / 'secrets' / 'tunnel_bridge_token'


def read_secret(path):
    raw = Path(path).read_bytes()
    if raw.startswith(HEADER):
        if os.name != 'nt':
            raise ValueError('Windows protected credentials require the original Windows account')
        from .ai_providers import _unprotect
        try:
            return _unprotect(raw)
        except Exception:
            raise ValueError('Unable to decrypt local credentials for this service account') from None
    return raw.decode('utf-8-sig')


def restrict_access(path):
    """Fail closed if permissions cannot be restricted before writing secrets."""
    path = Path(path).resolve()
    if os.name == 'nt':
        script = Path(__file__).resolve().parent.parent / 'scripts' / 'Set-CTNSecretAcl.ps1'
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-File', str(script), '-Path', str(path)],
            capture_output=True, timeout=30, check=False)
        if result.returncode:
            raise OSError('Unable to restrict credential file permissions')
    else:
        path.chmod(0o700 if path.is_dir() else 0o600)


def _directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    restrict_access(path)
    return path


def _atomic(path, raw):
    path = Path(path)
    _directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix='.secret-', dir=path.parent)
    try:
        os.close(descriptor)
        restrict_access(temporary)
        with open(temporary, 'wb') as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_secret(path, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Credentials must not be empty')
    if os.name != 'nt':
        raise ValueError('Encrypted credential provisioning requires Windows DPAPI')
    from .ai_providers import _protect, _unprotect
    protected = _protect(value)
    if not protected.startswith(HEADER) or _unprotect(protected) != value:
        raise ValueError('Credential encryption verification failed')
    _atomic(path, protected)


def prepare(root):
    root = Path(root).resolve()
    source = root / 'data' / 'secrets'
    if not source.is_dir():
        return 0
    restrict_access(source)
    target = _directory(root / 'data' / 'runtime-secrets')
    count = 0
    for name in DOCKER_NAMES:
        path = source / name
        if not path.is_file():
            continue
        raw = read_secret(path).encode('utf-8')
        if not raw.strip():
            raise ValueError('A configured Docker credential is empty')
        destination = target / name
        if not destination.is_file() or destination.read_bytes() != raw:
            _atomic(destination, raw)
        else:
            restrict_access(destination)
        count += 1
    return count


def migrate(root):
    """Create verified encrypted recovery copies before replacing legacy files."""
    if os.name != 'nt':
        raise ValueError('Credential migration requires Windows DPAPI')
    root = Path(root).resolve()
    source = _directory(root / 'data' / 'secrets')
    # Restrict old files (including existing Agent/provider keys) as well.
    for path in sorted(source.rglob('*')):
        if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
            raise ValueError('Credential directory contains an unexpected linked path')
        restrict_access(path)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = source / 'encrypted-backups' / stamp
    count = 0
    for name in NAMES:
        path = source / name
        if not path.is_file() or path.read_bytes().startswith(HEADER):
            continue
        value = read_secret(path)
        write_secret(backup / name, value)
        if read_secret(backup / name) != value:
            raise ValueError('Credential backup verification failed')
        write_secret(path, value)
        count += 1
    prepare(root)
    return count


def restore(root, backup):
    """Restore canonical encrypted files from an explicit in-tree recovery copy."""
    source = Path(root).resolve() / 'data' / 'secrets'
    backup = Path(backup).resolve()
    if not backup.is_relative_to((source / 'encrypted-backups').resolve()) or not backup.is_dir():
        raise ValueError('Recovery directory must be an encrypted credential backup')
    count = 0
    for name in NAMES:
        if (backup / name).is_file():
            write_secret(source / name, read_secret(backup / name))
            count += 1
    prepare(root)
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description='Protect local CTN credentials without displaying their contents')
    parser.add_argument('command', choices=('prepare', 'migrate', 'restore', 'set'))
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--name', choices=NAMES)
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            count = prepare(args.root)
        elif args.command == 'migrate':
            count = migrate(args.root)
        elif args.command == 'restore':
            if args.backup is None:
                parser.error('restore requires --backup')
            count = restore(args.root, args.backup)
        else:
            if args.name is None:
                parser.error('set requires --name')
            write_secret(args.root / 'data' / 'secrets' / args.name, sys.stdin.read().rstrip('\r\n'))
            count = 1
        print(f'{args.command}: {count} credential files processed')
        return 0
    except Exception as exc:
        # Never interpolate exceptions that might contain connection strings.
        print(f'Credential operation failed ({type(exc).__name__}); original files and verified recovery copies are retained.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
