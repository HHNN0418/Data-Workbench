"""Verify the deployed connection test; only the literal OK prompt leaves CTN."""
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

with httpx.Client(base_url='http://127.0.0.1:8765', timeout=50) as client:
    deadline = time.monotonic() + 40
    while True:
        try:
            if client.get('/api/health', timeout=2).status_code == 200:
                break
        except httpx.TransportError:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError('Local CTN service did not recover')
        time.sleep(1)
    session = client.get('/api/auth/session')
    session.raise_for_status()
    token = session.json()['csrf_token']
    login = client.post('/api/auth/login', json={'email': email, 'password': password},
                        headers={'X-CTN-Token': token})
    login.raise_for_status()
    settings = client.get('/api/ai/providers')
    assert settings.status_code == 200, settings.status_code
    data = settings.json()
    selected = next(provider for provider in data['providers'] if provider['id'] == data['default_provider_id'])
    test = client.post('/api/ai/providers/test', json={'provider_id': selected['id']}, headers={'X-CTN-Token': token})
    report = {'provider_list_status': settings.status_code, 'connection_test_status': test.status_code,
              'model': selected['model'], 'thinking_mode': selected['thinking_mode'],
              'max_output_tokens': selected['max_output_tokens'], 'result': test.json(),
              'collected_account_data_sent': False}
    (ROOT / 'reports/generated/ai-compatibility-service-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    assert test.status_code == 200 and test.json().get('ok'), test.status_code
