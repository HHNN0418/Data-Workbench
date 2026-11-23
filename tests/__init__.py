"""Regression and integration tests; no competition writes."""
import hashlib
import tempfile
from pathlib import Path

from ctn_data.auth import MEMBERS

# Windows sandbox's redirected system TEMP cannot host SQLite files reliably.
_temporary_root = Path(__file__).resolve().parent.parent / 'data' / 'test-tmp'
_temporary_root.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(_temporary_root)


# Integration tests use isolated databases and never depend on deployed accounts.
MEMBERS.clear()
for index, (password, name) in enumerate((
    ('dashboard-fixture-one', 'Test Member One'),
    ('dashboard-fixture-two', 'Test Member Two'),
    ('dashboard-fixture-three', 'Test Member Three'),
    ('dashboard-fixture-four', 'Test Member Four'),
), start=1):
    salt = index.to_bytes(16, 'big')
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    MEMBERS[password + '@example.com'] = (name, salt.hex(), digest.hex())
