from contextlib import closing
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from ctn_data import annotations, queries
from ctn_data.db import connect, initialize


class AnnotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.sqlite3'
        initialize(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def post(self, post_id, body, author=1, reply=None, edited=None):
        with closing(connect(self.path)) as conn, conn:
            conn.execute('''INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen,kind,reply_to_post_id,edited_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET body=excluded.body,edited_at=excluded.edited_at''',
                (post_id, author, body, '2026-10-21T07:00:00Z', '2026-10-21T07:00:00Z', '2026-10-21T07:00:00Z',
                 'reply' if reply else 'post', reply, edited))

    def stored(self):
        with closing(connect(self.path)) as conn:
            return {row['post_id']: dict(row) for row in conn.execute('SELECT * FROM post_annotations')}

    def test_posts_are_annotated_once_and_redone_after_edit_or_rule_change(self):
        self.post(1, 'I am voting no, this mandate is a power grab')
        self.post(2, 'Not sure about the vaccine, worried about side effects', reply=1)
        self.post(3, 'Lovely sunset at the beach today', reply=2)
        self.assertEqual(annotations.refresh_posts(self.path), 3)
        rows = self.stored()
        self.assertEqual([rows[i]['stance'] for i in (1, 2, 3)], ['no', 'undecided', 'unrelated'])
        self.assertIn('safety', json.loads(rows[2]['concerns_json']))
        self.assertEqual([rows[i]['thread_depth'] for i in (1, 2, 3)], [0, 1, 2])
        self.assertEqual(annotations.refresh_posts(self.path), 0)
        self.post(3, 'Vote yes to protect Kingstonia', reply=2, edited='2026-10-21T08:00:00Z')
        self.assertEqual(annotations.refresh_posts(self.path), 1)
        self.assertEqual(self.stored()[3]['stance'], 'yes')
        with patch('ctn_data.annotations.classifier_version', return_value='changed'):
            self.assertEqual(annotations.refresh_posts(self.path), 3)

    def test_pass_is_bounded_and_newest_first(self):
        for post_id in range(1, 8):
            self.post(post_id, 'vote yes')
        with patch.object(annotations, 'BATCH_SIZE', 2):
            self.assertEqual(annotations.refresh_posts(self.path, limit=3), 3)
        self.assertEqual(sorted(self.stored()), [5, 6, 7])
        self.assertEqual(annotations.refresh_posts(self.path), 4)

    def test_bot_flags_follow_current_verdicts(self):
        with patch('ctn_data.agent_targeting.behavior_bots', return_value=frozenset({11, 12})), \
                patch('ctn_data.agent_targeting.duplicate_authors', return_value=frozenset({12})):
            self.assertEqual(annotations.refresh_accounts(self.path), 2)
        with closing(connect(self.path)) as conn:
            rows = {row['account_id']: dict(row) for row in conn.execute('SELECT * FROM account_annotations')}
        self.assertEqual(json.loads(rows[12]['bot_reasons_json']), ['behavior', 'duplicate_text'])
        flagged_at = rows[11]['flagged_at']
        with patch('ctn_data.agent_targeting.behavior_bots', return_value=frozenset({11})), \
                patch('ctn_data.agent_targeting.duplicate_authors', return_value=frozenset()):
            annotations.refresh_accounts(self.path)
        with closing(connect(self.path)) as conn:
            rows = {row['account_id']: dict(row) for row in conn.execute('SELECT * FROM account_annotations')}
        self.assertEqual((rows[11]['is_bot'], rows[11]['flagged_at']), (1, flagged_at))
        self.assertEqual((rows[12]['is_bot'], rows[12]['bot_reasons_json']), (0, '[]'))

    def test_feature_rule_marks_two_features_and_spares_own_accounts(self):
        flags = {21: {'reasons': ['always_on', 'high_volume'], 'metrics': {'active_hours': 23, 'writes_per_day': 90}},
                 22: {'reasons': ['fast_reply'], 'metrics': {'reply_median_s': 40}},
                 5554: {'reasons': ['always_on', 'high_volume'], 'metrics': {'active_hours': 24}}}
        with closing(connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO team_accounts(account_id,team_label,is_own,evidence,updated_at) VALUES(5554,'Agent Zero',1,'t','x')")
        with patch('ctn_data.agent_targeting.behavior_bots', return_value=frozenset()),                 patch('ctn_data.agent_targeting.duplicate_authors', return_value=frozenset({23})),                 patch('ctn_data.agent_targeting.bot_feature_flags', return_value=flags):
            self.assertEqual(annotations.refresh_accounts(self.path), 1)
        with closing(connect(self.path)) as conn:
            rows = {row['account_id']: dict(row) for row in conn.execute('SELECT * FROM account_annotations')}
        self.assertEqual((rows[21]['is_bot'], json.loads(rows[21]['bot_reasons_json'])), (1, ['always_on', 'high_volume']))
        self.assertEqual(json.loads(rows[21]['bot_metrics_json'])['writes_per_day'], 90)
        self.assertEqual((rows[22]['is_bot'], rows[22]['flagged_at']), (0, None))
        self.assertEqual((rows[23]['is_bot'], rows[23]['bot_reasons_json']), (0, '["duplicate_text"]'))
        self.assertEqual(rows[5554]['is_bot'], 0)
        with patch('ctn_data.agent_targeting.behavior_bots', return_value=frozenset()),                 patch('ctn_data.agent_targeting.duplicate_authors', return_value=frozenset()),                 patch('ctn_data.agent_targeting.bot_feature_flags', side_effect=RuntimeError('down')):
            with self.assertRaises(RuntimeError):
                annotations.refresh_accounts(self.path)
        with closing(connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT is_bot FROM account_annotations WHERE account_id=21').fetchone()[0], 1)

    def test_post_reads_expose_annotations(self):
        self.post(1, 'I am voting no', author=11)
        self.post(2, 'not annotated yet', author=12)
        annotations.refresh_posts(self.path, limit=1)
        with patch('ctn_data.agent_targeting.behavior_bots', return_value=frozenset({12})), \
                patch('ctn_data.agent_targeting.duplicate_authors', return_value=frozenset()):
            annotations.refresh_accounts(self.path)
        items = {item['post_id']: item for item in queries.posts(self.path, {'feed': 'all'})['items']}
        self.assertEqual((items[2]['stance'], items[2]['is_bot'], items[2]['bot_reasons']), ('unrelated', True, ['behavior']))
        self.assertEqual((items[1]['stance'], items[1]['concerns'], items[1]['is_bot']), (None, [], False))
        detail = queries.post_detail(self.path, 2)['post']
        self.assertEqual((detail['stance'], detail['is_bot']), ('unrelated', True))
        with patch('ctn_data.agent_targeting.behavior_bots', return_value=frozenset()),                 patch('ctn_data.agent_targeting.duplicate_authors', return_value=frozenset({12})):
            annotations.refresh_accounts(self.path)
        detail = queries.post_detail(self.path, 2)['post']
        self.assertEqual((detail['is_bot'], detail['bot_reasons']), (False, ['duplicate_text']))


if __name__ == '__main__':
    unittest.main()
