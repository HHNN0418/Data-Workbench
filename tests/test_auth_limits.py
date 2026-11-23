from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from ctn_data.auth import AuthStore


class LoginLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AuthStore(Path(self.temp.name) / 'sessions.db')
        self.members = patch('ctn_data.auth.MEMBERS', {
            'one@example.test': ('One', '00', '01'),
            'two@example.test': ('Two', '00', '01'),
        })
        self.members.start()
        self.addCleanup(self.members.stop)
        self.hashing = patch('ctn_data.auth.hashlib.scrypt', side_effect=lambda value, **kw: b'\x01' if value == b'correct' else b'\x02')
        self.hashing.start()
        self.addCleanup(self.hashing.stop)
        self.clock = patch.object(self.store, 'clock', return_value=1000)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def attempt(self, email, client='127.0.0.1', password='wrong'):
        try:
            self.store.login(email, password, client)
        except HTTPException as exc:
            return exc.status_code
        return 200

    def test_one_account_does_not_lock_other_members_on_shared_tunnel(self):
        for _ in range(10):
            self.assertEqual(self.attempt('one@example.test'), 401)
        self.assertEqual(self.attempt(' ONE@EXAMPLE.TEST '), 429)
        self.assertEqual(self.attempt('two@example.test', password='correct'), 200)

    def test_account_limit_prevents_rotating_clients(self):
        for index in range(30):
            self.assertEqual(self.attempt('one@example.test', str(index)), 401)
        self.assertEqual(self.attempt('one@example.test', 'new-client'), 429)
        self.assertEqual(self.attempt('two@example.test', 'new-client', 'correct'), 200)

    def test_unknown_email_spray_does_not_lock_registered_member(self):
        for index in range(40):
            self.assertEqual(self.attempt(f'unknown{index}@example.test'), 401)
        self.assertEqual(self.attempt('unknown-new@example.test'), 429)
        self.assertEqual(self.attempt('two@example.test', password='correct'), 200)
        self.assertLessEqual(len(self.store.failures), 81)

    def test_expiry_and_success_only_reset_relevant_account(self):
        self.assertEqual(self.attempt('two@example.test'), 401)
        self.assertEqual(self.attempt('one@example.test'), 401)
        self.assertEqual(self.attempt('one@example.test', password='correct'), 200)
        self.assertIn(('account', 'two@example.test'), self.store.failures)
        with patch.object(self.store, 'clock', return_value=1301):
            self.assertEqual(self.attempt('two@example.test', password='correct'), 200)
        self.assertFalse(self.store.failures)
