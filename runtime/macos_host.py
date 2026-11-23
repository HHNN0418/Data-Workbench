"""macOS hosting only; invoke the bundled CTN application without business patches."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / 'data' / 'mac'
STATE = CONTROL / 'service.json'
sys.path.insert(0, str(ROOT))


def read_state():
    if not STATE.exists():
        return {}
    value = json.loads(STATE.read_text())
    if (type(value.get('pid')) is not int or value['pid'] < 2
            or len(value.get('instance', '')) != 32):
        raise ValueError('Invalid local service state')
    return value


def owned(value):
    if not value:
        return False
    result = subprocess.run(['ps', '-p', str(value['pid']), '-o', 'args='],
                            capture_output=True, text=True)
    return (str(Path(__file__).resolve()) in result.stdout
            and '--instance ' + value['instance'] in result.stdout)


def health(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'http://127.0.0.1:{port}/api/health', timeout=3) as reply:
        return json.load(reply)


def status():
    value = read_state()
    if not owned(value):
        return {'running': False, 'state': 'stopped', 'root': str(ROOT)}
    try:
        ready = bool(health(value['port']).get('ok'))
    except (OSError, ValueError):
        ready = False
    return {**value, 'running': True, 'state': 'running' if ready else 'starting_or_unhealthy',
            'url': f'http://127.0.0.1:{value["port"]}/', 'root': str(ROOT)}


def run(args):
    from dotenv import dotenv_values
    for key, value in dotenv_values(ROOT / '.env', interpolate=False).items():
        if isinstance(value, str):
            os.environ.setdefault(key, value)
    os.umask(0o077)
    from ctn_data.secret_files import read_secret
    from ctn_data.server import load_config, serve
    database = read_secret(ROOT / 'data' / 'secrets' / 'postgres_url').strip()
    serve(database, load_config(ROOT / 'config.example.json'), args.port, True)


def start(args):
    current = status()
    if current['running']:
        return current
    subprocess.run(['docker', 'compose', '-p', 'ctn-latest', '-f', 'compose.yaml',
                    '-f', 'compose.macos.yaml', 'up', '-d', '--wait', 'postgres'],
                   cwd=ROOT, check=True)
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', args.port)) == 0:
            raise RuntimeError(f'Port {args.port} is in use')
    instance = secrets.token_hex(16)
    logs = ROOT / 'data' / 'logs'
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = logs / 'mac-backend.log'
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'ab') as stream:
        child = subprocess.Popen([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()),
                                  'run', '--port', str(args.port), '--instance', instance],
                                 cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stream,
                                 stderr=subprocess.STDOUT, start_new_session=True, umask=0o077)
    value = {'pid': child.pid, 'instance': instance, 'port': args.port,
             'started_at': time.time(), 'auto_collect': True, 'log_file': str(path)}
    STATE.write_text(json.dumps(value))
    STATE.chmod(0o600)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f'Backend exited; inspect {path}')
        try:
            if health(args.port).get('ok'):
                return status()
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    raise RuntimeError(f'Backend remains starting; inspect {path}')


def stop():
    value = read_state()
    if not owned(value):
        return {'running': False, 'state': 'stopped'}
    os.kill(value['pid'], signal.SIGTERM)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if not owned(value):
            return {'running': False, 'state': 'stopped'}
        time.sleep(0.25)
    return {'running': True, 'state': 'stopping', 'pid': value['pid']}


def main():
    os.chdir(ROOT)
    parser = argparse.ArgumentParser(description='Host the unmodified latest CTN on macOS')
    parser.add_argument('command', choices=('start', 'stop', 'status', 'run'))
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--instance')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Invalid port')
    if args.command == 'run':
        if not args.instance:
            parser.error('run requires --instance')
        run(args)
        return
    CONTROL.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (CONTROL / 'control.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = start(args) if args.command == 'start' else stop() if args.command == 'stop' else status()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
