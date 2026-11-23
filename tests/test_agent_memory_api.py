"""Authenticated archive inspection and transaction integration, offline only."""
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient
from ctn_data import agent_memory
from ctn_data.agent_control import AgentStore
from ctn_data.db import connect
from ctn_data.server import create_app
from tests.auth_helpers import login


class AgentMemoryAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / 'memory.sqlite3'
        self.app = create_app(self.path, backup_dir=self.directory / 'backups')
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.controller = self.app.state.agent_control
        self.controller.save({'name': 'Maya', 'username': 'memory_maya'})
        self.controller.save({'name': 'Ethan', 'username': 'memory_ethan'})
        roles = self.controller.store.read()['agents'].values()
        self.aid = next(row['id'] for row in roles if row['name'] == 'Maya')
        self.bid = next(row['id'] for row in roles if row['name'] == 'Ethan')
        self.addCleanup(patch.stopall)
        patch('ctn_data.ai_providers.complete', side_effect=AssertionError('Offline only')).start()

    def seed(self):
        def add(data, conn):
            for aid, body in ((self.aid, 'Ferry meeting at the old harbour.'),
                              (self.bid, 'ETHAN_PRIVATE_MEMORY')):
                for number in range(1, 206):
                    agent_memory.record_read(data, aid, [{
                        'id': f'post:{number}', 'post_id': number, 'author_id': 18,
                        'version': 'v1', 'text': body if number == 1 else f'Later discussion {number}',
                        'title': 'Ferry access'}], 1792452000 + number)
        self.controller.store.change(add)

    def history(self, **params):
        return self.client.get('/api/agents/memory-history', params={'agent_id': self.aid, **params})

    def test_history_requires_login_and_valid_filters(self):
        self.assertEqual(self.history().status_code, 401)
        login(self.client)
        for params in ({'kind': 'invalid'}, {'limit': 0}, {'limit': 101}, {'offset': -1}, {'q': 'x' * 241}):
            self.assertEqual(self.history(**params).status_code, 400)
        self.assertEqual(self.history(agent_id=str(uuid.uuid4())).status_code, 404)

    def test_store_archives_before_eviction_and_api_search_is_role_scoped(self):
        self.seed()
        login(self.client)
        result = self.history(q='old harbour', kind='reading').json()
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['source_ids'], ['post:1'])
        self.assertEqual(result['counts']['reading'], 205)
        self.assertNotIn('ETHAN_PRIVATE_MEMORY', json.dumps(result))
        self.assertEqual(self.history(q='ETHAN_PRIVATE_MEMORY').json()['total'], 0)
        self.assertTrue(self.history(limit=20).json()['has_more'])
        self.assertEqual(len(self.history(offset=200, limit=20).json()['items']), 5)
        agent = next(row for row in self.controller.snapshot()['agents'] if row['id'] == self.aid)
        self.assertEqual(agent['memory_view']['long_term']['counts']['reading'], 205)
        data = self.controller.store.read()
        self.assertNotIn('_memory_records', data)
        self.assertEqual(len(data['memory'][self.aid]['episodes']['readings']), 180)

    def test_clear_backs_up_archive_and_forgets_only_one_role(self):
        self.seed()
        token = login(self.client)
        result = self.client.post('/api/agents/reset-memory', json={'agent_id': self.aid},
                                  headers={'X-CTN-Token': token})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.history().json()['total'], 0)
        self.assertEqual(self.history(agent_id=self.bid).json()['total'], 205)
        backups = list((self.controller.secret_dir.parent / 'agent-memory-backups').glob('*.json'))
        self.assertEqual(len(backups), 1)
        saved = json.loads(backups[0].read_text(encoding='utf-8'))
        self.assertEqual(len(saved['long_term_memory']), 205)
        data = self.controller.store.read()
        self.assertEqual(self.controller.recall_memory(data, data['agents'][self.aid], [])['readings'], [])

    def test_maintenance_open_does_not_initialize_or_require_archive_schema(self):
        # The draining CLI runs new Python against the old live schema first.
        conn = connect(self.path)
        with conn:
            conn.execute('DROP TABLE agent_memory_links')
            conn.execute('DROP TABLE agent_memory_records')
        conn.close()
        store = AgentStore.open_existing(self.path)
        store.change(lambda data, conn: data['rate'].update(memory_maintenance_probe=True))
        self.assertTrue(store.read()['rate']['memory_maintenance_probe'])
        # A normal replacement startup initializes and bootstraps the archive.
        self.assertTrue(AgentStore(self.path).read()['rate']['memory_maintenance_probe'])


if __name__ == '__main__':
    unittest.main()
