"""News fact extraction: grounding checks, review modes and retry bookkeeping."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ctn_data import fact_bank, fact_extraction
from ctn_data.db import connect, initialize

BODY = ('The health ministry has confirmed the first case of H2D2 Dolphin Flu within Kingstonia. '
        'In a representative poll of 1,000 citizens, the No campaign captured 36.2% of the vote. '
        'Columnists say the government cannot be trusted.')


class FakeStore:
    def __init__(self, settings):
        self.settings = settings

    def read(self):
        return {'settings': {fact_bank.SETTINGS_KEY: self.settings}}


class ExtractionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'ctn.sqlite3')
        initialize(self.db)
        conn = connect(self.db)
        with conn:
            fact_bank.initialize(conn)
            conn.execute('''INSERT INTO news_articles(article_key,site,slug,title,body,published_at,first_seen,last_seen)
                VALUES(?,?,?,?,?,?,?,?)''', ('kingstonia-herald/first-case', 'kingstonia-herald', 'first-case',
                'First H2D2 case confirmed', BODY, '2026-10-21T08:30:00Z', 'x', 'x'))
            conn.execute('''INSERT INTO news_articles(article_key,site,slug,title,body,published_at,first_seen,last_seen)
                VALUES(?,?,?,?,?,?,?,?)''', ('kingstonia-herald/cat', 'kingstonia-herald', 'cat',
                'Firefighters rescue cat', 'A cat was rescued from a banyan tree.', '2026-10-17T18:30:00Z', 'x', 'x'))
        conn.close()
        fact_bank.invalidate()

    def tearDown(self):
        fact_bank.invalidate()
        self.tmp.cleanup()

    def controller(self, mode='manual'):
        settings = {**fact_bank.DEFAULT_SETTINGS, 'extract_enabled': True, 'review_mode': mode, 'provider_id': 'p'}
        return SimpleNamespace(db_path=self.db, store=FakeStore(settings), _stop=threading.Event(),
                               providers=SimpleNamespace(get=lambda pid: {'id': pid, 'protocol': 'openai_chat'}))

    def model(self, facts):
        return patch('ctn_data.agent_budget.model_call', return_value={'text': json.dumps({'facts': facts})})

    def good_and_bad(self):
        return [
            {'claim': 'Kingstonia confirmed its first H2D2 Dolphin Flu case.', 'concerns': ['not_here_yet'],
             'evidence_quote': 'The health ministry has confirmed the first case of H2D2 Dolphin Flu within Kingstonia.',
             'topic_key': 'h2d2:cases_kingstonia'},
            {'claim': 'A poll of 1,000 citizens put No at 36.2%.', 'concerns': ['momentum'],
             'evidence_quote': 'In a representative poll of 1,000 citizens, the No campaign captured 36.2% of the vote',
             'topic_key': 'poll:referendum'},
            # The number 40 is not in the quote.
            {'claim': 'No leads by 40 points.', 'concerns': ['momentum'],
             'evidence_quote': 'the No campaign captured 36.2% of the vote', 'topic_key': None},
            # The quote is not in the article.
            {'claim': 'The hospital is full.', 'concerns': ['hospital'],
             'evidence_quote': 'Kingstonia Central Hospital is completely full tonight.', 'topic_key': None},
        ]

    def test_only_relevant_articles_are_due(self):
        conn = connect(self.db)
        due = fact_extraction.pending_articles(conn, '2026-10-22T00:00:00Z')
        conn.close()
        self.assertEqual([a['article_key'] for a in due], ['kingstonia-herald/first-case'])

    def test_grounded_claims_kept_and_pending_in_manual_mode(self):
        with self.model(self.good_and_bad()):
            summary = fact_extraction.run_once(self.controller('manual'))
        self.assertEqual((summary['accepted'], summary['dropped']), (2, 2))
        pending = fact_bank.list_facts(self.db, status='pending', layer='news')['items']
        self.assertEqual(len(pending), 2)
        self.assertTrue(all(row['source_label'].startswith('Kingstonia Herald, 2026-10-21') for row in pending))
        # Done articles are not extracted again.
        with self.model([]) as called:
            fact_extraction.run_once(self.controller('manual'))
        called.assert_not_called()

    def test_auto_mode_activates(self):
        with self.model(self.good_and_bad()):
            fact_extraction.run_once(self.controller('auto'))
        self.assertEqual(len(fact_bank.list_facts(self.db, status='active', layer='news')['items']), 2)

    def test_changed_body_is_extracted_again(self):
        with self.model(self.good_and_bad()):
            fact_extraction.run_once(self.controller('auto'))
        conn = connect(self.db)
        with conn:
            conn.execute("UPDATE news_articles SET body=body||' Contacts are being tested.' WHERE slug='first-case'")
        conn.close()
        with self.model([]) as called:
            fact_extraction.run_once(self.controller('auto'))
        called.assert_called_once()

    def test_failures_back_off_and_stop_after_max_attempts(self):
        with patch('ctn_data.agent_budget.model_call', side_effect=RuntimeError('boom')):
            summary = fact_extraction.run_once(self.controller())
        self.assertEqual(summary['failed'], 1)
        conn = connect(self.db)
        row = conn.execute('SELECT * FROM fact_extractions').fetchone()
        self.assertEqual((row['status'], row['attempts']), ('failed', 1))
        self.assertFalse(fact_extraction.pending_articles(conn, '2026-01-01T00:00:00Z'))
        with conn:
            conn.execute('UPDATE fact_extractions SET attempts=?', (fact_extraction.MAX_ATTEMPTS,))
        self.assertFalse(fact_extraction.pending_articles(conn, '2099-01-01T00:00:00Z'))
        conn.close()

    def test_disabled_does_nothing(self):
        controller = self.controller()
        controller.store.settings['extract_enabled'] = False
        with self.model([]) as called:
            self.assertEqual(fact_extraction.run_once(controller)['status'], 'disabled')
        called.assert_not_called()


if __name__ == '__main__':
    unittest.main()
