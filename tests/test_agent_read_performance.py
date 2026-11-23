"""Role inspection avoids unrelated views and repeated business-state reads."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ctn_data import agent_budget, agent_feedback, agent_memory, agent_memory_archive, agent_relationships
from ctn_data.agent_control import AgentController, AgentError, PREFIX
from ctn_data.db import connect


class AgentReadPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / 'roles.sqlite3'
        self.controller = AgentController(self.path, self.directory / 'secrets', clock=lambda: 1792548000)
        self.controller.save({'name': 'Maya', 'username': 'read_maya'})
        self.controller.save({'name': 'Ethan', 'username': 'read_ethan'})
        self.roles = list(self.controller.store.read()['agents'])

    def test_detail_reads_business_state_once_and_only_builds_selected_role(self):
        controller = self.controller
        expected = controller.snapshot()
        aid = self.roles[0]
        with patch.object(controller.store, 'read', wraps=controller.store.read) as reads, \
                patch.object(agent_memory, 'public_view', wraps=agent_memory.public_view) as memory_view, \
                patch.object(agent_relationships, 'snapshot_for', wraps=agent_relationships.snapshot_for) as relationships, \
                patch.object(agent_feedback, 'snapshot_for', wraps=agent_feedback.snapshot_for) as feedback, \
                patch.object(agent_memory_archive, 'stats', wraps=agent_memory_archive.stats) as archive:
            actual = controller.detail(aid)
        self.assertEqual(reads.call_count, 1)
        for function in (memory_view, relationships, feedback, archive):
            self.assertEqual(function.call_count, 1)
        self.assertEqual(memory_view.call_args.args[1]['id'], aid)
        self.assertEqual(feedback.call_args.args[2]['id'], aid)
        self.assertEqual(archive.call_args.args[2], aid)
        self.assertEqual(actual['agent'], next(row for row in expected['agents'] if row['id'] == aid))
        self.assertEqual(actual['drafts'], [row for row in expected['drafts'] if row['agent_id'] == aid])
        for key in ('settings', 'budgets', 'interaction_policy', 'maintenance'):
            self.assertEqual(actual[key], expected[key])

    def test_logs_validate_role_without_parsing_drafts_or_working_memory(self):
        controller = self.controller
        aid = self.roles[0]
        controller.store.change(lambda data, conn: controller._event(data, 'reading', 'A reading event', aid))
        conn = connect(self.path)
        try:
            with conn:
                for key in ('memory', 'drafts'):
                    conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                 (PREFIX + key, '{invalid unrelated business state'))
        finally:
            conn.close()
        with patch.object(controller.store, 'read', side_effect=AssertionError('full business read in log query')):
            result = controller.logs(aid)
            self.assertEqual(result['total'], 2)  # save event and the seeded reading
            self.assertTrue(any(row['message'] == 'A reading event' for row in result['items']))
            with self.assertRaises(AgentError) as missing:
                controller.logs('00000000-0000-0000-0000-000000000000')
            self.assertEqual(missing.exception.status_code, 404)

    def test_budget_presentation_reuses_supplied_snapshot(self):
        data = self.controller.store.read()
        expected = agent_budget.public_status(self.controller)
        with patch.object(self.controller.store, 'read', side_effect=AssertionError('duplicate read')):
            self.assertEqual(agent_budget.public_status(self.controller, data=data), expected)

    def test_invalid_detail_preserves_error_before_building_views(self):
        with patch.object(agent_memory, 'public_view', side_effect=AssertionError('view of invalid role')):
            for identifier, status in [('invalid', 400), ('00000000-0000-0000-0000-000000000000', 404)]:
                with self.subTest(identifier=identifier), self.assertRaises(AgentError) as error:
                    self.controller.detail(identifier)
                self.assertEqual(error.exception.status_code, status)

    def seed_raw(self, key, value):
        conn = connect(self.path)
        try:
            with conn:
                conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                             (PREFIX + key, value))
        finally:
            conn.close()

    def test_budget_gate_and_cooldown_do_not_parse_fifteen_megabytes_of_drafts(self):
        controller = self.controller
        drafts = json.dumps({'large': {'body': 'x' * 15_000_000}})
        self.seed_raw('drafts', drafts)
        self.seed_raw('memory', '{invalid unrelated working memory')
        loads = json.loads
        parsed_bytes = []

        def small_loads(value, *args, **kwargs):
            parsed_bytes.append(len(value))
            self.assertLess(len(value), 10_000, 'quota/gate operation parsed business data')
            return loads(value, *args, **kwargs)

        with patch('ctn_data.agent_control.json.loads', side_effect=small_loads), \
                patch.object(controller.store, '_load', side_effect=AssertionError('full business load')):
            controller._cooldown(60)
            self.assertEqual(controller.store.read_budget()['rate']['not_before'], controller.clock() + 60)
            controller.store.change_budget(lambda data, conn: data['rate'].update(maintenance={'active': True}))
            with patch.object(controller, '_competition_tick', side_effect=AssertionError('maintenance gate ignored')):
                controller.tick()
        self.assertTrue(parsed_bytes)
        conn = connect(self.path)
        try:
            self.assertEqual(conn.execute('SELECT value FROM meta WHERE key=?', (PREFIX + 'drafts',)).fetchone()[0], drafts)
        finally:
            conn.close()

    def test_maintenance_gate_reads_fresh_rate_and_ignores_broken_drafts(self):
        controller = self.controller
        self.seed_raw('drafts', '{invalid unrelated drafts')
        controller.store.change_budget(lambda data, conn: data['rate'].update(maintenance={'active': True}))
        controller.tick()
        controller.store.change_budget(lambda data, conn: data['rate'].update(maintenance={'active': False}))
        with patch.object(controller, '_competition_tick', side_effect=RuntimeError('gate is now open')):
            with self.assertRaisesRegex(RuntimeError, 'gate is now open'):
                controller.tick()

    def test_role_release_skips_business_json_and_preserves_replacement_owner(self):
        controller = self.controller
        aid = self.roles[0]
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                self.seed_raw('drafts', '{}')
                with controller._claim(aid) as token:
                    self.seed_raw('drafts', '{invalid unrelated draft material')
                    if replacement:
                        controller.store.change_runtime(lambda data, conn: data['leases']['agent:' + aid].update(
                            token='replacement-token', owner='replacement-owner'))
                lease = controller.store.read_runtime()['leases'].get('agent:' + aid)
                if replacement:
                    self.assertEqual(lease['token'], 'replacement-token')
                    self.assertEqual(lease['owner'], 'replacement-owner')
                else:
                    self.assertIsNone(lease)


if __name__ == '__main__':
    unittest.main()
