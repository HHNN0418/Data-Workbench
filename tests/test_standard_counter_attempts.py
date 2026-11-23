"""Counter attempt backoff semantics across missing and malformed legacy state."""
import json

from ctn_data.collector import _config, _standard_counter_schedule
from ctn_data.ingest import save_posts
from tests.test_collection import BaseDatabase


class StandardCounterAttemptTests(BaseDatabase):
    def test_backoff_reuses_the_joined_attempt_without_changing_eligibility(self):
        now = '2026-10-19T12:00:00Z'
        cases = (
            (None, None, True),
            ('{broken', '2026-10-19T11:59:00Z', True),
            ('null', '2026-10-19T11:59:00Z', True),
            ({}, '2026-10-19T11:59:00Z', True),
            ({'status': 'failed'}, '2026-10-19T11:56:00Z', False),
            ({'status': 'failed'}, '2026-10-19T11:55:00Z', True),
            ({'status': 'failed'}, 'invalid-time', True),
            ({'status': 'failed', 'http_status': 403}, '2026-10-19T11:54:00Z', False),
            ({'status': 'saved', 'http_status': 404}, '2026-10-19T11:46:00Z', False),
            ({'http_status': '410'}, '2026-10-19T11:46:00Z', False),
            ({'status': 'failed', 'http_status': 404}, '2026-10-19T11:45:00Z', True),
            ({'status': 'saved', 'http_status': 'invalid'}, '2026-10-19T11:59:00Z', True),
            ({'status': 'failed'}, '2026-10-19T22:56:00+11:00', False),
            ({'status': 'failed'}, '2026-10-19T22:54:00+11:00', True),
            ({'http_status': 404}, '2026-10-19T22:46:00+11:00', False),
            ({'http_status': 404}, '2026-10-19T22:44:00+11:00', True),
        )
        expected = set()
        for post_id, (value, at, eligible) in enumerate(cases, 1):
            save_posts(self.conn, [{'post_id': post_id, 'body': 'fixture'}],
                       'fixture', '2026-10-19T10:00:00Z')
            if value is not None:
                self.conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)',
                                  ('counter_attempt:' + str(post_id),
                                   value if isinstance(value, str) else json.dumps(value), at))
            if eligible:
                expected.add(post_id)
        self.conn.commit()
        cfg = _config({'counter_limit': 100, 'counter_new_share': 1,
                       'counter_recent_share': 0, 'counter_window_share': 0})
        for recent_share in (0, .5):
            configured = {**cfg, 'counter_recent_share': recent_share}
            selected = _standard_counter_schedule(self.conn, configured, now=now)
            self.assertEqual({row['post_id'] for row in selected}, expected)
            shifted = _standard_counter_schedule(self.conn, configured,
                                                 now='2026-10-19T23:00:00+11:00')
            self.assertEqual(shifted, selected)
