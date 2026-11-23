"""Add or update a dashboard member in the local credential file."""
from getpass import getpass
from hashlib import scrypt
import json
from secrets import token_bytes

from ctn_data.auth import MEMBERS_PATH, load_dashboard_members
from ctn_data.secret_files import _atomic


def main():
    members = load_dashboard_members(MEMBERS_PATH)
    email = input('Dashboard email: ').strip().lower()
    name = input('Display name: ').strip()
    if '@' not in email or len(email) > 254 or not name:
        raise SystemExit('Enter a valid email and a display name.')
    password = getpass('New dashboard password: ')
    if not 0 < len(password) <= 1024 or password != getpass('Confirm password: '):
        raise SystemExit('Passwords must be nonempty, match and have at most 1024 characters.')

    salt = token_bytes(16)
    digest = scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    members[email] = (name, salt.hex(), digest.hex())
    config = {
        'version': 1,
        'members': {
            address: {'name': member_name, 'salt': member_salt, 'digest': member_digest}
            for address, (member_name, member_salt, member_digest) in members.items()
        },
    }
    _atomic(MEMBERS_PATH, (json.dumps(config, indent=2, ensure_ascii=False) + '\n').encode())
    print('Dashboard member saved. Restart the service to load the change.')


if __name__ == '__main__':
    main()
