"""Confirmed publications drop generation inputs; unresolved work keeps them."""
from pathlib import Path
import tempfile
import unittest

from ctn_data import agent_intents, agent_memory
from ctn_data.agent_control import ACTION_TYPES, AgentStore, _compact_published


AT = '2026-10-20T02:00:00Z'


def source(number, **values):
    return {'id': f'post:{number}', 'post_id': number, 'author_id': 900 + number,
            'root_post_id': number, 'text': 'x' * 200, 'version': f'v{number}', **values}


def draft(draft_id, state, **values):
    return {'id': draft_id, 'agent_id': 'maya', 'account_id': 5, 'action': 'reply',
            'state': state, 'created_at': AT, 'published_at': AT, 'body': 'Reply text.',
            'target_post_id': 3, 'source_ids': ['post:1'],
            'sources': [source(number) for number in range(1, 8)],
            'relationship_context': {'relations': [{'target_account_id': 77}]},
            'tag_context': {'tags': ['#a']}, 'competition_feedback': {'note': 'y' * 100},
            'competition_model': {'id': 'm'}, **values}


class DraftCompactionTests(unittest.TestCase):
    def test_keeps_cited_and_target_rows_and_is_idempotent(self):
        row = draft('d1', 'published')
        _compact_published(row)
        self.assertEqual([s['id'] for s in row['sources']], ['post:1', 'post:3'])
        self.assertEqual(row['sources_compacted'], {'kept': 2, 'total': 7})
        for key in ('relationship_context', 'tag_context', 'competition_feedback'):
            self.assertNotIn(key, row)
        self.assertEqual(row['competition_model'], {'id': 'm'})
        self.assertEqual(agent_memory._target_author(row), 903)
        before = dict(row)
        _compact_published(row)
        self.assertEqual(row, before)

    def test_follow_keeps_target_account_rows(self):
        row = draft('d1', 'published', action='follow', target_post_id=None,
                    target_account_id=905, source_ids=[])
        _compact_published(row)
        self.assertEqual([s['id'] for s in row['sources']], ['post:5'])
        self.assertEqual(agent_memory._target_author(row), 905)

    def test_store_compacts_only_resolved_publications(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / 'agents.sqlite3')
            rows = {'published': draft('published', 'published'),
                    'held': draft('held', 'published', notification_ids=[11]),
                    'discarded': draft('discarded', 'discarded'),
                    'pending': draft('pending', 'pending')}
            signatures = {key: agent_intents.signature(value) for key, value in rows.items()}

            def seed(data, conn):
                data['agents']['maya'] = {'id': 'maya', 'version': 1, 'account_id': 5,
                                          'allowed_actions': list(ACTION_TYPES)}
                data['drafts'].update(rows)
            store.change(seed)
            saved = store.read()['drafts']
        self.assertEqual(len(saved['published']['sources']), 2)
        self.assertNotIn('relationship_context', saved['published'])
        for key in ('held', 'discarded', 'pending'):
            self.assertEqual(len(saved[key]['sources']), 7, key)
            self.assertIn('relationship_context', saved[key])
            self.assertNotIn('sources_compacted', saved[key])
            self.assertEqual(agent_intents.signature(saved[key]), signatures[key], key)


if __name__ == '__main__':
    unittest.main()
