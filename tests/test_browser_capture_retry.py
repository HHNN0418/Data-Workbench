import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ctn_data.browser_capture import capture
from ctn_data.db import connect, initialize


class BrowserCaptureRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'capture.db'
        initialize(self.db)
        self.record = {'id': 'retry-record-123', 'kind': 'post',
                       'observed_at': '2026-10-19T01:00:00Z',
                       'data': {'post_id': 1, 'body': 'observed', 'kind': 'post',
                                'visibility': 'public', 'created_at': '2026-10-19T00:00:00Z'}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_locked_record_is_retryable_and_retains_idempotent_receipt(self):
        connection = connect(self.db)

        class LockedOnce:
            failed = False

            def execute(self, sql, *args):
                if sql == 'BEGIN IMMEDIATE' and not self.failed:
                    self.failed = True
                    raise sqlite3.OperationalError('database is locked')
                return connection.execute(sql, *args)

            def __enter__(self):
                connection.__enter__()
                return self

            def __exit__(self, *args):
                return connection.__exit__(*args)

            def close(self):
                connection.close()

        with patch('ctn_data.browser_capture.connect', return_value=LockedOnce()):
            result = capture(self.db, [self.record])
        self.assertEqual(result['accepted'], [])
        self.assertEqual(result['errors'], [{'id': self.record['id'], 'error': 'database is locked', 'retryable': True}])
        for _ in range(2):
            self.assertEqual(capture(self.db, [self.record]), {'accepted': [self.record['id']], 'errors': []})
        conn = connect(self.db)
        try:
            self.assertEqual(conn.execute('SELECT count(*) FROM feed_observations').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT count(*) FROM checkpoints WHERE key=?',
                                          ('browser_receipt:' + self.record['id'],)).fetchone()[0], 1)
        finally:
            conn.close()

    def test_invalid_input_is_explicitly_permanent(self):
        result = capture(self.db, [{**self.record, 'kind': 'unknown'}])
        self.assertEqual(result['accepted'], [])
        self.assertFalse(result['errors'][0]['retryable'])
        self.assertEqual(capture(self.db, [self.record])['accepted'], [self.record['id']])
