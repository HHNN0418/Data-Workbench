"""Deterministic retrieval tests; these never call a cloud API or load models."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ctn_data.ai_providers import ProviderError
from ctn_data.local_rag import bm25_scores, rank_sources, reciprocal_rank_fusion, tokenize


class FakeEmbedding:
    cache_key = 'fake-endpoint:model:v1:2d'

    def __init__(self):
        self.document_batches = []
        self.query_calls = 0

    def encode(self, texts, *, is_query=False):
        if is_query:
            self.query_calls += 1
        else:
            self.document_batches.append(list(texts))
        # Fixed concepts verify vector plumbing, not model linguistic quality.
        return [[1., .01] if any(term in text.casefold() for term in
                                ('铁路', 'rail', 'transport safety', 'commuter'))
                else [.01, 1.] for text in texts]


class FakeReranker:
    def predict(self, pairs):
        return [1. if 'independent source' in text else .1 for _, text in pairs]


class LocalRagTests(unittest.TestCase):
    def source(self, identifier, text, author=1, **extra):
        return {'id': identifier, 'text': text, 'author_id': author,
                'group': 'search', 'metrics': {'view_count': 10}, **extra}

    def test_bm25_unicode_and_document_length(self):
        scores = bm25_scores(['铁路运输安全改善', '美食餐馆推荐', '铁路运输安全改善 ' + '无关 ' * 60], '铁路安全')
        self.assertGreater(scores[0], scores[1])
        self.assertGreater(scores[0], scores[2])
        self.assertIn('café', tokenize('Café railway'))
        self.assertIn('安全', tokenize('铁路安全'))

    def test_rrf_combines_independent_ranks_without_double_count(self):
        scores = reciprocal_rank_fusion([['a', 'b', 'a'], ['b', 'c']])
        self.assertAlmostEqual(scores['a'], 1 / 61)
        self.assertAlmostEqual(scores['b'], 1 / 62 + 1 / 61)
        self.assertGreater(scores['b'], scores['a'])

    def test_lexical_mode_is_truthfully_reported_without_backends(self):
        ranked, info = rank_sources([self.source('food', 'food'), self.source('rail', 'rail safety')], 'rail', diversify=False)
        self.assertEqual(ranked[0]['id'], 'rail')
        self.assertEqual(info['mode'], 'lexical_only')
        self.assertEqual(info['embedding_status'], 'not_configured')
        self.assertIsNone(ranked[0]['retrieval']['semantic'])

    def test_vectors_retrieve_paraphrase_across_languages(self):
        rows = [self.source('food', 'local restaurants'), self.source('rail', '铁路运输政策改善通勤安全')]
        ranked, info = rank_sources(rows, 'transport safety', embedding_backend=FakeEmbedding(), cache_path=False)
        self.assertEqual(ranked[0]['id'], 'rail')
        self.assertEqual(info['mode'], 'hybrid')
        self.assertTrue(info['semantic_coverage_complete'])
        self.assertGreater(ranked[0]['retrieval']['semantic'], ranked[1]['retrieval']['semantic'])

    def test_reranker_controls_order_without_mutating_metadata(self):
        rows = [self.source('one', 'rail rail rail'), self.source('two', 'rail independent source')]
        ranked, info = rank_sources(rows, 'rail', diversify=False, reranker_backend=FakeReranker())
        self.assertEqual(ranked[0]['id'], 'two')
        self.assertEqual(info['reranker_status'], 'ready')
        self.assertEqual(rows[0]['text'], 'rail rail rail')
        self.assertNotIn('retrieval', rows[0])

    def test_reporting_preserves_identical_posts_from_different_accounts(self):
        rows = [self.source('p1', 'same coordinated message', 1, created_at='2026-10-01'),
                self.source('p2', 'same coordinated message', 2, created_at='2026-10-02')]
        ranked, _ = rank_sources(rows, 'coordinated', diversify=False)
        self.assertEqual({row['id'] for row in ranked}, {'p1', 'p2'})
        self.assertEqual(ranked[0]['duplicate_count'], 2)
        self.assertEqual({row['author_id'] for row in ranked[0]['duplicate_sources']}, {1, 2})
        self.assertEqual(ranked[0]['metrics']['view_count'], 10)

    def test_diversity_adds_author_coverage(self):
        rows = [self.source('a', 'rail safety a', 1), self.source('b', 'rail safety b', 1),
                self.source('c', 'rail safety c', 2)]
        ranked, _ = rank_sources(rows, 'rail safety', limit=2, diversify=True)
        self.assertEqual({row['author_id'] for row in ranked}, {1, 2})

    def test_original_chunk_offsets_and_source_identity_are_retained(self):
        body = ('background text ' * 80) + 'rarekeyword exact evidence.' + (' unrelated ' * 80)
        rows = [self.source('long', 'preview', _index_text=body)]
        ranked, _ = rank_sources(rows, 'rarekeyword', limit=1)
        row = ranked[0]
        self.assertEqual(row['id'], 'long')
        self.assertIn('rarekeyword', row['text'])
        self.assertEqual(body[row['retrieval']['chunk_start']:row['retrieval']['chunk_end']], row['text'])
        self.assertGreater(row['text_excerpt_start'], 0)
        self.assertTrue(row['text_truncated'])
        self.assertNotIn('_index_text', row)
        self.assertEqual(rows[0]['_index_text'], body)

    def test_cache_does_not_expand_authorized_scope_or_stale_metrics(self):
        backend = FakeEmbedding()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cache.sqlite3'
            visible = self.source('visible', 'rail safety')
            hidden = self.source('formerly-visible', 'rail policy', author=22)
            rank_sources([visible, hidden], 'rail', embedding_backend=backend, cache_path=path)
            visible['metrics']['view_count'] = 99
            ranked, info = rank_sources([visible], 'rail', embedding_backend=backend, cache_path=path)
            self.assertEqual([row['id'] for row in ranked], ['visible'])
            self.assertEqual(ranked[0]['metrics']['view_count'], 99)
            self.assertGreaterEqual(info['cache_hits'], 1)
            with closing(sqlite3.connect(path)) as conn:
                columns = [row[1] for row in conn.execute('PRAGMA table_info(embeddings)')]
                stored = json.dumps(conn.execute('SELECT * FROM embeddings').fetchall())
            self.assertEqual(columns, ['model', 'digest', 'vector'])
            self.assertNotIn('formerly-visible', stored)
            self.assertNotIn('rail safety', stored)
            before = len(backend.document_batches)
            backend.cache_key = 'fake-endpoint:model:v2:2d'
            rank_sources([visible], 'rail', embedding_backend=backend, cache_path=path)
            self.assertEqual(len(backend.document_batches), before + 1)

    def test_explicit_cloud_failure_is_actionable_instead_of_silent_fallback(self):
        class Broken:
            def encode(self, texts, *, is_query=False):
                raise ProviderError('test cloud unavailable', 502)
        with self.assertRaisesRegex(ProviderError, 'test cloud unavailable'):
            rank_sources([self.source('p', 'rail')], 'rail', embedding_backend=Broken(), cache_path=False)
        class BrokenReranker:
            def predict(self, pairs):
                return [float('nan')] * len(pairs)
        with self.assertRaises(ProviderError):
            rank_sources([self.source('p', 'rail')], 'rail', reranker_backend=BrokenReranker())

    def test_cloud_work_and_batch_sizes_are_bounded(self):
        backend = FakeEmbedding()
        rows = [self.source(str(index), 'rail ' + str(index), author=index) for index in range(250)]
        _, info = rank_sources(rows, 'rail', embedding_backend=backend, cache_path=False)
        self.assertLessEqual(info['semantic_chunks'], 200)
        self.assertFalse(info['semantic_coverage_complete'])
        self.assertTrue(all(len(batch) <= 32 for batch in backend.document_batches))
        self.assertLessEqual(info['candidate_chunks'], 80)


if __name__ == '__main__':
    unittest.main()
