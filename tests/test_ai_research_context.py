import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from ctn_data.db import connect, initialize
from ctn_data.ai_research_context import build_research_context
from ctn_data.ai_evidence import build_task_prompt


class ResearchContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'context.sqlite3'
        initialize(self.path)
        with closing(connect(self.path)) as conn:
            for post_id, author_id, visibility, parent in [(1, 7, 'public', None), (2, 8, 'public', 1), (3, 9, 'private', 1)]:
                conn.execute('INSERT INTO posts(post_id,author_id,kind,body,visibility,reply_to_post_id,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?)',
                             (post_id, author_id, 'post', 'sample text', visibility, parent, '2026-10-19T07:00:00Z', '2026-10-19T07:00:00Z', '2026-10-19T08:00:00Z'))
            for observed, likes, replies in [('2026-10-19T07:00:00Z', 5, 2), ('2026-10-19T07:10:00Z', 9, 3)]:
                conn.execute('INSERT INTO counter_snapshots(post_id,observed_at,like_count,reply_count,repost_count,view_count) VALUES(1,?,?,?,?,?)',
                             (observed, likes, replies, 1, 20))
            conn.commit()

    def tearDown(self):
        self.temp.cleanup()

    def test_full_scope_totals_and_actual_observation_interval(self):
        result = build_research_context(self.path, {'own': 'all'}, [{'kind': 'post', 'post_id': 1}])
        self.assertEqual(result['corpus']['posts'], 2)
        self.assertEqual(result['corpus']['authors'], 2)
        self.assertEqual(result['relations'][0]['known_direct_replies_in_scope'], 1)
        interval = result['snapshot_intervals'][0]
        self.assertEqual(interval['elapsed_minutes'], 10)
        self.assertEqual(interval['interaction_delta'], 5)
        self.assertEqual(interval['interactions_per_minute'], .5)
        self.assertEqual(interval['status'], 'observed_pair')

    def test_scope_applies_to_aggregates_and_missing_pair_is_unknown(self):
        result = build_research_context(self.path, {'author_id': 8}, [{'kind': 'post', 'post_id': 2}])
        self.assertEqual(result['corpus']['posts'], 1)
        self.assertEqual(result['snapshot_intervals'][0]['status'], 'insufficient_snapshots')
        self.assertEqual(result['relations'][0]['reply_to_post_id'], 1)

    def test_decreased_counter_is_preserved_without_growth_rate(self):
        with closing(connect(self.path)) as conn:
            conn.execute('UPDATE counter_snapshots SET like_count=1 WHERE observed_at=?', ('2026-10-19T07:10:00Z',))
            conn.commit()
        result = build_research_context(self.path, {}, [{'kind': 'post', 'post_id': 1}])
        interval = result['snapshot_intervals'][0]
        self.assertEqual(interval['status'], 'counter_decreased')
        self.assertEqual(interval['deltas']['like_count'], -4)
        self.assertIsNone(interval['interactions_per_minute'])

    def test_large_source_context_selects_auditable_subset_inside_provider_budget(self):
        import json
        sources = [{'id': f'post:{index}', 'kind': 'post', 'group': 'popular', 'title': 'Research sample ' * 30,
                    'text': '长文本\n' * 600, 'metrics': {'interactions': index, 'trending_rank': None},
                    'unused_metadata': 'x' * 1000} for index in range(200)]
        prompt = build_task_prompt('custom', '要求' * 6000, {'sources': sources})
        self.assertLess(len(prompt), 85000)
        encoded = prompt.split('<evidence_json>\n')[1].split('\n</evidence_json>')[0]
        payload = json.loads(encoded)
        self.assertTrue({row['id'] for row in payload['sources']} <= {row['id'] for row in sources})
        self.assertGreater(payload['context_pack']['dropped_sources'], 0)
        from ctn_data.ai_context import estimate_tokens
        self.assertLessEqual(estimate_tokens(payload), payload['context_pack']['input_budget_tokens'])
        self.assertTrue(payload['context_pack']['text_shortened'])


if __name__ == '__main__':
    unittest.main()
