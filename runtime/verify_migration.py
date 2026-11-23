"""Read-only deployment checks; never submit a platform action."""
import hashlib
import json
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[1]
report = {}
baseline = json.loads((ROOT / 'data/migration/original-code-sha256.json').read_text())
report['changed_original_files'] = [name for name, digest in baseline.items()
                                    if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest]
assert not report['changed_original_files']
login = {}
for line in (ROOT / 'data/secrets/dashboard_login.txt').read_text().splitlines():
    if ': ' in line:
        key, value = line.split(': ', 1)
        login[key.lower()] = value

with httpx.Client(base_url='http://127.0.0.1:8765', trust_env=False, timeout=90) as client:
    reply = client.get('/api/health')
    reply.raise_for_status()
    report['health'] = reply.json()
    reply = client.get('/api/auth/session')
    reply.raise_for_status()
    token = reply.json()['csrf_token']
    reply = client.post('/api/auth/login', json={'email': login['email'], 'password': login['password']},
                        headers={'Origin': 'http://127.0.0.1:8765', 'X-CTN-Token': token})
    reply.raise_for_status()
    report['existing_dashboard_login'] = True
    print('Original dashboard login: OK', flush=True)
    report['routes'] = {}
    for path in ('/', '/score', '/processing/risk', '/blue', '/api/polls/forecast',
                 '/api/risk/overview', '/api/ai/providers', '/api/agents'):
        reply = client.get(path)
        report['routes'][path] = reply.status_code
        reply.raise_for_status()
        print(path, reply.status_code, flush=True)
        if path == '/api/polls/forecast':
            value = reply.json()
            report['forecast'] = {key: value.get(key) for key in ('status', 'generated_at', 'model')}
        if path == '/api/ai/providers':
            report['providers'] = [{key: provider[key] for key in ('name', 'model', 'has_key')}
                                   for provider in reply.json()['providers']]

original = json.loads((ROOT / 'data/migration/agent_control_v1_agents.original.json').read_text())
design_fields = ('account_id', 'allowed_actions', 'background', 'daily_limit', 'enabled',
                 'freshness_minutes', 'id', 'interests', 'interval_seconds', 'language', 'mode',
                 'name', 'news_freshness_hours', 'notification_interval_seconds', 'persona_revision',
                 'provider_id', 'reading_selection_mode', 'stance', 'username', 'voice')
with psycopg.connect((ROOT / 'data/secrets/postgres_url').read_text().strip()) as database:
    current = json.loads(database.execute("SELECT value FROM meta WHERE key='agent_control_v1:agents'").fetchone()[0])
    assert set(current) == set(original)
    changes = {identifier: [field for field in design_fields if current[identifier].get(field) != item.get(field)]
               for identifier, item in original.items()}
    report['agent_design_changes'] = {key: value for key, value in changes.items() if value}
    assert not report['agent_design_changes'], report['agent_design_changes']
    report['agents'] = len(current)
    report['collection_requests_since_start'] = database.execute(
        'SELECT count(*) FROM api_call_logs WHERE occurred_at >= %s',
        (report['health']['collection']['last_run_at'][:10],)).fetchone()[0]

target = ROOT / 'data/migration/verification.json'
target.write_text(json.dumps(report, ensure_ascii=False, indent=2))
target.chmod(0o600)
print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
