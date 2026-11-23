"""Verify the fact bank, extraction and targeting SQL on a random temporary PostgreSQL database.

Creates ctn_verify_<random>, runs the checks and drops it; never touches live CTN.
"""
import json
import sys
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from ctn_data import agent_targeting, fact_bank, fact_extraction
from ctn_data.db import connect, initialize


def iso(minutes=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec='seconds').replace('+00:00', 'Z')


class FactBankPostgres(unittest.TestCase):
    dsn = None

    def setUp(self):
        initialize(self.dsn)
        fact_bank.invalidate()
        agent_targeting._cache.clear()
        conn = connect(self.dsn)
        with conn:
            for table in ('facts', 'fact_rebuttals', 'fact_guardrails', 'fact_extractions', 'news_articles', 'posts'):
                conn.execute('DELETE FROM ' + table)
            self.seeded = fact_bank.initialize(conn)
        conn.close()

    def test_seed_review_supersede_and_query(self):
        self.assertGreater(self.seeded, 30)
        conn = connect(self.dsn)
        with conn:
            self.assertEqual(fact_bank.initialize(conn), 0)
            fact_bank.insert_extracted(conn, [{'claim': 'Poll: No 36.2%, Yes 33.8%.', 'concerns': ['momentum'],
                                               'topic_key': 'poll:referendum'}], review_mode='auto',
                                       article_key='a', source_label='Herald', observed_at='2026-10-21T07:00:00Z')
            fact_bank.insert_extracted(conn, [{'claim': 'Poll: Yes 38%, No 34%.', 'concerns': ['momentum'],
                                               'topic_key': 'poll:referendum'}], review_mode='manual',
                                       article_key='b', source_label='Herald', observed_at='2026-10-24T07:00:00Z')
        conn.close()
        pending = fact_bank.list_facts(self.dsn, status='pending')['items']
        self.assertEqual(len(pending), 1)
        fact_bank.decide(self.dsn, pending[0]['id'], 'approve', expected_revision=pending[0]['revision'])
        polls = [f['claim'] for f in fact_bank.facts_for(self.dsn, ['momentum'], limit=10) if 'Poll' in f['claim']]
        self.assertEqual(polls, ['Poll: Yes 38%, No 34%.'])
        rules = fact_bank.list_rules(self.dsn)
        self.assertTrue(rules['rebuttals'] and rules['guardrails'])

    def test_extraction_queue_and_duplicate_authors(self):
        conn = connect(self.dsn)
        with conn:
            conn.execute('''INSERT INTO news_articles(article_key,site,slug,title,body,published_at,first_seen,last_seen)
                VALUES(?,?,?,?,?,?,?,?)''', ('kingstonia-herald/x', 'kingstonia-herald', 'x', 'H2D2 case',
                'The health ministry has confirmed the first case of H2D2 Dolphin Flu.', iso(5), iso(5), iso(5)))
            text = 'Exactly the same campaign sentence copied by many accounts on Legit Social.'
            for post_id, author in ((1, 11), (2, 12), (3, 13)):
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                             (post_id, author, text, iso(10), iso(10), iso(10)))
        self.assertEqual(len(fact_extraction.pending_articles(conn, iso())), 1)
        conn.close()
        self.assertEqual(agent_targeting.duplicate_authors(self.dsn), frozenset({11, 12, 13}))
        settings = {**fact_bank.DEFAULT_SETTINGS, 'extract_enabled': True, 'review_mode': 'auto'}
        controller = SimpleNamespace(db_path=self.dsn, _stop=threading.Event(),
                                     store=SimpleNamespace(read=lambda: {'settings': {fact_bank.SETTINGS_KEY: settings}}),
                                     providers=SimpleNamespace(get=lambda _: {'protocol': 'openai_chat'}))
        reply = {'facts': [{'claim': 'Kingstonia confirmed its first H2D2 case.', 'concerns': ['not_here_yet'],
                            'evidence_quote': 'The health ministry has confirmed the first case of H2D2 Dolphin Flu.',
                            'topic_key': 'h2d2:cases_kingstonia'}]}
        with patch('ctn_data.agent_budget.model_call', return_value={'text': json.dumps(reply)}):
            summary = fact_extraction.run_once(controller)
        self.assertEqual(summary['accepted'], 1)
        conn = connect(self.dsn)
        self.assertEqual(fact_extraction.pending_articles(conn, iso()), [])
        conn.close()


class AttributionPostgres(unittest.TestCase):
    dsn = None

    def test_capture_settle_and_stats(self):
        from ctn_data import post_attribution
        initialize(self.dsn)
        agent_targeting._cache.clear()
        conn = connect(self.dsn)
        with conn:
            for table in ('post_attribution', 'counter_snapshots', 'posts', 'team_accounts'):
                conn.execute('DELETE FROM ' + table)
            conn.execute('INSERT INTO team_accounts VALUES(?,?,?,?,?)', (900, 'Agent Zero', 1, 'manual', iso(600)))
            for post_id, author, body, minutes, parent in ((10, 101, "Not sure about the ID card.", 540, None),
                                                          (50, 900, 'Voting Yes.', 480, 10),
                                                          (51, 102, "I'm voting yes now.", 420, 50)):
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen,reply_to_post_id) '
                             'VALUES(?,?,?,?,?,?,?)', (post_id, author, body, iso(minutes), iso(minutes), iso(minutes), parent))
        conn.close()
        state = {'drafts': {'r': {'id': 'r', 'agent_id': 'a', 'state': 'published', 'action': 'reply', 'post_id': 50,
                                  'target_post_id': 10, 'concern': 'id_privacy', 'body': 'Voting Yes.',
                                  'published_at': iso(480)}}}
        self.assertEqual(post_attribution.capture(self.dsn, state), 1)
        self.assertEqual(post_attribution.settle(self.dsn), 1)
        stats = post_attribution.arm_stats(self.dsn)
        self.assertEqual(stats[('id_privacy', 'reply')]['posts'], 1)
        self.assertEqual(stats[('id_privacy', 'reply')]['reply_yes'], 1)


def run():
    from ctn_data.secret_files import read_secret
    config = conninfo_to_dict(read_secret(ROOT / 'data/secrets/postgres_url').strip())
    database = 'ctn_verify_' + uuid.uuid4().hex[:12]
    admin = psycopg.connect(make_conninfo(**config), autocommit=True)
    admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
    try:
        FactBankPostgres.dsn = AttributionPostgres.dsn = (
            f"postgresql://{quote(config['user'], safe='')}:{quote(config['password'], safe='')}"
            f"@{config['host']}:{config['port']}/{database}")
        suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(FactBankPostgres),
                                    unittest.defaultTestLoader.loadTestsFromTestCase(AttributionPostgres)])
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    finally:
        from ctn_data import postgres
        close = getattr(postgres, 'close_pools', None)
        if callable(close):
            close()
        admin.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(database)))
        admin.close()


if __name__ == '__main__':
    sys.exit(run())
