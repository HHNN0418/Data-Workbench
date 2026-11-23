"""The dashboard's compact team snapshot, the views it defers, and write receipts."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient

from ctn_data import agent_feedback
from ctn_data.agent_control import AgentController, COMPACT_HIDDEN_DRAFT_FIELDS
from ctn_data.db import utc_now
from ctn_data.server import create_app
from tests.auth_helpers import login

SOURCE = {'id': 'post:7', 'kind': 'post', 'post_id': 7, 'title': 'Post 7', 'text': 'A long thread ' * 50}
HEAVY = {'content_check': {'ok': True}, 'angle': 'local cost', 'tracking': {'samples': [1, 2]},
         'usage': {'tokens': 900}, 'fact_ids': ['f1'], 'competition_model': {'model': 'm'}}


def seed(controller):
    aid = controller.snapshot()['agents'][0]['id']
    pending, published = str(uuid.uuid4()), str(uuid.uuid4())

    def change(data, conn):
        agent = data['agents'][aid]
        base = {'agent_id': aid, 'account_id': None, 'body': 'Reply text', 'action': 'reply',
                'reason': 'Because', 'source_ids': ['post:7'], 'sources': [dict(SOURCE)],
                'persona_version': agent['version'], 'persona_revision': agent['persona_revision'],
                'version': 1, **HEAVY}
        data['drafts'][pending] = {**base, 'id': pending, 'state': 'pending', 'created_at': utc_now()}
        data['drafts'][published] = {**base, 'id': published, 'state': 'published', 'post_id': 9,
                                     'created_at': utc_now(), 'published_at': utc_now()}
        memory = data['memory'].setdefault(aid, {})
        memory['inbox'] = [{'id': 31, 'notification_id': 31, 'post_id': 7, 'kind': 'reply',
                            'status': 'unresolved', 'created_at': utc_now(), 'source_id': 'post:7',
                            'source': dict(SOURCE)}]
        memory['last_sources'] = [dict(SOURCE)]
        memory['activity'] = [{'draft_id': published, 'action': 'reply', 'state': 'published'}]
    controller.store.change(change)
    return aid, pending, published


class CompactSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.controller = AgentController(directory / 'roles.sqlite3', directory / 'secrets', clock=lambda: 1792548000)
        self.controller.save({'name': 'Maya', 'username': 'compact_maya'})
        self.aid, self.pending, self.published = seed(self.controller)

    def test_compact_view_leaves_out_only_deferred_material(self):
        full, compact = self.controller.snapshot(), self.controller.snapshot(compact=True)
        role, full_role = compact['agents'][0], full['agents'][0]
        for key in ('memory_view', 'last_sources', 'activity'):
            self.assertIn(key, full_role)
            self.assertNotIn(key, role)
        self.assertEqual(full_role['inbox'][0]['source']['id'], 'post:7')
        self.assertNotIn('source', role['inbox'][0])
        self.assertEqual({k: v for k, v in full_role['inbox'][0].items() if k != 'source'}, role['inbox'][0])
        drafts = {row['id']: row for row in compact['drafts']}
        full_drafts = {row['id']: row for row in full['drafts']}
        # An unresolved draft keeps its sources for review.
        self.assertEqual(drafts[self.pending]['sources'], full_drafts[self.pending]['sources'])
        self.assertNotIn('sources_omitted', drafts[self.pending])
        self.assertNotIn('sources', drafts[self.published])
        self.assertEqual(drafts[self.published]['sources_omitted'], len(full_drafts[self.published]['sources']))
        for row in drafts.values():
            self.assertFalse(set(COMPACT_HIDDEN_DRAFT_FIELDS) & set(row))
            self.assertNotIn('send_token', row)
        self.assertIn('content_check', full_drafts[self.pending])
        for key in ('body', 'reason', 'status', 'source_ids', 'post_id', 'published_at'):
            self.assertEqual(drafts[self.published].get(key), full_drafts[self.published].get(key))
        for key in ('settings', 'events', 'budgets', 'queues', 'maintenance', 'interaction_policy'):
            self.assertEqual(compact[key], full[key])

    def test_deferred_views_match_the_full_snapshot(self):
        full = self.controller.snapshot()
        role = full['agents'][0]
        view = self.controller.memory_view(self.aid)
        self.assertEqual(view['memory_view'], role['memory_view'])
        self.assertEqual(view['last_sources'], role['last_sources'])
        self.assertEqual(view['activity'], role['activity'])
        self.assertEqual(self.controller.draft(self.published),
                         next(row for row in full['drafts'] if row['id'] == self.published))

    def test_compact_feedback_is_bounded_without_touching_the_cache(self):
        rows = {'recent_posts': [{'post_id': n} for n in range(20)],
                'actions': [{'draft_id': str(n), 'status': 'published'} for n in range(20)]}
        result = {'account_id': None, 'status': 'unbound', **rows}
        with patch.object(agent_feedback, 'snapshot_for', return_value=result):
            compact = self.controller.snapshot(compact=True)['agents'][0]['feedback']
            full = self.controller.snapshot()['agents'][0]['feedback']
        self.assertEqual(compact['recent_posts'], rows['recent_posts'][:3])
        self.assertEqual(compact['actions'], rows['actions'][:5])
        self.assertEqual(compact['status'], 'unbound')
        self.assertEqual(len(full['recent_posts']), 20)
        self.assertEqual(len(result['actions']), 20)

    def test_receipts_skip_the_team_snapshot_only_inside_the_block(self):
        with patch.object(self.controller.store, 'read', wraps=self.controller.store.read) as reads, \
                self.controller.receipts():
            result = self.controller.save({'id': self.aid, 'name': 'Maya B'})
            detail = self.controller.detail(self.aid)
        self.assertEqual(result, {'ok': True})
        self.assertEqual(reads.call_count, 1)  # the per-role detail only
        self.assertEqual(detail['agent']['name'], 'Maya B')
        self.assertIn('agents', self.controller.save({'id': self.aid, 'name': 'Maya C'}))


class DeferredViewApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.client = TestClient(create_app(Path(temporary.name) / 'api.sqlite3'), base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.controller = self.client.app.state.agent_control
        self.addCleanup(self.controller.stop)
        self.headers = {'X-CTN-Token': login(self.client)}
        self.controller.save({'name': 'Maya', 'username': 'compact_api'})
        self.aid, self.pending, self.published = seed(self.controller)

    def test_dashboard_endpoints_serve_compact_team_and_full_details(self):
        team = self.client.get('/api/agents').json()
        self.assertNotIn('memory_view', team['agents'][0])
        published = next(row for row in team['drafts'] if row['id'] == self.published)
        self.assertEqual(published['sources_omitted'], 1)
        draft = self.client.get(f'/api/agents/drafts/{self.published}')
        self.assertEqual(draft.status_code, 200, draft.text)
        self.assertEqual(draft.json()['draft']['sources'][0]['id'], 'post:7')
        self.assertEqual(self.client.get(f'/api/agents/drafts/{uuid.uuid4()}').status_code, 404)
        self.assertEqual(self.client.get('/api/agents/drafts/not-a-draft').status_code, 400)
        memory = self.client.get(f'/api/agents/{self.aid}/memory')
        self.assertEqual(memory.status_code, 200, memory.text)
        self.assertIn('long_term', memory.json()['memory_view'])
        self.assertEqual(self.client.get(f'/api/agents/{uuid.uuid4()}/memory').status_code, 404)
        detail = self.client.get(f'/api/agents/{self.aid}').json()
        self.assertIn('memory_view', detail['agent'])

    def test_writes_return_receipts(self):
        response = self.client.post('/api/agents/discard', json={'draft_id': self.pending}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'ok': True})
        self.assertEqual(self.controller.store.read()['drafts'][self.pending]['state'], 'discarded')
        self.client.cookies.clear()
        self.assertEqual(self.client.get(f'/api/agents/drafts/{self.published}').status_code, 401)
        self.assertEqual(self.client.get(f'/api/agents/{self.aid}/memory').status_code, 401)


if __name__ == '__main__':
    unittest.main()
