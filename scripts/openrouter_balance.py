"""Read-only OpenRouter balance and burn-rate check. Never prints an API key.

Reads the saved OpenRouter connections from the database (BEGIN READ ONLY,
rolled back) and their keys from data/secrets (or CTN_AI_KEY_<UUID_HEX>), then
calls two free account endpoints: GET /api/v1/credits (account balance) and
GET /api/v1/key (per-key usage). No model request is made and no service state
changes. By default one JSON line (no key) is appended to
reports/generated/openrouter-balance.jsonl so the next run can compute the
burn rate since the previous check.

    .venv/Scripts/python.exe -X utf8 scripts/openrouter_balance.py
    .venv/Scripts/python.exe -X utf8 scripts/openrouter_balance.py --json --no-log

https://openrouter.ai/docs/api-reference/limits (GET /api/v1/key)
"""
import sys

sys.dont_write_bytecode = True  # keep the run free of side files

import argparse
import json
import math
import os
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ctn_data import ai_providers, db, postgres  # noqa: E402
from ctn_data.db import connect  # noqa: E402
from ctn_data.secret_files import read_secret  # noqa: E402


def _refuse_schema_changes(*args, **kwargs):
    raise RuntimeError('read-only script: schema initialization against the live database is not allowed')


# Board rule 6: a newer working-tree SCHEMA must never reach the live database.
db.initialize = postgres.initialize_postgres = _refuse_schema_changes

API = 'https://openrouter.ai/api/v1'
LOG = ROOT / 'reports' / 'generated' / 'openrouter-balance.jsonl'
KEY_FIELDS = ('usage', 'usage_daily', 'usage_weekly', 'usage_monthly', 'limit', 'limit_remaining', 'is_free_tier')


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def connections(url):
    """OpenRouter chat connections from the saved public settings (read only)."""
    conn = connect(url)
    try:
        conn.execute('BEGIN READ ONLY')
        row = conn.execute('SELECT value FROM meta WHERE key=?', (ai_providers.SETTINGS_KEY,)).fetchone()
    finally:
        conn.rollback()
        conn.close()
    providers = json.loads(row['value']).get('providers', []) if row else []
    return [{'id': p['id'], 'name': p.get('name', ''), 'model': p.get('model', ''), 'enabled': p.get('enabled', True)}
            for p in providers if urlsplit(p.get('base_url', '')).hostname == 'openrouter.ai']


def api_key(provider_id):
    key = os.environ.get('CTN_AI_KEY_' + uuid.UUID(provider_id).hex.upper(), '')
    if key:
        return key
    path = ROOT / 'data' / 'secrets' / (provider_id + '.key')
    return ai_providers._unprotect(path.read_bytes()) if path.is_file() else ''


def get(path, key):
    """One GET without proxy inheritance; errors never echo the key or a body."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(API + path, headers={'Authorization': 'Bearer ' + key,
                                                          'Accept': 'application/json'})
    try:
        with opener.open(request, timeout=20) as response:
            return json.load(response).get('data') or {}, None
    except urllib.error.HTTPError as exc:
        return None, 'HTTP %s' % exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, type(exc).__name__


def previous_entry():
    try:
        lines = LOG.read_text(encoding='utf-8').splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if number(entry.get('account_usage')) is not None:
            return entry
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--warn-below', type=float, default=4.0, help='print ALERT below this balance in USD (default 4)')
    parser.add_argument('--json', action='store_true', help='print the full JSON entry')
    parser.add_argument('--no-log', action='store_true', help='do not append to reports/generated/openrouter-balance.jsonl')
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    entry = {'checked_at': iso(now), 'keys': [], 'read_only': True}
    credits = None
    for provider in connections(read_secret(ROOT / 'data/secrets/postgres_url').strip()):
        row = {'connection': provider['name'], 'provider_id': provider['id'], 'model': provider['model'],
               'enabled': provider['enabled']}
        try:
            key = api_key(provider['id'])
        except Exception as exc:  # DPAPI/file problems: report the type only
            key, row['error'] = '', 'key unreadable (%s)' % type(exc).__name__
        if key:
            data, error = get('/key', key)
            if data is not None:
                row.update({field: data.get(field) for field in KEY_FIELDS if field in data})
            else:
                row['error'] = error
            if credits is None:
                data, error = get('/credits', key)
                credits = data if data is not None else {'error': error}
        elif 'error' not in row:
            row['error'] = 'no key saved'
        key = None
        entry['keys'].append(row)

    total_credits = number((credits or {}).get('total_credits'))
    total_usage = number((credits or {}).get('total_usage'))
    if total_credits is not None and total_usage is not None:
        entry.update(account_credits=round(total_credits, 4), account_usage=round(total_usage, 4),
                     balance=round(total_credits - total_usage, 4))
    elif credits and credits.get('error'):
        entry['credits_error'] = credits['error']
    daily = sum(number(row.get('usage_daily')) or 0 for row in entry['keys'])
    entry['usage_daily_all_keys'] = round(daily, 4)
    hours_today = (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 3600
    if hours_today >= .5:
        entry['rate_today_per_hour'] = round(daily / hours_today, 4)
    previous = previous_entry()
    if previous and number(entry.get('account_usage')) is not None:
        hours = (now - datetime.fromisoformat(previous['checked_at'].replace('Z', '+00:00'))).total_seconds() / 3600
        if hours >= .25:
            entry['rate_since_last_per_hour'] = round((entry['account_usage'] - previous['account_usage']) / hours, 4)
            entry['since_last_hours'] = round(hours, 2)
    rate = entry.get('rate_since_last_per_hour') or entry.get('rate_today_per_hour')
    if number(entry.get('balance')) is not None and rate and rate > 0:
        hours_left = entry['balance'] / rate
        entry['hours_left'] = round(hours_left, 1)
        entry['runs_out_at'] = iso(datetime.fromtimestamp(now.timestamp() + hours_left * 3600, timezone.utc))

    if not args.no_log:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open('a', encoding='utf-8') as target:
            target.write(json.dumps(entry, ensure_ascii=False) + '\n')

    balance = entry.get('balance')
    if balance is not None and balance < args.warn_below:
        print('ALERT balance $%.2f is below $%.2f' % (balance, args.warn_below))
    print('OpenRouter %s  balance %s  (credits %s, used %s)' % (
        entry['checked_at'], '$%.2f' % balance if balance is not None else 'unknown (' + entry.get('credits_error', 'n/a') + ')',
        entry.get('account_credits'), entry.get('account_usage')))
    print('  today (UTC) all keys $%.2f; rate today $%s/h; since last check $%s/h over %sh' % (
        daily, entry.get('rate_today_per_hour'), entry.get('rate_since_last_per_hour'), entry.get('since_last_hours')))
    if 'hours_left' in entry:
        print('  at that rate: %.1f h left, runs out about %s' % (entry['hours_left'], entry['runs_out_at']))
    for row in entry['keys']:
        print('  key %-12s %-36s usage $%s, today $%s%s' % (
            row['connection'], row['model'], row.get('usage'), row.get('usage_daily'),
            ', error ' + row['error'] if row.get('error') else ''))
    if args.json:
        print(json.dumps(entry, ensure_ascii=False, indent=2))
    if not args.no_log:
        print('Appended', LOG.relative_to(ROOT))


if __name__ == '__main__':
    main()
