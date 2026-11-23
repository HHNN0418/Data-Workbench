from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from ctn_data import fact_bank
from ctn_data.server import create_app
from tests.auth_helpers import login


class FactRouteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        fact_bank.invalidate()
        self.app = create_app(self.directory / 'test.db', backup_dir=self.directory / 'backups')
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.cloud = patch('ctn_data.ai_providers.complete', side_effect=AssertionError('No live AI')).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(fact_bank.invalidate)

    def test_auth_and_csrf_are_required(self):
        self.assertEqual(self.client.get('/api/agents/facts/list').status_code, 401)
        login(self.client)
        self.assertEqual(self.client.post('/api/agents/facts/approve-pending', json={}).status_code, 403)

    def test_settings_list_decide_and_rules(self):
        headers = {'X-CTN-Token': login(self.client)}
        overview = self.client.get('/api/agents/facts/settings').json()
        self.assertEqual(overview['settings']['review_mode'], 'manual')
        self.assertIn('hospital', overview['concerns'])
        saved = self.client.post('/api/agents/facts/settings', headers=headers, json={
            'review_mode': 'auto', 'extract_enabled': False, 'provider_id': '',
            'expected_revision': overview['settings']['revision']})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()['settings']['review_mode'], 'auto')
        stale = self.client.post('/api/agents/facts/settings', headers=headers, json={
            'review_mode': 'manual', 'extract_enabled': False, 'provider_id': '',
            'expected_revision': overview['settings']['revision']})
        self.assertEqual(stale.status_code, 409)
        enable = self.client.post('/api/agents/facts/settings', headers=headers, json={
            'review_mode': 'manual', 'extract_enabled': True, 'provider_id': 'missing',
            'expected_revision': saved.json()['settings']['revision']})
        self.assertEqual(enable.status_code, 400)

        listing = self.client.get('/api/agents/facts/list', params={'status': 'active', 'layer': 'core'}).json()
        self.assertGreater(listing['total'], 30)
        fact = listing['items'][0]
        retired = self.client.post('/api/agents/facts/decision', headers=headers, json={
            'id': fact['id'], 'action': 'retire', 'expected_revision': fact['revision']})
        self.assertEqual(retired.status_code, 200, retired.text)
        self.assertEqual(retired.json()['status'], 'retired')
        created = self.client.post('/api/agents/facts/create', headers=headers, json={
            'claim': 'Pop-up clinics open in Crown Harbour this weekend.', 'concerns': ['access'],
            'source_label': 'Operator note'})
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()['status'], 'active')

        rules = self.client.get('/api/agents/facts/rules').json()
        rebuttal = rules['rebuttals'][0]
        updated = self.client.post('/api/agents/facts/rebuttal', headers=headers, json={
            'concern': rebuttal['concern'], 'no_argument': rebuttal['no_argument'], 'line': 'New line.',
            'fact_ids': rebuttal['fact_ids'], 'expected_revision': rebuttal['revision']})
        self.assertEqual(updated.status_code, 200, updated.text)
        added = self.client.post('/api/agents/facts/guardrail', headers=headers,
                                 json={'rule': 'Do not quote unnamed insiders.'})
        self.assertEqual(added.status_code, 200, added.text)
        self.cloud.assert_not_called()

    def test_prompts_are_readable_but_not_writable(self):
        headers = {'X-CTN-Token': login(self.client)}
        data = self.client.get('/api/agents/facts/prompts').json()
        self.assertEqual([p['key'] for p in data['prompts']], ['radar', 'decide', 'write', 'check', 'extract'])
        self.assertIn('{name}', next(p for p in data['prompts'] if p['key'] == 'decide')['text'])
        self.assertEqual(self.client.post('/api/agents/facts/prompts', json={}, headers=headers).status_code, 405)

    def test_extraction_runs_are_listed(self):
        login(self.client)
        empty = self.client.get('/api/agents/facts/extractions').json()
        self.assertEqual(empty['items'], [])
        self.assertEqual(empty['totals']['runs'], 0)
        from ctn_data.db import connect
        conn = connect(self.directory / 'test.db')
        with conn:
            conn.execute("INSERT INTO news_articles(article_key,site,title,first_seen,last_seen) "
                         "VALUES('a1','kingstonia-herald','Clinics open','2026-10-22T00:00:00Z','2026-10-22T00:00:00Z')")
            conn.execute("INSERT INTO fact_extractions(article_key,body_hash,status,accepted,dropped,attempts,"
                         "created_at,updated_at) VALUES('a1','h','done',2,1,1,'2026-10-22T00:00:00Z','2026-10-22T00:00:00Z')")
        conn.close()
        data = self.client.get('/api/agents/facts/extractions').json()
        self.assertEqual(data['items'][0]['title'], 'Clinics open')
        self.assertEqual(data['items'][0]['site'], 'Kingstonia Herald')
        self.assertEqual(data['totals']['accepted'], 2)

    def test_fact_page_is_served_for_direct_links(self):
        self.assertEqual(self.client.get('/ai/facts').status_code, 200)

    def test_agent_detail_route_still_resolves(self):
        login(self.client)
        self.assertEqual(self.client.get('/api/agents/00000000-0000-4000-8000-000000000000').status_code, 404)


if __name__ == '__main__':
    unittest.main()
