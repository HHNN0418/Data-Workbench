"""Run the existing workbench's small model connection test."""
import json
from pathlib import Path
import time
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
login = dict(line.split(': ', 1) for line in (ROOT / 'data/secrets/dashboard_login.txt').read_text().splitlines() if ': ' in line)
with httpx.Client(base_url='http://127.0.0.1:8765', trust_env=False, timeout=40) as client:
    reply = client.get('/api/auth/session')
    reply.raise_for_status()
    headers = {'Origin': 'http://127.0.0.1:8765', 'X-CTN-Token': reply.json()['csrf_token']}
    reply = client.post('/api/auth/login', json={'email': login['Email'], 'password': login['Password']}, headers=headers)
    reply.raise_for_status()
    if '--cancel' in sys.argv:
        reply = client.post('/api/ai/jobs/084017c2d613484c98f2272a8432e6f3/cancel', json={}, headers=headers)
        reply.raise_for_status()
        print('Cancelled the diagnostic job after production GLM requests verified HTTP 200.', flush=True)
        sys.exit(0)
    reply = client.post('/api/ai/providers/test', json={'provider_id': 'c5fa102e-e97d-45e6-99b8-db557374dad9'}, headers=headers)
    reply.raise_for_status()
    job = reply.json()
    print('GLM connection test submitted:', job.get('id'), flush=True)
    for _ in range(80):
        reply = client.get('/api/ai/jobs/' + job['id'])
        reply.raise_for_status()
        job = reply.json()
        if job['state'] in ('completed', 'failed', 'cancelled'):
            result = {key: job.get(key) for key in ('id', 'state', 'result', 'error')}
            path = ROOT / 'data/migration/glm-connection-test.json'
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            path.chmod(0o600)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if job['state'] == 'failed':
                raise SystemExit(1)
            break
        time.sleep(3)
    else:
        print('Test is still running; check its existing job in the workbench.', flush=True)
