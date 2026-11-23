"""Validate and switch the existing GLM connection through the workbench API."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ctn_data.ai_providers import _unprotect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('model')
    args = parser.parse_args()
    env = ROOT / '.env'
    previous = env.read_text()
    updated, count = re.subn(r'(?m)^GLM_MODEL=.*$', lambda match: 'GLM_MODEL=' + args.model, previous)
    if count != 1:
        raise RuntimeError('Expected exactly one GLM_MODEL setting; no changes made')
    login = dict(line.split(': ', 1) for line in (ROOT / 'data/secrets/dashboard_login.txt').read_text().splitlines() if ': ' in line)
    base = 'http://127.0.0.1:8765'
    with httpx.Client(base_url=base, trust_env=False, timeout=60) as client:
        response = client.get('/api/auth/session'); response.raise_for_status()
        headers = {'Origin': base, 'X-CTN-Token': response.json()['csrf_token']}
        response = client.post('/api/auth/login', json={'email': login['Email'], 'password': login['Password']}, headers=headers)
        response.raise_for_status()
        response = client.get('/api/ai/providers'); response.raise_for_status()
        registry = response.json()
        provider = next(item for item in registry['providers'] if item['id'] == registry['default_provider_id'])
        if urlsplit(provider['base_url']).hostname != 'open.bigmodel.cn':
            raise RuntimeError('Default provider is not the existing GLM connection')
        key = _unprotect((ROOT / 'data/secrets' / (provider['id'] + '.key')).read_bytes())
        print('Validating requested GLM model with a small request...', flush=True)
        response = client.post(provider['base_url'].rstrip('/') + '/chat/completions',
                               headers={'Authorization': 'Bearer ' + key},
                               json={'model': args.model, 'messages': [{'role': 'user', 'content': 'Reply OK.'}],
                                     'max_tokens': 64, 'stream': False})
        print('Requested model validation HTTP status:', response.status_code, flush=True)
        response.raise_for_status()
        response = client.post('/api/ai/providers/save', headers=headers,
                               json={'id': provider['id'], 'name': provider['name'],
                                     'base_url': provider['base_url'], 'model': args.model})
        response.raise_for_status()
        descriptor, temporary = tempfile.mkstemp(prefix='.model-', dir=ROOT)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(updated); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, env)
        finally:
            Path(temporary).unlink(missing_ok=True)
        response = client.get('/api/ai/providers'); response.raise_for_status()
        saved = next(item for item in response.json()['providers'] if item['id'] == provider['id'])
        assert saved['model'] == args.model
        response = client.get('/api/health'); response.raise_for_status()
        health = response.json()
        print(json.dumps({'previous_model': provider['model'], 'current_model': saved['model'],
                          'provider_id_preserved': True, 'api_key_unchanged': True,
                          'environment_synchronized': True, 'backend_ok': health['ok'],
                          'collection_active': health['collection']['active']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
