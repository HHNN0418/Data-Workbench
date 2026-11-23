import json
import threading
import unittest
from unittest.mock import patch

from ctn_data.cloud_rag import CloudEmbedding, CloudReranker
from ctn_data.ai_providers import ProviderError, _validate_url


def provider(protocol):
    return {'protocol': protocol, 'base_url': 'https://example.com/v1', 'model': 'multilingual', 'api_key': 'secret-test-key'}


class CloudRetrievalTests(unittest.TestCase):
    def test_cancellation_reaches_transport_and_prevents_later_embedding_batches(self):
        cancelled = threading.Event()
        def reply(endpoint, body, headers, **options):
            self.assertIs(options['cancellation_event'], cancelled)
            cancelled.set()
            return {'data': [{'index': i, 'embedding': [1, 1]}
                             for i in range(len(json.loads(body)['input']))]}
        with patch('ctn_data.ai_providers._request_json', side_effect=reply) as request:
            with self.assertRaises(ProviderError) as failed:
                CloudEmbedding(provider('openai_embeddings'), cancellation_event=cancelled).encode(
                    [f'post {i}' for i in range(33)])
        self.assertEqual(failed.exception.status_code, 499)
        self.assertEqual(request.call_count, 1)

    def test_cancelled_reranking_never_makes_a_new_request(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch('ctn_data.ai_providers._request_json') as request:
            with self.assertRaises(ProviderError) as failed:
                CloudReranker(provider('rerank'), cancellation_event=cancelled).predict([('q', 'post')])
        self.assertEqual(failed.exception.status_code, 499)
        request.assert_not_called()

    def test_embedding_batches_and_restores_response_index_order(self):
        requests = []
        def reply(endpoint, body, headers, **options):
            payload = json.loads(body)
            requests.append(payload)
            self.assertEqual(endpoint, 'https://example.com/v1/embeddings')
            self.assertEqual(headers['Authorization'], 'Bearer secret-test-key')
            self.assertEqual(payload['encoding_format'], 'float')
            return {'data': [{'index': i, 'embedding': [i + 1, 1]} for i in reversed(range(len(payload['input'])))]}
        with patch('ctn_data.ai_providers._request_json', side_effect=reply):
            vectors = CloudEmbedding(provider('openai_embeddings')).encode([f'post {i}' for i in range(33)])
        self.assertEqual([len(x['input']) for x in requests], [16, 16, 1])
        self.assertEqual(vectors[0], [1., 1.])
        self.assertEqual(vectors[15], [16., 1.])
        self.assertEqual(requests[0]['input'][0], 'post 0')

    def test_embedding_rejects_duplicates_nan_and_dimension_mismatch(self):
        for rows in ([{'index': 0, 'embedding': [1]}, {'index': 0, 'embedding': [1]}],
                     [{'index': 0, 'embedding': [float('nan')]}, {'index': 1, 'embedding': [1]}],
                     [{'index': 0, 'embedding': [1]}, {'index': 1, 'embedding': [1, 2]}]):
            with self.subTest(rows=rows), patch('ctn_data.ai_providers._request_json', return_value={'data': rows}):
                with self.assertRaises(ProviderError):
                    CloudEmbedding(provider('openai_embeddings')).encode(['a', 'b'])

    def test_rerank_maps_indices_back_to_original_order(self):
        with patch('ctn_data.ai_providers._request_json', return_value={'results': [
            {'index': 1, 'relevance_score': .8}, {'index': 0, 'relevance_score': .2}]}) as request:
            scores = CloudReranker(provider('rerank')).predict([('query', 'first'), ('query', 'second')])
        self.assertEqual(scores, [.2, .8])
        payload = json.loads(request.call_args.args[1])
        self.assertEqual(payload['documents'], ['first', 'second'])
        self.assertEqual(payload['top_n'], 2)
        self.assertFalse(payload['return_documents'])

    def test_rerank_rejects_partial_or_duplicate_response_and_no_retry(self):
        for rows in ([{'index': 0, 'relevance_score': .2}],
                     [{'index': 0, 'relevance_score': .2}, {'index': 0, 'relevance_score': .8}]):
            with patch('ctn_data.ai_providers._request_json', return_value={'results': rows}) as request:
                with self.assertRaises(ProviderError):
                    CloudReranker(provider('rerank')).predict([('q', 'a'), ('q', 'b')])
                self.assertEqual(request.call_count, 1)

    def test_connection_identity_excludes_key_and_inputs_are_bounded(self):
        first = provider('openai_embeddings')
        second = {**first, 'api_key': 'other-secret'}
        self.assertEqual(CloudEmbedding(first).cache_key, CloudEmbedding(second).cache_key)
        self.assertNotEqual(CloudEmbedding(first).cache_key, CloudEmbedding({**first, 'model': 'new'}).cache_key)
        self.assertNotIn(first['api_key'], CloudEmbedding(first).cache_key)
        with patch('ctn_data.ai_providers._request_json') as request:
            with self.assertRaises(ValueError):
                CloudEmbedding(first).encode(['x'] * 201)
            request.assert_not_called()
        for suffix in ('chat/completions', 'embeddings', 'rerank'):
            self.assertEqual(_validate_url('https://example.com/v1/' + suffix), 'https://example.com/v1')

    def test_network_error_does_not_expose_upstream_or_key(self):
        with patch('ctn_data.ai_providers._request_json', side_effect=OSError('secret-test-key upstream body')):
            with self.assertRaises(ProviderError) as error:
                CloudEmbedding(provider('openai_embeddings')).encode(['post'])
        self.assertNotIn('secret-test-key', str(error.exception))
        self.assertNotIn('upstream', str(error.exception))
