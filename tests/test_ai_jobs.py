"""Interactive jobs use mocked providers and temporary databases only."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from ctn_data.ai_jobs import AnalysisJobs
from ctn_data.server import create_app
from tests.auth_helpers import login
from tests.test_ai_routes import EVIDENCE


class JobManagerTests(unittest.TestCase):
    def test_watcher_start_failure_releases_slot_and_never_executes_provider(self):
        slots = threading.BoundedSemaphore(2)
        jobs = AnalysisJobs(slots)
        start = threading.Thread.start
        called = []
        def start_or_fail(thread):
            if thread.name == 'ctn-ai-job-control':
                raise RuntimeError('synthetic thread resource failure')
            return start(thread)
        with patch.object(threading.Thread, 'start', start_or_fail):
            item = jobs.start('owner', lambda cancel, progress: called.append(True))
            jobs.jobs[item['id']]['thread'].join(2)
        self.assertEqual(jobs.get('owner', item['id'])['state'], 'failed')
        self.assertEqual(called, [])
        self.assertTrue(slots.acquire(blocking=False))
        self.assertTrue(slots.acquire(blocking=False))

    def test_ownership_cancellation_and_slot_lifetime(self):
        slots = threading.BoundedSemaphore(2)
        jobs = AnalysisJobs(slots)
        entered, exited = threading.Event(), threading.Event()

        def execute(cancelled, progress):
            progress('写作研究')
            entered.set()
            cancelled.wait(3)
            exited.wait(3)
            return {'text': 'never return a cancelled result'}

        item = jobs.start('member-a', execute)
        self.assertTrue(entered.wait(1))
        with self.assertRaises(HTTPException) as failure:
            jobs.get('member-b', item['id'])
        self.assertEqual(failure.exception.status_code, 404)
        self.assertEqual(jobs.get('member-a', item['id'])['stage'], '写作研究')
        jobs.cancel('member-a', item['id'])
        self.assertTrue(slots.acquire(blocking=False))
        self.assertFalse(slots.acquire(blocking=False), 'slot retained until provider has stopped')
        slots.release()
        exited.set()
        jobs.jobs[item['id']]['thread'].join(2)
        self.assertEqual(jobs.get('member-a', item['id'])['state'], 'cancelled')
        self.assertIsNone(jobs.get('member-a', item['id'])['result'])
        self.assertTrue(slots.acquire(blocking=False))
        self.assertTrue(slots.acquire(blocking=False))

    def test_no_reader_or_elapsed_time_cancels_work(self):
        jobs = AnalysisJobs(threading.BoundedSemaphore(2))
        entered = threading.Event()
        def execute(cancelled, progress):
            entered.set()
            cancelled.wait(5)
            return {'text': 'cancelled'}
        item = jobs.start('owner', execute)
        self.assertTrue(entered.wait(1))
        job = jobs.jobs[item['id']]
        job['monotonic_started'] -= 100000
        time.sleep(1.1)
        self.assertFalse(job['cancel'].is_set())
        jobs.cancel('owner', item['id'])
        job['thread'].join(2)
        self.assertEqual(jobs.get('owner', item['id'])['state'], 'cancelled')

    def test_unexpected_errors_are_redacted(self):
        jobs = AnalysisJobs(threading.BoundedSemaphore(2))
        def broken(cancel, progress):
            raise KeyError('secret-provider-url')
        item = jobs.start('owner', broken)
        jobs.jobs[item['id']]['thread'].join(2)
        result = jobs.get('owner', item['id'])
        self.assertEqual(result['state'], 'failed')
        self.assertNotIn('secret-provider-url', str(result))


class AnalysisJobRoutesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.temp.name) / 'jobs.sqlite3')
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.token = login(self.client)
        self.headers = {'X-CTN-Token': self.token}
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.client.close)
        self.addCleanup(self.app.state.ai_jobs.stop)

    def test_start_returns_while_model_runs_and_cancel_prevents_second_step(self):
        entered = threading.Event()
        def complete(provider, messages, **kwargs):
            entered.set()
            self.assertTrue(kwargs['cancellation_event'].wait(3))
            return {'text': 'First stage [post:7]', 'model': 'mock'}
        with patch.object(self.app.state.ai_providers, 'get', return_value={'model': 'mock'}), \
             patch('ctn_data.ai_evidence.build_evidence', return_value=dict(EVIDENCE)), \
             patch('ctn_data.ai_providers.complete', side_effect=complete) as cloud:
            response = self.client.post('/api/ai/jobs', headers=self.headers,
                                        json={'provider_id': 'mock', 'mode': 'write'})
            self.assertEqual(response.status_code, 202, response.text)
            self.assertTrue(entered.wait(1))
            identifier = response.json()['id']
            self.assertEqual(self.client.get('/api/ai/jobs/' + identifier).json()['state'], 'running')
            response = self.client.post('/api/ai/jobs/' + identifier + '/cancel', headers=self.headers, json={})
            self.assertEqual(response.status_code, 200)
            self.app.state.ai_jobs.jobs[identifier]['thread'].join(2)
            self.assertEqual(self.client.get('/api/ai/jobs/' + identifier).json()['state'], 'cancelled')
            self.assertEqual(cloud.call_count, 1)

    def test_legacy_route_also_returns_a_cancellable_background_job(self):
        entered = threading.Event()
        def complete(provider, messages, **kwargs):
            entered.set()
            kwargs['cancellation_event'].wait(3)
            return {'text': 'cancelled', 'model': 'mock'}
        with patch.object(self.app.state.ai_providers, 'get', return_value={'model': 'mock'}), \
             patch('ctn_data.ai_evidence.build_evidence', return_value=dict(EVIDENCE)), \
             patch('ctn_data.ai_providers.complete', side_effect=complete) as cloud:
            response = self.client.post('/api/ai/run', headers=self.headers,
                                        json={'provider_id': 'mock', 'mode': 'write'})
            self.assertEqual(response.status_code, 202, response.text)
            self.assertTrue(entered.wait(1))
            identifier = response.json()['id']
            self.client.post('/api/ai/jobs/' + identifier + '/cancel', headers=self.headers, json={})
            self.app.state.ai_jobs.jobs[identifier]['thread'].join(2)
            self.assertEqual(cloud.call_count, 1)

    def test_cancel_during_cloud_retrieval_stops_transport_before_generation(self):
        from ctn_data.ai_providers import _TransportCancelled
        entered = threading.Event()
        def provider(identifier):
            return {'protocol': 'openai_embeddings' if identifier == 'embedding' else 'openai_chat',
                    'model': 'mock', 'base_url': 'https://example.invalid/v1', 'api_key': 'synthetic-key'}
        def evidence(*args, **kwargs):
            kwargs['embedding_backend'].encode(['synthetic source'] * 33)
            return dict(EVIDENCE)
        def transport(endpoint, body, headers, **kwargs):
            entered.set()
            self.assertTrue(kwargs['cancellation_event'].wait(3))
            raise _TransportCancelled()
        with patch.object(self.app.state.ai_providers, 'get', side_effect=provider), \
             patch('ctn_data.ai_evidence.build_evidence', side_effect=evidence), \
             patch('ctn_data.ai_providers._request_json', side_effect=transport) as request, \
             patch('ctn_data.ai_providers.complete') as cloud:
            response = self.client.post('/api/ai/jobs', headers=self.headers, json={
                'provider_id': 'text', 'embedding_provider_id': 'embedding', 'mode': 'write'})
            self.assertEqual(response.status_code, 202, response.text)
            identifier = response.json()['id']
            self.assertTrue(entered.wait(1))
            self.client.post(f'/api/ai/jobs/{identifier}/cancel', headers=self.headers, json={})
            self.app.state.ai_jobs.jobs[identifier]['thread'].join(2)
            self.assertEqual(self.client.get(f'/api/ai/jobs/{identifier}').json()['state'], 'cancelled')
            self.assertEqual(request.call_count, 1)
            cloud.assert_not_called()
            self.assertTrue(self.app.state.ai_jobs.slots.acquire(blocking=False))
            self.assertTrue(self.app.state.ai_jobs.slots.acquire(blocking=False))

    def test_internal_type_and_key_errors_are_500_without_details(self):
        for kind in (KeyError, TypeError):
            with self.subTest(kind=kind), patch('ctn_data.queries.overview', side_effect=kind('private-detail')):
                client = TestClient(self.app, base_url='http://127.0.0.1:8765', raise_server_exceptions=False)
                client.cookies.update(self.client.cookies)
                response = client.get('/api/overview')
                self.assertEqual(response.status_code, 500)
                self.assertNotIn('private-detail', response.text)
                client.close()


class ReadOnlyCollectionStatusTests(unittest.TestCase):
    def test_status_never_acquires_a_collection_lane(self):
        from ctn_data.collector import collection_lock, is_collecting
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'status.sqlite3'
            with patch('ctn_data.collector._lane_lock', side_effect=AssertionError('status must not take locks')):
                self.assertFalse(is_collecting(path))
            with collection_lock(path, 'feeds'):
                self.assertTrue(is_collecting(path))
                with collection_lock(path, 'counters'):
                    self.assertTrue(is_collecting(path))
            self.assertFalse(is_collecting(path))


if __name__ == '__main__':
    unittest.main()
