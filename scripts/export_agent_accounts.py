"""Export the managed platform accounts (role, username, account ID, password) to a CSV file.

Reads the role list from the live database (BEGIN READ ONLY, rolled back) and
the DPAPI-encrypted files in data/secrets/agents. Decryption only works under
the Windows account that runs CTN. Nothing secret is printed: the console shows
the row count and the output path only. The file is plaintext, so it is written
under data/secrets (git-ignored) with the same restricted ACL as the key files.

    .venv/Scripts/python.exe -X utf8 scripts/export_agent_accounts.py
    .venv/Scripts/python.exe -X utf8 scripts/export_agent_accounts.py --no-passwords
"""
import sys

sys.dont_write_bytecode = True  # keep the run free of side files

import argparse
import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ctn_data import db, postgres  # noqa: E402
from ctn_data.agent_control import PREFIX  # noqa: E402
from ctn_data.db import connect  # noqa: E402
from ctn_data.secret_files import _atomic, read_secret  # noqa: E402


def _refuse_schema_changes(*args, **kwargs):
    raise RuntimeError('read-only script: schema initialization against the live database is not allowed')


# Board rule 6: a newer working-tree SCHEMA must never reach the live database.
db.initialize = postgres.initialize_postgres = _refuse_schema_changes

SECRETS = ROOT / 'data' / 'secrets'
COLUMNS = ('role', 'agent_id', 'username', 'account_id', 'password', 'account_state', 'in_role_list')


def load_roles():
    conn = connect(read_secret(SECRETS / 'postgres_url').strip())
    try:
        conn.execute('BEGIN READ ONLY')
        row = conn.execute('SELECT value FROM meta WHERE key=?', (PREFIX + 'agents',)).fetchone()
    finally:
        conn.rollback()
        conn.close()
    return json.loads(row['value']) if row else {}


def account_rows(roles, with_passwords):
    rows = []
    for path in sorted((SECRETS / 'agents').glob('*.key')):
        if path.stem == 'team':
            continue  # team code, not an account
        try:
            secret = json.loads(read_secret(path))
        except Exception:
            secret = {}
        role = roles.get(path.stem, {})
        rows.append({
            'role': role.get('name', ''),
            'agent_id': path.stem,
            'username': secret.get('username') or role.get('username', ''),
            'account_id': secret.get('account_id') or role.get('account_id', ''),
            'password': secret.get('password', '') if with_passwords else '',
            'account_state': role.get('account_state', '') if role else 'unreadable' if not secret else '',
            'in_role_list': 'yes' if role else 'no',
        })
    rows.sort(key=lambda r: (r['in_role_list'] != 'yes', r['role'], r['username']))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--no-passwords', action='store_true', help='leave the password column empty')
    parser.add_argument('--output', type=Path, help='CSV path (default data/secrets/account-exports/agent-accounts-<UTC>.csv)')
    args = parser.parse_args()
    rows = account_rows(load_roles(), not args.no_passwords)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = args.output or SECRETS / 'account-exports' / f'agent-accounts-{stamp}.csv'
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    # utf-8-sig so Excel shows Chinese role names; _atomic restricts the ACL before writing.
    _atomic(output, buffer.getvalue().encode('utf-8-sig'))
    missing = sum(1 for r in rows if not args.no_passwords and not r['password'])
    print(f'exported {len(rows)} accounts to {Path(output).resolve()}'
          + (f' ({missing} without a saved password)' if missing else ''))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
