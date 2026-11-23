"""D77: model stance/concern labels for citizen posts and the targeting A/B."""
import json
import tempfile
import time
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ctn_data import agent_targeting, stance_labels
from ctn_data.db import connect, initialize

NOW = datetime(2026, 10, 23, 5, 0, tzinfo=timezone.utc)


def iso(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')


class LabelBatchTest(unittest.TestCase):
    def test_reads_stance_and_concerns_and_skips_bad_items(self):
        answer = {'results': [
            {'i': 0, 'about': 'yes', 'stance': 'no', 'concerns': ['cost', 'not_a_concern', 'cost']},
            {'i': 1, 'about': 'no', 'stance': 'undecided', 'concerns': ['cost']},
            {'i': 7, 'about': 'yes', 'stance': 'yes', 'concerns': []}]}
        with patch('ctn_data.ai_providers.complete', return_value={'text': json.dumps(answer), 'model': 'm'}) as call:
            labels = stance_labels.label_batch({'model': 'm'}, [(11, 'Too expensive, voting No.'), (12, 'Nice beach.')])
        self.assertEqual(labels[11], {'stance': 'no', 'concerns': ['cost'], 'model': 'm'})
        self.assertEqual(labels[12], {'stance': 'unrelated', 'concerns': [], 'model': 'm'})
        self.assertEqual(set(labels), {11, 12})
        schema = call.call_args.kwargs['response_schema']
        enum = schema['properties']['results']['items']['properties']['concerns']['items']['enum']
        self.assertEqual(enum, sorted(agent_targeting.CONCERN_PATTERNS))


class StoreAndPendingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'ctn.sqlite3')
        initialize(self.db)
        conn = connect(self.db)
        with conn:
            conn.execute("INSERT INTO team_accounts VALUES(7001,'Agent Zero',1,'test',?)", (iso(0),))
            for post_id, author, minutes in ((1, 100, 5), (2, 7001, 5), (3, 5000, 5), (4, 4000, 5), (5, 100, 300), (6, 101, 1)):
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                             (post_id, author, f'post {post_id}', iso(minutes), iso(minutes), iso(minutes)))
        conn.close()

    def test_pending_is_queued_targets_then_recent_known_npc_posts(self):
        stance_labels._requested.clear()
        self.addCleanup(stance_labels._requested.clear)
        stance_labels.store(self.db, {6: {'stance': 'yes', 'concerns': [], 'model': 'm'}})
        stance_labels.request({4: 'post 4', 6: 'post 6'}, now=NOW.timestamp())
        # Queued target 4 comes first; 6 is labelled already; known NPC 100's recent post follows;
        # its old post (5), own (7001) and unqueued other accounts (5000) are left out.
        self.assertEqual([post_id for post_id, _ in stance_labels.pending(self.db, NOW)], [4, 1])
        stance_labels.request({9: 'old target'}, now=NOW.timestamp() - 3 * 3600)
        self.assertNotIn(9, [post_id for post_id, _ in stance_labels.pending(self.db, NOW)])
        # A citizen seen in a reading is watched: their other recent posts are labelled too.
        stance_labels._requested.clear()
        stance_labels._watched.clear()
        self.addCleanup(stance_labels._watched.clear)
        stance_labels.request({}, now=NOW.timestamp(), authors={4000})
        self.assertEqual(sorted(post_id for post_id, _ in stance_labels.pending(self.db, NOW)), [1, 4])

    def test_store_and_lookup_round_trip(self):
        stance_labels.store(self.db, {1: {'stance': 'no', 'concerns': ['cost'], 'model': 'm'}})
        stance_labels.store(self.db, {1: {'stance': 'yes', 'concerns': [], 'model': 'm'}})  # first label stays
        self.assertEqual(stance_labels.lookup(self.db, [1, 2, 'x']), {1: {'stance': 'no', 'concerns': ['cost']}})


class ModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'ctn.sqlite3')
        initialize(self.db)
        stance_labels.invalidate()
        self.addCleanup(stance_labels.invalidate)

    def set_mode(self, value):
        conn = connect(self.db)
        with conn:
            conn.execute("INSERT INTO meta(key,value) VALUES('agent_control_v1:settings',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps({'stance_source': value}),))
        conn.close()
        stance_labels.invalidate()

    def test_model_is_the_default_and_keyword_switches_back(self):
        self.assertEqual(stance_labels.mode(self.db), 'model')
        stance_labels.store(self.db, {1: {'stance': 'no', 'concerns': ['cost'], 'model': 'm'}})
        assess = stance_labels.classifier(self.db, [1, 2])
        self.assertEqual(assess(1, 'Voting yes!'), {'stance': 'no', 'concerns': ['cost'], 'stance_source': 'model'})
        self.assertEqual(assess(2, 'Voting yes!')['stance_source'], 'keyword')
        self.set_mode('keyword')
        self.assertEqual(stance_labels.mode(self.db), 'keyword')
        self.assertEqual(stance_labels.classifier(self.db, [1])(1, 'Voting yes!')['stance'], 'yes')
        self.set_mode('nonsense')
        self.assertEqual(stance_labels.mode(self.db), 'model')

    def test_stored_annotations_follow_the_switch(self):
        from ctn_data import annotations
        conn = connect(self.db)
        with conn:
            conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                         (1, 100, 'Voting yes, the jab keeps us safe.', iso(5), iso(5), iso(5)))
        conn.close()
        def annotation():
            conn = connect(self.db)
            try:
                return tuple(conn.execute('SELECT stance,classifier FROM post_annotations WHERE post_id=1').fetchone())
            finally:
                conn.close()
        annotations.refresh_posts(self.db)
        self.assertEqual(annotation()[0], 'yes')
        stance_labels.store(self.db, {1: {'stance': 'undecided', 'concerns': ['safety'], 'model': 'm'}})
        annotations.refresh_posts(self.db)
        self.assertEqual(tuple(annotation()), ('undecided', stance_labels.CLASSIFIER))
        self.set_mode('keyword')
        annotations.refresh_posts(self.db)
        self.assertEqual(annotation()[0], 'yes')
        self.set_mode('model')
        annotations.refresh_posts(self.db)
        self.assertEqual(annotation()[0], 'undecided')


class AnnotateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'ctn.sqlite3')
        initialize(self.db)
        agent_targeting._cache.clear()

    def test_only_the_model_arm_uses_the_stored_label(self):
        stance_labels.store(self.db, {5: {'stance': 'no', 'concerns': ['cost'], 'model': 'm'}})
        for arm, expected in (('model', ('no', ['cost'], 'model')), ('keyword', ('yes', ['safety'], 'keyword'))):
            sources = {'post:5': {'id': 'post:5', 'kind': 'post', 'post_id': 5, 'author_id': 600,
                                  'text': 'Voting yes, the vaccine keeps us safe.'},
                       'post:6': {'id': 'post:6', 'kind': 'post', 'post_id': 6, 'author_id': 601,
                                  'text': 'Voting yes, the vaccine keeps us safe.'}}
            with self.subTest(arm=arm), patch('ctn_data.stance_labels.mode', return_value=arm):
                result = agent_targeting.annotate(self.db, {'agents': {}, 'drafts': {}}, {'id': 'a'}, sources, time.time())
                target = result['post:5']['target']
                self.assertEqual((target['stance'], target['concerns'], target['stance_source']), expected)
                # A post without a stored label falls back to keywords in either arm; the
                # model arm queues it for labelling.
                self.assertEqual(result['post:6']['target']['stance_source'], 'keyword')
                self.assertEqual(6 in stance_labels._requested, arm == 'model')
                stance_labels._requested.clear()


if __name__ == '__main__':
    unittest.main()
