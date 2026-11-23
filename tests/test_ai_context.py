from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

from ctn_data.ai_context import evidence_budget, estimate_tokens, pack_evidence


def source(index, text='A complete useful short post.', author=None, group='popular'):
    return {'id': f'post:{index}', 'kind': 'post', 'post_id': index,
            'author_id': author if author is not None else index, 'text': text,
            'group': group, 'created_at': '2026-10-20T00:00:00Z',
            'observed_at': '2026-10-20T01:00:00Z', 'metrics': {'interactions': index},
            'source_parent': {'id': 'post:99'}, 'coverage': 'full'}


class AIContextTests(unittest.TestCase):
    def test_budget_covers_serialized_complete_object_and_is_immutable(self):
        evidence = {'summary': {'scope_count': 1000},
                    'sources': [source(i, '完整短帖，必须保留其上下文。' * 5) for i in range(40)]}
        snapshot = deepcopy(evidence)
        packed = pack_evidence(evidence, budget_tokens=3000)
        self.assertLessEqual(estimate_tokens(packed), 3000)
        self.assertEqual(packed['context_pack']['estimated_tokens'], estimate_tokens(packed))
        self.assertEqual(evidence, snapshot)
        self.assertLess(len(packed['sources']), 40)
        self.assertGreater(len(packed['sources']), 1)
        self.assertNotIn('prompt_template', packed)
        json.dumps(packed, allow_nan=False)

    def test_full_short_posts_and_original_provenance_remain(self):
        rows = [source(1), source(2, group='comparison')]
        packed = pack_evidence({'sources': rows}, budget_tokens=2000)
        self.assertEqual(packed['sources'], rows)
        self.assertFalse(packed['context_pack']['text_shortened'])
        self.assertEqual(packed, pack_evidence(packed, budget_tokens=2000))

    def test_popular_interleaves_control_and_distinct_authors(self):
        rows = [source(1, author=1), source(2, author=1), source(3, author=2),
                source(4, group='comparison'), source(5, group='comparison')]
        packed = pack_evidence({'sources': rows}, budget_tokens=3000)
        self.assertEqual([row['post_id'] for row in packed['sources']], [1, 4, 3, 5, 2])
        self.assertEqual(packed['context_pack']['selected_groups']['comparison'], 2)

    def test_reporting_keeps_cross_account_duplicates_and_rotates(self):
        rows = [source(1, author=1), source(2, author=1), source(3, author=2), source(4, author=3)]
        packed = pack_evidence({'sources': rows}, mode='reporting', budget_tokens=1800)
        self.assertGreaterEqual(len(packed['sources']), 3)
        self.assertEqual([row['author_id'] for row in packed['sources'][:3]], [1, 2, 3])
        self.assertEqual(packed['sources'][0]['text'], packed['sources'][1]['text'])

    def test_retrieval_order_stays_ranked(self):
        rows = [source(3), source(1), source(2)]
        packed = pack_evidence({'sources': rows}, mode='chat', budget_tokens=2000)
        self.assertEqual([row['post_id'] for row in packed['sources']], [3, 1, 2])

    def test_long_source_has_real_sentence_and_offsets(self):
        text = 'Unrelated sentence. ' * 120 + 'Important climate evidence is here. ' + 'Other detail. ' * 120
        row = source(1, text)
        row['matched_terms'] = ['climate']
        packed = pack_evidence({'sources': [row]}, budget_tokens=2000)
        selected = packed['sources'][0]
        self.assertIn('climate', selected['text'])
        self.assertEqual(text[selected['excerpt_start']:selected['excerpt_end']], selected['text'])
        self.assertEqual(selected['excerpt_offset_unit'], 'unicode_codepoints')
        self.assertTrue(selected['text_truncated'])
        self.assertEqual(selected['coverage'], 'excerpt')

    def test_huge_metadata_is_bounded_and_disclosed(self):
        evidence = {key: [{'text': '中' * 5000, 'facts': list(range(100))}] * 500
                    for key in ('summary', 'features', 'research_context', 'reporting_context', 'scoring_context')}
        evidence['sources'] = [source(1)]
        packed = pack_evidence(evidence, budget_tokens=2000)
        self.assertLessEqual(estimate_tokens(packed), 2000)
        self.assertTrue(packed['context_pack']['metadata_shortened_fields'])
        self.assertEqual(len(packed['sources']), 1)

    def test_reporting_preserves_deep_peer_measurements_and_unknown_eligibility(self):
        accounts = []
        for index in range(3):
            accounts.append({'account_id': index, 'data_observed': {'sufficient': index != 2},
                'account_report_eligibility': {'status': 'unknown', 'official_action_count': None},
                'signals': [{'key': 'coordination', 'measurements': {'peer_post_ids': [11, 12],
                    'peer_account_ids': [21, 22], 'window_seconds': 120,
                    'coordination_examples': [{'post_ids': [11, 12], 'account_ids': [21, 22]}]}}]})
        packed = pack_evidence({'reporting_context': {'accounts': accounts},
            'sources': [source(11), source(12)]}, mode='reporting', budget_tokens=8000)
        self.assertLessEqual(estimate_tokens(packed), 8000)
        for row in packed['reporting_context']['accounts']:
            measurements = row['signals'][0]['measurements']
            self.assertEqual(measurements['peer_post_ids'], [11, 12])
            self.assertEqual(measurements['coordination_examples'][0]['account_ids'], [21, 22])
            self.assertEqual(row['account_report_eligibility']['status'], 'unknown')
            self.assertIsNone(row['account_report_eligibility']['official_action_count'])
        self.assertFalse(packed['reporting_context']['accounts'][2]['data_observed']['sufficient'])

    def test_configuration_is_strict_and_diagnostics_are_not_actual_tokens(self):
        with patch.dict(os.environ, {'CTN_AI_EVIDENCE_TOKENS': '4096'}):
            self.assertEqual(evidence_budget(), 4096)
        for value in ('bad', '100', '100000'):
            with patch.dict(os.environ, {'CTN_AI_EVIDENCE_TOKENS': value}):
                with self.assertRaises(ValueError):
                    evidence_budget()
        for value in (True, 100, 80000, '2000'):
            with self.assertRaises(ValueError):
                pack_evidence({'sources': []}, budget_tokens=value)
        packed = pack_evidence({'sources': []}, budget_tokens=512)
        self.assertIn('not_provider_tokenizer', packed['context_pack']['estimator'])
        self.assertEqual(packed['context_pack']['estimated_tokens'], estimate_tokens(packed))


if __name__ == '__main__':
    unittest.main()
