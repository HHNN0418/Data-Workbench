"""Annotation progress, backlog and the stance, concern and depth distributions; read-only."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ctn_data import agent_targeting, processing_view, stance_labels
from ctn_data.annotations import classifier_version
from ctn_data.db import connect, initialize

NOW = datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc)


def stamp(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec='microseconds').replace('+00:00', 'Z')


class Worker:
    last_result = {'posts': 12, 'bots': 1, 'finished_at': '2026-10-22T10:59:00Z'}


class ProcessingViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'test.sqlite3'
        initialize(self.db)
        version = classifier_version()
        conn = connect(self.db)
        with conn:
            conn.execute("INSERT INTO team_accounts VALUES(5554,'Agent Zero',1,'test',?)", (stamp(0),))
            conn.execute("INSERT INTO account_annotations(account_id,is_bot,bot_reasons_json,updated_at) "
                         "VALUES(300,1,'[\"behavior\"]',?)", (stamp(0),))
            posts = [  # post_id, author, hours ago, stance, concerns, depth, classifier
                (1, 100, 1, 'undecided', ['hospital', 'cost'], 0, version),
                (2, 100, 2, 'no', ['freedom'], 1, version),
                (3, 101, 3, 'undecided', ['hospital'], 8, version),
                (4, 300, 1, 'yes', ['hospital'], 2, version),
                (5, 5554, 1, 'yes', [], 5, version),
                (6, 102, 30, 'no', ['cost'], 0, version),
                (7, 103, 4, 'unrelated', [], None, 'old-rules'),
            ]
            for post_id, author, hours, stance, concerns, depth, classifier in posts:
                conn.execute('INSERT INTO posts(post_id,author_id,kind,body,created_at,first_seen,last_seen) '
                             "VALUES(?,?,'post','x',?,?,?)", (post_id, author, stamp(hours), stamp(hours), stamp(hours)))
                conn.execute('INSERT INTO post_annotations(post_id,author_id,created_at,stance,concerns_json,thread_depth,'
                             'classifier,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                             (post_id, author, stamp(hours), stance, json.dumps(concerns), depth, classifier,
                              stamp(0.5 if post_id == 1 else 2)))
            conn.execute('INSERT INTO posts(post_id,author_id,kind,body,created_at,first_seen,last_seen) '
                         "VALUES(8,104,'post','new',?,?,?)", (stamp(0.1), stamp(0.1), stamp(0.1)))
        conn.close()

    def test_progress_and_backlog_against_the_current_classifier(self):
        result = processing_view.build(self.db, worker=Worker(), now=NOW)
        progress = result['progress']
        self.assertEqual((progress['posts_total'], progress['annotated'], progress['current_classifier'],
                          progress['backlog']), (8, 7, 6, 2))
        self.assertEqual(progress['coverage'], 0.75)
        self.assertEqual(progress['bot_accounts'], 1)
        self.assertEqual(progress['classifier'], classifier_version())
        self.assertEqual(progress['last_annotated_at'], stamp(0.5))
        self.assertEqual(progress['last_pass']['posts'], 12)
        self.assertTrue(result['read_only'])

    def test_model_labelled_rows_are_current_not_backlog(self):
        # D84: posts with a stored model label carry classifier 'model' and the worker leaves them alone.
        conn = connect(self.db)
        with conn:
            conn.execute('INSERT INTO posts(post_id,author_id,kind,body,created_at,first_seen,last_seen) '
                         "VALUES(9,105,'post','x',?,?,?)", (stamp(1), stamp(1), stamp(1)))
            conn.execute('INSERT INTO post_annotations(post_id,author_id,created_at,stance,concerns_json,thread_depth,'
                         "classifier,updated_at) VALUES(9,105,?,'yes','[]',0,?,?)",
                         (stamp(1), stance_labels.CLASSIFIER, stamp(2)))
        conn.close()
        progress = processing_view.build(self.db, now=NOW)['progress']
        self.assertEqual((progress['posts_total'], progress['annotated'], progress['current_classifier'],
                          progress['backlog']), (9, 8, 7, 2))
        self.assertEqual(progress['coverage'], round(7 / 9, 4))

    def test_distributions_for_all_posts_and_citizens(self):
        result = processing_view.build(self.db, now=NOW)
        totals = result['window_totals']
        self.assertEqual((totals['all'], totals['citizen'], totals['own'], totals['bot'], totals['posts']),
                         (6, 4, 1, 1, 7))
        citizen = {row['key']: row['count'] for row in result['stance']['citizen']}
        self.assertEqual(citizen, {'yes': 0, 'no': 1, 'undecided': 2, 'unrelated': 1})
        everyone = {row['key']: row['share'] for row in result['stance']['all']}
        self.assertEqual(everyone['yes'], round(2 / 6, 4))
        depth = {row['key']: row['count'] for row in result['depth']['citizen']}
        self.assertEqual((depth['0'], depth['1'], depth['7+'], depth['unknown']), (1, 1, 1, 1))
        self.assertEqual(result['max_reply_depth'], agent_targeting.MAX_REPLY_DEPTH)
        concerns = {row['concern']: row for row in result['concerns']}
        self.assertEqual(result['concerns'][0]['concern'], 'hospital')
        self.assertEqual((concerns['hospital']['all'], concerns['hospital']['citizen'],
                          concerns['hospital']['citizen_undecided']), (3, 2, 2))
        self.assertEqual(concerns['cost']['all'], 1)
        self.assertEqual(totals['with_concern'], {'all': 4, 'citizen': 3})


if __name__ == '__main__':
    unittest.main()
