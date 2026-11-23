"""Focused regressions for retrieval-only chat and publication-time ordering."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from ctn_data.ai_routes import ChatRequest
from ctn_data.db import connect, initialize
from ctn_data.ingest import save_posts
from ctn_data.queries import posts, post_detail
from ctn_data.server import create_app
from tests.auth_helpers import completed_job, login


EVIDENCE = {
    'sources': [{'id': 'post:7', 'kind': 'post', 'post_id': 7,
                 'title': 'Evidence', 'text': 'A source passage', 'metrics': {}}],
    'summary': {'scope_count': 1}, 'features': [], 'caveats': [],
    'retrieval': {'method': 'hybrid'},
}


class RetrievalOnlyChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(Path(self.temp.name) / 'chat.sqlite3')
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.token = login(self.client)
        self.addCleanup(self.client.close)
        self.addCleanup(self.app.state.ai_jobs.stop)
        self.evidence = self.mock('ctn_data.ai_evidence.build_evidence',
                                  side_effect=lambda *args, **kwargs: deepcopy(EVIDENCE))
        self.mock('ctn_data.ai_evidence.build_task_prompt', return_value='Source [post:7]')
        self.embedding = self.mock('ctn_data.cloud_rag.CloudEmbedding')
        self.reranker = self.mock('ctn_data.cloud_rag.CloudReranker')
        provider = patch.object(self.app.state.ai_providers, 'get',
                                side_effect=lambda pid: {'id': pid, 'protocol': 'openai_chat'})
        self.providers = provider.start()
        self.addCleanup(provider.stop)
        self.complete = self.mock('ctn_data.ai_providers.complete',
                                  return_value={'text': 'Answer [post:7]', 'model': 'fixture'})

    def mock(self, target, **kwargs):
        mocked = patch(target, **kwargs)
        result = mocked.start()
        self.addCleanup(mocked.stop)
        return result

    def post_chat(self, payload):
        return completed_job(self.client, self.client.post('/api/ai/chat',
            json=payload.model_dump(), headers={'X-CTN-Token': self.token}))

    def chat(self, payload):
        response = self.post_chat(payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_selected_retrieval_services_work_without_a_text_provider(self):
        for ids in ({'embedding_provider_id': 'vector'}, {'rerank_provider_id': 'ranker'},
                    {'embedding_provider_id': 'vector', 'rerank_provider_id': 'ranker'}):
            with self.subTest(ids=ids):
                self.embedding.reset_mock()
                self.reranker.reset_mock()
                result = self.chat(ChatRequest(question='find similar passages', **ids))
                options = self.evidence.call_args.kwargs
                if 'embedding_provider_id' in ids:
                    self.assertIs(options['embedding_backend'], self.embedding.return_value)
                else:
                    self.assertNotIn('embedding_backend', options)
                    self.embedding.assert_not_called()
                if 'rerank_provider_id' in ids:
                    self.assertIs(options['reranker_backend'], self.reranker.return_value)
                else:
                    self.assertNotIn('reranker_backend', options)
                    self.reranker.assert_not_called()
                self.assertEqual(options['cache_path'].parent, Path(self.temp.name))
                self.assertFalse(result['local_only'])
                self.assertTrue(result['retrieval_only'])
                self.assertIn('所选检索服务', result['text'])
                self.assertIn('未调用文字生成模型', result['text'])
                self.assertNotIn('尚未调用云端模型', result['text'])
                self.complete.assert_not_called()

    def test_keyword_only_chat_remains_local_and_avoids_cloud_services(self):
        result = self.chat(ChatRequest(question='find passages'))
        self.assertTrue(result['local_only'])
        self.assertTrue(result['retrieval_only'])
        self.assertIn('本地关键词检索', result['text'])
        self.embedding.assert_not_called()
        self.reranker.assert_not_called()
        self.complete.assert_not_called()

    def test_no_match_after_selected_retrieval_is_not_labeled_purely_local(self):
        self.evidence.side_effect = lambda *args, **kwargs: {**deepcopy(EVIDENCE), 'sources': []}
        result = self.chat(ChatRequest(question='no match', embedding_provider_id='vector'))
        self.assertEqual(result['sources'], [])
        self.assertFalse(result['local_only'])
        self.assertTrue(result['retrieval_only'])
        self.assertIn('未找到匹配依据', result['text'])
        self.complete.assert_not_called()

    def test_followup_keeps_selected_retrieval_services(self):
        self.evidence.side_effect = [{**deepcopy(EVIDENCE), 'sources': []}, deepcopy(EVIDENCE)]
        result = self.chat(ChatRequest(question='它们呢？', rerank_provider_id='ranker',
                                      history=[{'role': 'user', 'content': 'education'}]))
        self.assertEqual(self.evidence.call_count, 2)
        for call in self.evidence.call_args_list:
            self.assertIs(call.kwargs['reranker_backend'], self.reranker.return_value)
        self.assertTrue(result['retrieval']['used_previous_question'])
        self.assertFalse(result['local_only'])
        self.complete.assert_not_called()

    def test_retrieval_failure_propagates_and_releases_concurrency_slot(self):
        self.embedding.side_effect = ValueError('invalid retrieval provider')
        for _ in range(3):
            response = self.post_chat(ChatRequest(question='find passages', embedding_provider_id='vector'))
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn('invalid retrieval provider', response.text)
        self.embedding.side_effect = None
        self.assertFalse(self.chat(ChatRequest(question='find passages',
                                              embedding_provider_id='vector'))['local_only'])
        self.complete.assert_not_called()

    def test_text_generation_still_returns_model_output(self):
        result = self.chat(ChatRequest(question='explain passages', provider_id='writer',
                                      embedding_provider_id='vector'))
        self.assertFalse(result['local_only'])
        self.assertFalse(result['retrieval_only'])
        self.assertEqual(result['model'], 'fixture')
        self.complete.assert_called_once()


class PublicationOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'posts.sqlite3'
        initialize(self.db)
        connection = connect(self.db)
        try:
            with connection:
                values = ['2026-10-20T12:00:00Z', '2026-10-20T12:00:00.100000Z',
                          '2026-10-20T22:30:00+11:00', '2026-10-20T10:00:00-03:00',
                          '2026-10-20T12:00:00+00:00', 'not-a-timestamp', None]
                save_posts(connection, [dict(post_id=i, body=f'post {i}', kind='post', created_at=at)
                                        for i, at in enumerate(values, 1)],
                           'latest', '2026-10-20T14:00:00Z')
                save_posts(connection, [{'post_id': 1, 'rank_position': 2},
                                        {'post_id': 3, 'rank_position': 1}],
                           'trending', '2026-10-20T14:01:00Z')
        finally:
            connection.close()

    def test_all_and_latest_sort_real_instants_and_keep_pagination_stable(self):
        for feed in ('all', 'latest'):
            with self.subTest(feed=feed):
                full = posts(self.db, {'feed': feed})
                self.assertEqual([row['post_id'] for row in full['items']], [4, 2, 5, 1, 3, 7, 6])
                pages = [posts(self.db, {'feed': feed, 'limit': 2, 'offset': offset})
                         for offset in (0, 2, 4, 6)]
                self.assertEqual([row['post_id'] for page in pages for row in page['items']],
                                 [4, 2, 5, 1, 3, 7, 6])

    def test_trending_stays_ordered_by_rank(self):
        result = posts(self.db, {'feed': 'trending'})
        self.assertEqual([row['post_id'] for row in result['items']], [3, 1])

    def test_post_detail_relations_sort_offsets_and_fractional_seconds(self):
        connection = connect(self.db)
        try:
            with connection:
                save_posts(connection, [
                    {'post_id': 100, 'body': 'root'},
                    {'post_id': 11, 'kind': 'reply', 'reply_to_post_id': 100,
                     'created_at': '2026-10-20T22:30:00+11:00'},
                    {'post_id': 12, 'kind': 'reply', 'reply_to_post_id': 100,
                     'created_at': '2026-10-20T12:00:00Z'},
                    {'post_id': 21, 'kind': 'repost', 'repost_of_post_id': 100,
                     'created_at': '2026-10-20T12:00:00Z'},
                    {'post_id': 22, 'kind': 'repost', 'repost_of_post_id': 100,
                     'created_at': '2026-10-20T12:00:00.100000Z'},
                ], 'latest', '2026-10-20T14:02:00Z')
        finally:
            connection.close()
        result = post_detail(self.db, 100)
        self.assertEqual([row['post_id'] for row in result['replies']], [11, 12])
        self.assertEqual([row['post_id'] for row in result['reposts']], [21, 22])


if __name__ == '__main__':
    unittest.main()
