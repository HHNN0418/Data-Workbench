from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ctn_data.account_screening import build_screening
from ctn_data.db import connect, initialize

AT = '2026-10-19T00:00:00Z'
TEXT = 'This is a sufficiently long original statement about a specific public policy with detailed evidence.'


class AccountScreeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'screen.sqlite3'
        initialize(self.path)
        self.next_id = 1

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, sql, args=()):
        with closing(connect(self.path)) as conn, conn:
            conn.execute(sql, args)

    def posts(self, author, count=12, interval=600, text=None, kind='post', start=AT,
              times=None, visibility='public', repost_of=None):
        first = datetime.fromisoformat(start.replace('Z', '+00:00'))
        rows = []
        for index in range(count):
            created = times[index] if times is not None else (first + timedelta(seconds=index * interval)).isoformat()
            body = text if text is not None else TEXT + f' Unique subject {author} number {index}.'
            rows.append((self.next_id, author, body, kind, created, visibility, repost_of, AT, AT))
            self.next_id += 1
        with closing(connect(self.path)) as conn, conn:
            conn.executemany('''INSERT INTO posts(post_id,author_id,body,kind,created_at,visibility,repost_of_post_id,first_seen,last_seen)
                VALUES(?,?,?,?,?,?,?,?,?)''', rows)
        return [row[0] for row in rows]

    def account(self, author, private=0, verified=0):
        self.execute('INSERT INTO accounts(account_id,username,private,verified,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                     (author, f'user{author}', private, verified, AT, AT))

    def result_for(self, author):
        return next(item for item in build_screening(self.path)['items'] if item['account_id'] == author)

    def signal(self, account, key):
        return next(signal for signal in account['signals'] if signal['key'] == key)

    def test_model_diagnostic_sample_includes_zero_and_unknown_beyond_priority_page(self):
        for author in range(1, 6):
            self.posts(author)
        first = datetime.fromisoformat(AT.replace('Z', '+00:00'))
        offsets = [0, 90, 2000, 2100, 4200, 6500, 6700, 9999, 18000, 18500, 27000, 50000]
        self.posts(9, times=[(first + timedelta(seconds=x)).isoformat() for x in offsets])
        self.posts(10, count=1)
        ordinary = build_screening(self.path, limit=3)['items']
        self.assertTrue(all(x['score'] and x['score'] > 0 for x in ordinary))
        diagnostic = build_screening(self.path, limit=3, include_diagnostics=True)['items']
        self.assertIn(0, [x['score'] for x in diagnostic])
        self.assertIn(None, [x['score'] for x in diagnostic])
        self.assertTrue(any(x['score'] and x['score'] > 0 for x in diagnostic))

    def test_regular_publication_times_differ_from_noisy_and_ignore_collection_times(self):
        self.posts(1)
        first = datetime.fromisoformat(AT.replace('Z', '+00:00'))
        offsets = [0, 90, 2000, 2100, 4200, 6500, 6700, 9999, 18000, 18500, 27000, 50000]
        self.posts(2, times=[(first + timedelta(seconds=offset)).isoformat() for offset in offsets])
        regular = self.result_for(1)
        noisy = self.result_for(2)
        self.assertEqual(self.signal(regular, 'regular_publication_intervals')['points'], 20)
        self.assertEqual(self.signal(noisy, 'regular_publication_intervals')['points'], 0)
        self.assertGreater(regular['score'], noisy['score'])
        self.assertEqual(regular['data_observed']['timestamp_basis'], 'original_post_created_at')
        self.assertEqual(regular['data_observed']['observed_span_hours'], round(110 / 60, 3))

    def test_duplicate_long_originals_supply_bounded_original_evidence(self):
        ids = self.posts(1, text=TEXT)
        item = self.result_for(1)
        repeated = self.signal(item, 'repeated_original_text')
        self.assertEqual(repeated['points'], 30)
        self.assertEqual(repeated['measurements']['duplicate_posts_after_first'], 11)
        self.assertTrue(set(repeated['post_ids']).issubset(ids))
        self.assertLessEqual(len(repeated['post_ids']), 8)
        self.assertLessEqual(len(item['examples']), 3)
        self.assertTrue(all(len(example['text']) <= 500 for example in item['examples']))
        self.assertEqual(item['known_post_count'], 12)
        self.assertFalse(build_screening(self.path)['methodology']['bot_probability'])

    def test_short_text_reposts_and_quoted_reposts_do_not_establish_bot_signals(self):
        self.posts(1, text='Thank you!')
        self.posts(2, text=TEXT, kind='repost', interval=1)
        self.posts(3, text=TEXT, kind='quoted_repost', interval=1)
        self.posts(4, text=TEXT, repost_of=1, interval=1)
        self.assertEqual(self.signal(self.result_for(1), 'repeated_original_text')['points'], 0)
        for author in (2, 3, 4):
            item = self.result_for(author)
            self.assertIsNone(item['score'])
            self.assertEqual(item['band'], '资料不足')
            self.assertTrue(all(signal['points'] == 0 for signal in item['signals']))

    def test_missing_data_is_unknown_not_low_risk_and_threshold_excludes_unknown(self):
        self.posts(1, count=4, text=TEXT)
        self.posts(2, count=10, times=['invalid'] * 10)
        result = build_screening(self.path)
        self.assertEqual(result['summary']['insufficient_account_count'], 2)
        self.assertTrue(all(item['score'] is None for item in result['items']))
        self.assertEqual(build_screening(self.path, min_score=1)['items'], [])

    def test_private_own_and_null_authors_excluded_own_scope_empty(self):
        self.account(1, private=1)
        self.account(2, verified=1)
        self.execute('INSERT INTO team_accounts VALUES(?,?,?,?,?)', (3, 'Our team', 1, 'manual', AT))
        for author in (1, 2, 3):
            self.posts(author)
        self.posts(4, visibility='private')
        self.posts(None)
        result = build_screening(self.path)
        self.assertEqual([item['account_id'] for item in result['items']], [2])
        self.assertEqual(result['items'][0]['verified'], 1)
        self.assertEqual(result['summary']['source_post_count'], 12)
        own = build_screening(self.path, {'own': 'own'})
        self.assertEqual(own['items'], [])
        self.assertEqual(own['summary']['reason'], 'own_scope_excluded')

    def test_temporal_cross_account_coordination_requires_three_distinct_accounts(self):
        for author in (1, 2, 3):
            self.posts(author, count=8, text=TEXT, interval=600)
        for author in (1, 2, 3):
            self.assertGreater(self.signal(self.result_for(author), 'cross_account_exact_text')['points'], 0)
        self.assertEqual(self.signal(self.result_for(1), 'cross_account_exact_text')['measurements']['distinct_shared_texts'], 1)
        self.assertEqual(self.signal(self.result_for(1), 'cross_account_exact_text')['measurements']['coordinated_posts'], 8)
        scoped = build_screening(self.path, {'author_id': 1})['items'][0]
        self.assertEqual(self.signal(scoped, 'cross_account_exact_text')['points'], 0)

    def test_different_time_identical_prefixes_and_two_accounts_do_not_establish_coordination(self):
        self.posts(1, text=TEXT)
        self.posts(2, text=TEXT)
        self.posts(3, text=TEXT, start='2026-10-20T00:00:00Z')
        self.assertEqual(self.signal(self.result_for(1), 'cross_account_exact_text')['points'], 0)
        for author in (4, 5, 6):
            self.posts(author, text='long prefix ' * 900 + f' different ending {author}')
        for author in (4, 5, 6):
            self.assertEqual(self.signal(self.result_for(author), 'cross_account_exact_text')['points'], 0)

    def test_coordination_witnesses_only_show_peers_in_actual_matching_window(self):
        post_owners = {}
        for author in (1, 2, 3):
            for post_id in self.posts(author, count=8, text=TEXT):
                post_owners[post_id] = author
        # Same text on the next day is not a peer in the first day's windows.
        for post_id in self.posts(4, count=8, text=TEXT, start='2026-10-20T00:00:00Z'):
            post_owners[post_id] = 4
        item = self.result_for(1)
        measurements = self.signal(item, 'cross_account_exact_text')['measurements']
        self.assertEqual(set(measurements['peer_account_ids']), {2, 3})
        self.assertNotIn(4, measurements['peer_account_ids'])
        self.assertTrue(set(measurements['peer_post_ids']).issubset(post_owners))
        self.assertLessEqual(len(measurements['coordination_examples']), 2)
        for example in measurements['coordination_examples']:
            self.assertEqual(post_owners[example['post_id']], 1)
            self.assertEqual([post_owners[post_id] for post_id in example['peer_post_ids']], example['peer_account_ids'])
            first = datetime.fromisoformat(example['window_start'])
            last = datetime.fromisoformat(example['window_end'])
            self.assertLessEqual((last - first).total_seconds(), 120)
            self.assertEqual(first.date().isoformat(), '2026-10-19')
        late = self.signal(self.result_for(4), 'cross_account_exact_text')['measurements']
        self.assertEqual(late['peer_account_ids'], [])
        self.assertEqual(late['coordination_examples'], [])

    def test_bursts_continuous_activity_and_official_eligibility_stay_separate(self):
        self.posts(1, interval=1)
        self.posts(2, count=72, interval=3600)
        burst = self.result_for(1)
        continuous = self.result_for(2)
        self.assertEqual(self.signal(burst, 'publication_bursts')['points'], 20)
        self.assertEqual(self.signal(continuous, 'continuous_daily_activity')['points'], 10)
        eligibility = burst['account_report_eligibility']
        self.assertEqual(eligibility['status'], 'unknown')
        self.assertIsNone(eligibility['permanently_hidden_posts'])
        self.assertIsNone(eligibility['official_action_count'])

    def test_candidate_limit_scope_and_strict_inputs(self):
        self.posts(1)
        with patch('ctn_data.account_screening.MAX_POSTS', 5):
            result = build_screening(self.path)
        self.assertTrue(result['summary']['truncated'])
        self.assertEqual(result['summary']['source_post_count'], 12)
        self.assertEqual(result['summary']['scanned_post_count'], 5)
        self.assertEqual(result['items'][0]['known_post_count'], 12)
        for kwargs in ({'limit': True}, {'limit': 201}, {'limit': '100'}, {'min_score': 101}, {'min_score': 2.0}):
            with self.assertRaises(ValueError):
                build_screening(self.path, **kwargs)
        self.assertEqual(build_screening(self.path, {'since': '2027-01-01T00:00:00Z'})['items'], [])


if __name__ == '__main__':
    unittest.main()
