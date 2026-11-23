"""Confirmed actor receipts join public analytics without fabricated evidence."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ctn_data.agent_tracking import record_account, record_success
from ctn_data.db import connect, initialize
from ctn_data.ingest import save_accounts, save_counters, save_posts


AT = '2026-10-20T02:00:00Z'
LATER = '2026-10-20T02:01:00Z'


class AgentTrackingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.path = Path(self.directory.name) / 'tracking.sqlite3'
        initialize(self.path)
        self.conn = connect(self.path)
        self.agent = {'id': 'maya', 'account_id': 42, 'name': 'Invented display', 'username': 'local_setting'}
        self.draft = {'id': 'operation', 'agent_id': 'maya', 'account_id': 42, 'action': 'post',
                      'post_id': 100, 'body': 'A considered response.', 'sources': []}
        self.data = {'settings': {}, 'agents': {'maya': self.agent}, 'drafts': {'operation': self.draft}}

    def tearDown(self):
        self.conn.close()
        self.directory.cleanup()

    def track(self, remote=None, at=AT):
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            return record_success(self.data, self.conn, self.draft, self.agent, remote, at)

    def rows(self, table):
        return [dict(row) for row in self.conn.execute('SELECT * FROM ' + table)]

    def target(self, post_id=7, **kwargs):
        with self.conn:
            save_posts(self.conn, [{'post_id': post_id, 'author_id': 9, 'kind': 'post',
                'body': 'A real observed discussion', 'created_at': AT, **kwargs}], 'latest', AT)

    def test_confirmed_post_joins_posts_versions_team_and_shared_sampling(self):
        result = self.track({'post_id': 100, 'author_id': 42, 'kind': 'post',
                             'body': self.draft['body'], 'created_at': AT, 'visibility': 'public'})
        post = self.rows('posts')[0]
        self.assertEqual((post['author_id'], post['kind'], post['created_at']), (42, 'post', AT))
        self.assertEqual(len(self.rows('post_versions')), 1)
        self.assertEqual(self.rows('feed_observations')[0]['source'], 'agent:maya')
        self.assertEqual(self.rows('team_accounts')[0]['is_own'], 1)
        self.assertEqual(self.rows('accounts')[0]['account_id'], 42)
        self.assertIsNone(self.rows('accounts')[0]['username'])
        task = self.rows('collection_tasks')[0]
        self.assertEqual((task['task_key'], task['kind']), ('counter:100', 'counter'))
        self.assertEqual(json.loads(task['payload_json'])['path'], '/api/posts/100/counters')
        self.assertTrue(result['content_confirmed'])
        self.assertEqual(self.rows('counter_snapshots'), [])
        self.assertEqual(self.rows('official_activity'), [])
        self.assertEqual(self.rows('score_snapshots'), [])

    def test_sparse_repost_preserves_unknown_content_and_remote_time(self):
        self.target()
        self.draft.update(action='repost', target_post_id=7, body='New generated comment')
        result = self.track({'post_id': 100})
        post = dict(self.conn.execute('SELECT * FROM posts WHERE post_id=100').fetchone())
        self.assertEqual((post['author_id'], post['kind'], post['repost_of_post_id']), (42, 'repost', 7))
        self.assertIsNone(post['body'])
        self.assertIsNone(post['created_at'])
        self.assertIsNone(post['visibility'])
        self.assertFalse(result['content_confirmed'])
        self.assertEqual(set(result['counter_task_keys']), {'counter:100', 'counter:7'})
        receipts = self.conn.execute("SELECT payload_json FROM raw_records WHERE source='agent:confirmed'").fetchall()
        self.assertNotIn('New generated comment', receipts[0]['payload_json'])

    def test_sparse_receipt_preserves_previously_observed_repost_body(self):
        self.target()
        with self.conn:
            save_posts(self.conn, [{'post_id': 100, 'author_id': 42, 'kind': 'repost',
                'repost_of_post_id': 7, 'body': 'Existing confirmed comment', 'created_at': AT}], 'latest', AT)
        self.draft.update(action='repost', target_post_id=7, body='Different current draft')
        self.track({'post_id': 100}, LATER)
        post = self.conn.execute('SELECT * FROM posts WHERE post_id=100').fetchone()
        self.assertEqual(post['body'], 'Existing confirmed comment')
        self.assertEqual(post['created_at'], AT)

    def test_like_records_exact_relationship_without_faking_post_or_counter(self):
        self.target()
        with self.conn:
            save_counters(self.conn, 7, {'like_count': 4, 'reply_count': 2}, AT)
        self.draft.update(action='like', target_post_id=7, post_id=7, body='')
        result = self.track(None, LATER)
        self.assertEqual(len(self.rows('posts')), 1)
        self.assertEqual(self.rows('counter_snapshots')[0]['like_count'], 4)
        relation = self.conn.execute("SELECT value FROM checkpoints WHERE key='agent_tracking:like:42:7'").fetchone()
        self.assertEqual(json.loads(relation['value'])['account_id'], 42)
        self.assertEqual(result['counter_task_keys'], ['counter:7'])

    def test_like_missing_target_stores_fact_without_fabricating_target_metadata(self):
        self.draft.update(action='like', target_post_id=7, post_id=7, body='')
        result = self.track()
        self.assertEqual(self.rows('posts'), [])
        self.assertEqual(result['counter_task_keys'], [])
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM checkpoints WHERE key='agent_tracking:like:42:7'").fetchone())

    def test_reply_retains_parent_and_root_and_schedules_shared_targets(self):
        self.target(7, kind='reply', root_post_id=3, reply_to_post_id=3)
        self.draft.update(action='reply', target_post_id=7,
                          sources=[{'post_id': 7, 'root_post_id': 3}])
        self.track({'post_id': 100, 'body': self.draft['body'], 'root_post_id': 3,
                    'reply_to_post_id': 7, 'author_id': 42, 'kind': 'reply'})
        post = self.conn.execute('SELECT * FROM posts WHERE post_id=100').fetchone()
        self.assertEqual((post['reply_to_post_id'], post['root_post_id']), (7, 3))

    def test_duplicate_ack_is_idempotent_even_if_task_already_finished(self):
        remote = {'post_id': 100, 'body': self.draft['body']}
        first = self.track(remote)
        with self.conn:
            self.conn.execute("UPDATE collection_tasks SET state='done'")
        before = {table: len(self.rows(table)) for table in ('posts', 'post_versions', 'raw_records', 'feed_observations')}
        self.assertEqual(self.track(remote, LATER), first)
        self.assertEqual(self.rows('collection_tasks')[0]['state'], 'done')
        self.assertEqual(before, {table: len(self.rows(table)) for table in before})

    def test_receipt_identity_cannot_be_rebound_to_another_post(self):
        self.track({'post_id': 100})
        self.draft['post_id'] = 101
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            self.track({'post_id': 101}, LATER)
        self.assertEqual(len(self.rows('posts')), 1)

    def test_mismatched_remote_identity_or_relationship_is_never_ingested(self):
        cases = [({'author_id': 99}, {}), ({'post_id': 101}, {}), ({'post_id': True}, {}),
                 ({'kind': 'repost'}, {}), ({'body': 'Not the submitted action'}, {}),
                 ({'reply_to_post_id': 9}, {}), ({'repost_of_post_id': 9}, {}),
                 ({'created_at': 'missing offset'}, {}),
                 ({'reply_to_post_id': 8}, {'action': 'reply', 'target_post_id': 7}),
                 ({'root_post_id': 99}, {'action': 'reply', 'target_post_id': 7,
                                       'sources': [{'post_id': 7, 'root_post_id': 3}]}),
                 ({'repost_of_post_id': 8}, {'action': 'repost', 'target_post_id': 7})]
        original = copy.deepcopy(self.draft)
        for remote, changes in cases:
            with self.subTest(remote=remote):
                self.draft.clear()
                self.draft.update(copy.deepcopy(original), **changes)
                with self.assertRaises(ValueError):
                    self.track(remote)
                self.assertEqual(self.rows('posts'), [])
                self.assertEqual(self.rows('raw_records'), [])

    def test_failure_rolls_back_public_rows_and_queue_without_committing_caller(self):
        self.conn.execute('BEGIN IMMEDIATE')
        self.conn.execute("INSERT INTO meta VALUES('outer_transaction','preserve')")
        with patch('ctn_data.agent_tracking.enqueue', side_effect=RuntimeError('queue unavailable')):
            with self.assertRaises(RuntimeError):
                record_success(self.data, self.conn, self.draft, self.agent, {'post_id': 100}, AT)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.rows('posts'), [])
        self.assertEqual(self.rows('raw_records'), [])
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM meta WHERE key='outer_transaction'").fetchone())
        self.conn.rollback()

    def test_manual_analysis_cache_and_existing_profile_and_team_label_remain_usable(self):
        with self.conn:
            save_accounts(self.conn, [{'account_id': 42, 'username': 'remote_name',
                                      'display_name': 'Confirmed profile'}], AT)
            self.conn.execute("INSERT INTO team_accounts VALUES(42,'Custom team',1,'manual confirmation',?)", (AT,))
            self.conn.execute("INSERT INTO analysis_cache VALUES('cache','scope',?,'{}',2)", (AT,))
        self.track({'post_id': 100}, LATER)
        self.assertEqual(self.rows('accounts')[0]['last_seen'], AT)
        self.assertEqual(self.rows('accounts')[0]['username'], 'remote_name')
        self.assertEqual(self.rows('team_accounts')[0]['team_label'], 'Custom team')
        self.assertEqual(self.rows('team_accounts')[0]['evidence'], 'manual confirmation')
        self.assertEqual(self.rows('analysis_cache')[0]['created_at'], AT)

    def test_recent_target_counter_and_existing_pending_task_are_reused(self):
        self.target()
        with self.conn:
            save_counters(self.conn, 7, {'like_count': 4}, AT)
            self.conn.execute("INSERT INTO collection_tasks(task_key,kind,payload_json,priority,due_at,state,updated_at) VALUES('counter:100','counter','{\"post_id\":100}',10,0,'leased',?)", (AT,))
        self.draft.update(action='repost', target_post_id=7)
        result = self.track({'post_id': 100})
        self.assertEqual(result['counter_task_keys'], ['counter:100'])
        task = self.rows('collection_tasks')[0]
        self.assertEqual((task['state'], task['priority'], task['due_at']), ('leased', 10, 0))

    def test_requires_existing_transaction_and_never_commits_action(self):
        with self.assertRaisesRegex(ValueError, 'existing action transaction'):
            record_success(self.data, self.conn, self.draft, self.agent, {'post_id': 100}, AT)
        self.conn.execute('BEGIN IMMEDIATE')
        record_success(self.data, self.conn, self.draft, self.agent, {'post_id': 100}, AT)
        self.conn.rollback()
        self.assertEqual(self.rows('posts'), [])
        self.assertEqual(self.rows('collection_tasks'), [])

    def test_authenticated_binding_immediately_indexes_only_known_public_profile(self):
        account = SimpleNamespace(account_id=42, username='verified_remote', token='SECRET_TOKEN',
            password='SECRET_PASSWORD', profile={'account_id': 42, 'username': 'verified_remote',
            'display_name': 'Remote display', 'private': False, 'verified': True, 'created_at': AT,
            'token': 'SECRET_NESTED_TOKEN', 'password': 'SECRET_NESTED_PASSWORD',
            'team': {'key': 'SECRET_TEAM'}, 'extra': {'credential': 'SECRET_OTHER'}})
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            public = record_account(self.conn, self.agent, account, LATER)
        row = self.rows('accounts')[0]
        self.assertEqual((row['username'], row['display_name'], row['private'], row['verified']),
                         ('verified_remote', 'Remote display', 0, 1))
        self.assertEqual(row['created_at'], AT)
        self.assertEqual(self.rows('team_accounts')[0]['is_own'], 1)
        self.assertNotIn('SECRET_', json.dumps(public))
        self.assertNotIn('SECRET_', json.dumps(self.rows('raw_records')))

    def test_sparse_binding_does_not_invent_role_name_or_created_at_or_private_flag(self):
        account = SimpleNamespace(account_id=42, username='verified_remote', token='SECRET_TOKEN', profile={})
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            record_account(self.conn, self.agent, account, AT)
        row = self.rows('accounts')[0]
        self.assertEqual(row['username'], 'verified_remote')
        self.assertIsNone(row['display_name'])
        self.assertIsNone(row['created_at'])
        self.assertIsNone(row['private'])
        self.assertNotIn('SECRET_TOKEN', json.dumps(self.rows('raw_records')))

    def test_binding_rejects_profile_identity_mismatch_without_public_writes(self):
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            with self.assertRaises(ValueError):
                record_account(self.conn, self.agent, SimpleNamespace(account_id=42,
                               profile={'account_id': 99, 'username': 'wrong_account'}), AT)
        self.assertEqual(self.rows('accounts'), [])
        self.assertEqual(self.rows('raw_records'), [])

    def test_reply_uses_fresh_execution_target_when_older_sources_have_no_root(self):
        self.target(7, kind='reply', root_post_id=3, reply_to_post_id=3)
        self.draft.update(action='reply', target_post_id=7, sources=[{'post_id': 7}],
            execution_target={'post_id': 7, 'root_post_id': 3, 'author_id': 9})
        self.track({'post_id': 100, 'body': self.draft['body']})
        self.assertEqual(self.conn.execute('SELECT root_post_id FROM posts WHERE post_id=100').fetchone()[0], 3)

    def test_sparse_legacy_sources_accept_root_confirmed_on_remote_reply(self):
        self.draft.update(action='reply', target_post_id=7, sources=[{'post_id': 7}])
        self.track({'post_id': 100, 'root_post_id': 3, 'reply_to_post_id': 7, 'body': self.draft['body']})
        self.assertEqual(self.conn.execute('SELECT root_post_id FROM posts WHERE post_id=100').fetchone()[0], 3)


if __name__ == '__main__':
    unittest.main()
