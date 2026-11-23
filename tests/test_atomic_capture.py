import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ctn_data.db import connect, initialize
from ctn_data.browser_capture import capture
from ctn_data.imports import add_score_snapshot
from ctn_data.ingest import save_posts, save_counters


class AtomicCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'atomic.sqlite3'
        initialize(self.db)
        conn = connect(self.db)
        with conn:
            conn.execute("INSERT INTO meta VALUES('browser_team_id','46')")
            save_posts(conn, [{'post_id': 1, 'body': 'same', 'kind': 'post', 'visibility': 'public'}], 'latest')
        conn.close()
        self.portal = dict(id='portal-atomic-1',kind='portal',observed_at='2026-10-19T01:00:00Z',
            data=dict(team_id=46,total='10.25',activity_csv='When (UTC),Type,Message\n2026-10-19T00:00:00Z,SCORE,awarded 10.25 points\n'))

    def tearDown(self):
        self.temp.cleanup()

    def count(self, table):
        conn=connect(self.db)
        try:return conn.execute('SELECT count(*) FROM '+table).fetchone()[0]
        finally:conn.close()

    def test_portal_failure_rolls_back_activity_and_receipt(self):
        with patch('ctn_data.browser_capture.add_score_snapshot',side_effect=ValueError('injected failure')):
            self.assertEqual(len(capture(self.db,[self.portal])['errors']),1)
        self.assertEqual(self.count('official_activity'),0)
        self.assertEqual(self.count('activity_imports'),0)
        self.assertEqual(self.count('checkpoints'),0)
        self.assertEqual(capture(self.db,[self.portal])['errors'],[])
        self.assertEqual(self.count('score_snapshots'),1)
        self.assertEqual(self.count('official_activity'),1)

    def test_concurrent_same_receipt_only_commits_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            replies=list(pool.map(lambda _:capture(self.db,[self.portal]),range(12)))
        self.assertTrue(all(r['accepted']==['portal-atomic-1'] and not r['errors'] for r in replies))
        for table in ('official_activity','score_snapshots','activity_imports','checkpoints','score_observation_keys'):
            self.assertEqual(self.count(table),1,table)

    def test_score_semantic_key_deduplicates_different_receipts(self):
        capture(self.db,[self.portal])
        for index in range(4):
            self.assertFalse(capture(self.db,[{**self.portal,'id':f'portal-other-{index}'}])['errors'])
        self.assertEqual(self.count('score_snapshots'),1)
        self.assertEqual(self.count('checkpoints'),5)

    def test_same_timestamp_conflict_is_preserved_and_retry_idempotent(self):
        conn=connect(self.db)
        at='2026-10-19T01:00:00Z'
        with conn:
            save_counters(conn,1,{'like_count':2},at)
            for _ in range(2):save_counters(conn,1,{'like_count':3},at,source='browser:counters')
            save_counters(conn,1,{'like_count':3},'2026-10-19T01:01:00Z')
        self.assertEqual(conn.execute('SELECT like_count FROM counter_snapshots ORDER BY id LIMIT 1').fetchone()[0],2)
        self.assertEqual(conn.execute('SELECT like_count FROM counter_conflicts').fetchone()[0],3)
        conn.close()
        self.assertEqual(self.count('counter_snapshots'),2)
        self.assertEqual(self.count('counter_conflicts'),1)


if __name__=='__main__':unittest.main()
