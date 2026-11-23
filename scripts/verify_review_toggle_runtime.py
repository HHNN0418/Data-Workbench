"""Read-only deployment checks; never prints credentials or writes app state."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ctn_data.agent_control import AgentStore
from ctn_data.db import connect, utc_now
from ctn_data.secret_files import read_secret


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def capture():
    connection = connect(read_secret(ROOT / 'data/secrets/postgres_url').strip())
    try:
        connection.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        store = AgentStore.__new__(AgentStore)
        state = store._load(connection)
        configuration = {'settings': state['settings'],
            'roles': {key: {name: role.get(name) for name in
                ('enabled', 'mode', 'account_id', 'allowed_actions', 'provider_id', 'persona_revision')}
                for key, role in state['agents'].items()},
            'competition': state['rate'].get('competition', {}).get('settings', {})}
        return {'checked_at': utc_now(), 'configuration_sha256': digest(configuration),
                'review_enabled': bool(state['settings'].get('ai_review', {}).get('enabled')),
                'roles': len(state['agents']),
                'database_read_only': connection.execute('SHOW transaction_read_only').fetchone()[0] == 'on'}
    finally:
        connection.rollback()
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('before', 'after'))
    arguments = parser.parse_args()
    directory = ROOT / 'reports/generated'
    baseline = directory / 'agent-review-toggle-before.json'
    result = capture()
    if arguments.phase == 'before':
        baseline.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    else:
        previous = json.loads(baseline.read_text(encoding='utf-8'))
        result['configuration_preserved'] = previous['configuration_sha256'] == result['configuration_sha256']
        with urlopen('http://127.0.0.1:8765/api/health', timeout=10) as response:
            health = json.load(response)
        result['health_ok'] = health.get('ok') is True
        result['review_policy'] = health.get('agent_review_policy')
        index = ROOT / 'frontend/dist/index.html'
        with urlopen('http://127.0.0.1:8765/ai/review', timeout=10) as response:
            result['page_matches_build'] = response.read() == index.read_bytes()
        assets = sorted((ROOT / 'frontend/dist/assets').glob('AIReview-*.js'))
        result['review_assets_match'] = bool(assets)
        for asset in assets:
            with urlopen('http://127.0.0.1:8765/assets/' + asset.name, timeout=10) as response:
                result['review_assets_match'] &= response.read() == asset.read_bytes()
        result['passed'] = all((result['configuration_preserved'], result['database_read_only'],
            result['health_ok'], result['review_policy'] == 2,
            result['page_matches_build'], result['review_assets_match']))
        (directory / 'agent-review-toggle-runtime.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('passed', True) else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({'passed': False, 'error_type': type(error).__name__}))
        sys.exit(1)
