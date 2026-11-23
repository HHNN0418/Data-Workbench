"""Verify the local authenticated API without model calls."""
import json
from pathlib import Path
import sys
import time
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from getpass import getpass

email = input('Dashboard email: ').strip()
password = getpass('Dashboard password: ')

with httpx.Client(base_url='http://127.0.0.1:8765', timeout=20) as client:
    deadline = time.monotonic() + 40
    while True:
        try:
            health = client.get('/api/health')
            if health.status_code == 200:
                break
        except httpx.TransportError:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError('CTN health did not recover within 40 seconds')
        time.sleep(1)
    session = client.get('/api/auth/session')
    session.raise_for_status()
    token = session.json()['csrf_token']
    login = client.post('/api/auth/login', json={'email': email, 'password': password},
                        headers={'X-CTN-Token': token})
    login.raise_for_status()
    schema = client.get('/openapi.json').json()
    for route in ('/api/ai/account-screening', '/api/ai/account-review'):
        assert route in schema['paths']
    scan = client.post('/api/ai/account-screening', json={'limit': 50}, headers={'X-CTN-Token': token})
    assert scan.status_code == 200, scan.text
    result = {'health_status': health.status_code, 'scan_status': scan.status_code,
              'summary': scan.json()['summary'], 'model_calls': 0,
              'top_accounts': [{key: row[key] for key in ('account_id', 'score', 'band')} for row in scan.json()['items'][:3]]}
    (ROOT / 'reports/generated/ai-screening-service-smoke.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
