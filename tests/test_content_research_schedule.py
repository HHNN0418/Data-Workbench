"""Recurring research stays opt-in and never overrides newer user decisions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from ctn_data import content_research as research
from ctn_data import content_research_schedule as schedule
from ctn_data.content_research_schedule import KEY, LEASE_SECONDS, ResearchAutomation
from ctn_data.db import connect, initialize
from ctn_data.server import create_app
from tests.auth_helpers import login
from tests.test_content_research import add_post, EVIDENCE


class ResearchScheduleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / 'research.db'
        initialize(self.db)
        add_post(self.db)
        self.now = 1792458000.0
        self.provider = {'id': 'fake', 'protocol': 'openai_chat', 'model': 'fake-model',
                         'api_key': 'secret-test-value', 'enabled': True}
        self.provider_for = Mock(return_value=self.provider)
        self.prepare = Mock(side_effect=lambda settings: deepcopy(EVIDENCE))
        self.runner = Mock(side_effect=self.save)
        self.scheduler = self.make_scheduler()

    def make_scheduler(self):
        return ResearchAutomation(self.db, provider_for=self.provider_for, prepare=self.prepare,
                                  run=self.runner, clock=lambda: self.now)

    def save(self, settings, evidence, provider):
        return research.save_report(self.db, report='表达方式值得比较 [post:7]',
            evidence=evidence, provider_id=settings['provider_id'], model=provider['model'],
            scope=settings['params'], now=self.now)

    def enable(self, **extra):
        return self.scheduler.configure({'expected_revision': self.scheduler.snapshot()['settings']['revision'],
            'enabled': True, 'provider_id': 'fake', 'interval_seconds': 900, **extra})

    def stored_state(self):
        with closing(connect(self.db)) as conn:
            return json.loads(conn.execute('SELECT value FROM checkpoints WHERE key=?', (KEY,)).fetchone()[0])

    def update_status(self, **values):
        with closing(connect(self.db)) as conn, conn:
            state = schedule._load(conn)
            state['status'].update(values)
            schedule._save(conn, state)

    def abandon_sent_request(self):
        self.enable()
        settings, run_id, _, _ = self.scheduler._claim()
        input_hash = schedule._fingerprint(settings, self.provider, EVIDENCE)
        self.assertTrue(self.scheduler._mark_request(run_id, input_hash))
        self.update_status(progress={'stage': 'receiving', 'started_at': schedule._stamp(self.now),
            'updated_at': schedule._stamp(self.now + 10), 'elapsed_seconds': 10, 'received_bytes': 444262})
        self.now += LEASE_SECONDS + 1
        return run_id, input_hash

    def test_default_and_restart_are_read_only_until_enabled(self):
        self.scheduler.tick()
        state = self.scheduler.snapshot()
        self.assertFalse(state['settings']['enabled'])
        self.assertIsNone(state['status']['last_run_at'])
        self.prepare.assert_not_called()
        self.runner.assert_not_called()
        self.enable()
        restored = self.make_scheduler()
        self.assertTrue(restored.snapshot()['settings']['enabled'])
        restored.tick()
        self.runner.assert_called_once()
        self.assertEqual(restored.snapshot()['status']['last_result']['outcome'], 'generated')
        self.assertIsNone(research.snapshot(self.db, self.now)['active_brief_id'])

    def test_unchanged_evidence_skips_cost_but_changed_content_or_model_runs(self):
        self.enable()
        self.scheduler.tick()
        self.now += 900
        self.prepare.side_effect = lambda settings: {
            **deepcopy(EVIDENCE), 'summary': {**EVIDENCE['summary'], 'observation_end': str(self.now)}}
        self.scheduler.tick()
        self.assertEqual(self.scheduler.snapshot()['status']['last_result']['outcome'], 'unchanged')
        self.runner.assert_called_once()
        self.now += 900
        evidence = deepcopy(EVIDENCE)
        evidence['sources'][0]['text'] += ' A meaningful addition.'
        self.prepare.side_effect = lambda settings: evidence
        self.scheduler.tick()
        self.assertEqual(self.runner.call_count, 2)
        self.now += 900
        self.provider['model'] = 'changed-model'
        self.scheduler.tick()
        self.assertEqual(self.runner.call_count, 3)

    def test_intervals_no_data_and_failure_have_durable_backoff(self):
        self.enable()
        self.prepare.side_effect = lambda settings: {'sources': []}
        self.scheduler.tick()
        self.assertEqual(self.scheduler.snapshot()['status']['last_result']['outcome'], 'empty')
        self.runner.assert_not_called()
        self.scheduler.tick()
        self.assertEqual(self.prepare.call_count, 1)
        self.now += 900
        self.prepare.side_effect = RuntimeError('secret-test-value')
        self.scheduler.tick()
        state = self.scheduler.snapshot()
        self.assertEqual(state['status']['last_result']['outcome'], 'failed')
        self.assertNotIn('secret-test-value', json.dumps(state))
        self.assertIsNotNone(state['status']['next_run_at'])
        self.scheduler.tick()
        self.assertEqual(self.prepare.call_count, 2)

    def test_expired_report_refreshes_even_when_sources_are_unchanged(self):
        self.enable(auto_activate=True)
        self.scheduler.tick()
        first = self.scheduler.snapshot()['status']['last_result']['brief_id']
        self.now += 900
        self.scheduler.tick()
        self.runner.assert_called_once()
        self.now += 7200
        self.scheduler.tick()
        self.assertEqual(self.runner.call_count, 2)
        result = self.scheduler.snapshot()['status']['last_result']
        self.assertNotEqual(result['brief_id'], first)
        self.assertTrue(result['activated'])

    def test_enabling_auto_activation_refreshes_and_activates_unchanged_evidence(self):
        self.enable()
        self.scheduler.tick()
        self.assertIsNone(research.snapshot(self.db, self.now)['active_brief_id'])
        self.scheduler.configure({'expected_revision': 1, 'auto_activate': True})
        self.scheduler.tick()
        self.assertEqual(self.runner.call_count, 2)
        self.assertTrue(self.scheduler.snapshot()['status']['last_result']['activated'])

    def test_changed_research_statistics_are_part_of_input_fingerprint(self):
        self.enable()
        evidence = deepcopy(EVIDENCE)
        evidence['research_context'] = {'snapshot_intervals': [{'interaction_delta': 3}]}
        self.prepare.side_effect = lambda settings: evidence
        self.scheduler.tick()
        self.now += 900
        evidence['research_context']['snapshot_intervals'][0]['interaction_delta'] = 0
        self.scheduler.tick()
        self.assertEqual(self.runner.call_count, 2)

    def test_concurrent_processes_make_one_model_request(self):
        self.enable()
        entered, release = threading.Event(), threading.Event()
        def slow(settings, evidence, provider):
            entered.set()
            self.assertTrue(release.wait(5))
            return self.save(settings, evidence, provider)
        self.runner.side_effect = slow
        other = self.make_scheduler()
        with ThreadPoolExecutor(max_workers=2) as pool:
            task = pool.submit(self.scheduler.tick)
            self.assertTrue(entered.wait(5))
            other.tick()
            self.assertTrue(other.snapshot()['status']['running'])
            release.set()
            task.result(5)
        self.runner.assert_called_once()

    def test_abandoned_lease_recovers_after_expiry(self):
        self.enable()
        claim = self.scheduler._claim()
        self.assertIsNotNone(claim)
        other = self.make_scheduler()
        other.tick()
        self.runner.assert_not_called()
        # Claim schedules the next interval before work, avoiding a restart
        # immediately repeating a request whose result may be uncertain.
        self.now += max(LEASE_SECONDS, 900)
        other.tick()
        self.runner.assert_not_called()
        self.assertEqual(other.snapshot()['status']['last_result']['outcome'], 'interrupted')
        # Nothing was sent by the abandoned preparation, so a later due check
        # may run it normally after the interruption has been recorded.
        other.tick()
        self.runner.assert_called_once()
        self.assertFalse(other.snapshot()['status']['running'])

    def test_stale_snapshot_is_read_only_and_freezes_progress_until_owner_is_confirmed_gone(self):
        run_id, _ = self.abandon_sent_request()
        before = self.stored_state()
        status = self.make_scheduler().snapshot()['status']
        self.assertFalse(status['running'])
        self.assertFalse(status['lease_healthy'])
        self.assertTrue(status['stale'])
        self.assertFalse(status['interrupted'])
        self.assertEqual(status['run_id'], run_id)
        self.assertEqual(status['progress']['elapsed_seconds'], 10)
        self.now += 300
        self.assertEqual(self.scheduler.snapshot()['status']['progress']['elapsed_seconds'], 10)
        self.assertEqual(self.stored_state(), before)
        self.prepare.assert_not_called()
        self.runner.assert_not_called()

    def test_expired_owner_recovers_before_due_without_repeating_paid_request(self):
        run_id, input_hash = self.abandon_sent_request()
        success = schedule._stamp(self.now - 1000)
        self.update_status(last_success_at=success)
        before_due = self.scheduler.snapshot()['status']['next_run_at']
        other = self.make_scheduler()
        other.tick()
        status = other.snapshot()['status']
        self.assertTrue(status['interrupted'])
        self.assertFalse(status['stale'])
        self.assertFalse(status['running'])
        self.assertIsNone(status['run_id'])
        self.assertEqual(status['interrupted_run_id'], run_id)
        self.assertEqual(status['last_success_at'], success)
        self.assertEqual(status['next_run_at'], before_due)
        self.assertEqual(status['progress']['elapsed_seconds'], 10)
        self.assertEqual(status['progress']['received_bytes'], 444262)
        self.assertEqual(self.stored_state()['status']['blocked_input_hash'], input_hash)
        other.tick()
        self.prepare.assert_not_called()
        self.now += 900
        other.tick()
        self.runner.assert_not_called()
        self.assertEqual(other.snapshot()['status']['last_result']['outcome'], 'interrupted')
        self.assertEqual(other.snapshot()['status']['interrupted_run_id'], run_id)

    def test_disabled_automation_recovers_abandoned_owner_without_calling_ai(self):
        run_id, _ = self.abandon_sent_request()
        self.scheduler.configure({'expected_revision': 1, 'enabled': False})
        self.make_scheduler().tick()
        status = self.scheduler.snapshot()['status']
        self.assertEqual(status['interrupted_run_id'], run_id)
        self.assertIsNone(status['next_run_at'])
        self.prepare.assert_not_called()
        self.runner.assert_not_called()

    def test_maintenance_allows_recovery_but_prevents_new_claims(self):
        run_id, _ = self.abandon_sent_request()
        self.update_status(maintenance_id='maintenance-test')
        self.make_scheduler().tick()
        self.assertEqual(self.scheduler.snapshot()['status']['interrupted_run_id'], run_id)
        self.now += 900
        self.make_scheduler().tick()
        self.assertIsNone(self.scheduler._claim())
        self.prepare.assert_not_called()
        self.runner.assert_not_called()

    def test_live_owner_lock_blocks_recovery_and_retry_even_after_lease_expiry(self):
        run_id, _ = self.abandon_sent_request()
        other = self.make_scheduler()
        with schedule.owner_lock(self.db):
            other.tick()
            status = other.snapshot()['status']
            self.assertEqual(status['run_id'], run_id)
            self.assertTrue(status['stale'])
            self.assertFalse(status['interrupted'])
            with self.assertRaises(research.ResearchError) as raised:
                other.retry_interrupted(run_id)
            self.assertEqual(raised.exception.status_code, 409)
        self.prepare.assert_not_called()
        self.runner.assert_not_called()
        other.tick()
        self.assertTrue(other.snapshot()['status']['interrupted'])

    def test_explicit_retry_is_compare_and_swap_and_only_bypasses_block_once(self):
        run_id, input_hash = self.abandon_sent_request()
        self.make_scheduler().tick()
        pending = self.scheduler.retry_interrupted(run_id)
        self.assertEqual(pending['status']['last_result'], {'outcome': 'retry_pending', 'run_id': run_id})
        self.assertEqual(pending['status']['next_run_at'], schedule._stamp(self.now))
        self.assertTrue(self.scheduler._wake.is_set())
        self.runner.assert_not_called()
        with self.assertRaises(research.ResearchError) as raised:
            self.make_scheduler().retry_interrupted(run_id)
        self.assertEqual(raised.exception.status_code, 409)
        self.runner.side_effect = RuntimeError('synthetic request failure')
        self.make_scheduler().tick()
        self.runner.assert_called_once()
        stored = self.stored_state()['status']
        self.assertNotIn('retry_run_id', stored)
        self.assertNotIn('retry_input_hash', stored)
        self.assertEqual(stored['blocked_input_hash'], input_hash)
        self.now += 900
        self.make_scheduler().tick()
        self.runner.assert_called_once()

    def test_retry_rejects_wrong_round_disabled_and_maintenance(self):
        run_id, _ = self.abandon_sent_request()
        self.make_scheduler().tick()
        with self.assertRaises(research.ResearchError) as raised:
            self.scheduler.retry_interrupted('another-round')
        self.assertEqual(raised.exception.status_code, 409)
        self.scheduler.configure({'expected_revision': 1, 'enabled': False})
        with self.assertRaises(research.ResearchError) as raised:
            self.scheduler.retry_interrupted(run_id)
        self.assertEqual(raised.exception.status_code, 409)
        self.enable()
        self.update_status(maintenance_id='maintenance-test')
        with self.assertRaises(research.ResearchError) as raised:
            self.scheduler.retry_interrupted(run_id)
        self.assertEqual(raised.exception.status_code, 409)
        self.prepare.assert_not_called()
        self.runner.assert_not_called()

    def test_concurrent_retries_schedule_only_one_attempt(self):
        run_id, _ = self.abandon_sent_request()
        self.make_scheduler().tick()
        def retry():
            try:
                return self.make_scheduler().retry_interrupted(run_id)['status']['last_result']['outcome']
            except research.ResearchError as exc:
                return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: retry(), range(2)))
        self.assertCountEqual(results, ['retry_pending', 409])
        self.make_scheduler().tick()
        self.runner.assert_called_once()

    def test_retry_before_original_request_was_sent_still_refreshes_a_usable_report(self):
        self.enable()
        self.scheduler.tick()
        self.now += 900
        _, run_id, _, _ = self.scheduler._claim()
        self.now += LEASE_SECONDS + 1
        self.make_scheduler().tick()
        self.scheduler.retry_interrupted(run_id)
        self.make_scheduler().tick()
        self.assertEqual(self.runner.call_count, 2)
        self.assertNotIn('blocked_input_hash', self.stored_state()['status'])
        self.now += 7200
        self.make_scheduler().tick()
        self.assertEqual(self.runner.call_count, 3)

    def test_retry_rejects_live_round_and_wrong_stale_round_without_changing_state(self):
        self.enable()
        _, run_id, _, _ = self.scheduler._claim()
        before = self.stored_state()
        with self.assertRaises(research.ResearchError) as raised:
            self.make_scheduler().retry_interrupted(run_id)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.stored_state(), before)
        self.now += LEASE_SECONDS + 1
        with self.assertRaises(research.ResearchError) as raised:
            self.make_scheduler().retry_interrupted('another-round')
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.stored_state(), before)
        pending = self.make_scheduler().retry_interrupted(run_id)
        self.assertEqual(pending['status']['last_result']['outcome'], 'retry_pending')
        self.runner.assert_not_called()

    def test_maintenance_arriving_after_claim_allows_the_existing_round_to_finish(self):
        self.enable()
        def prepare(settings):
            self.update_status(maintenance_id='maintenance-test')
            return deepcopy(EVIDENCE)
        self.prepare.side_effect = prepare
        self.scheduler.tick()
        self.runner.assert_called_once()
        self.assertEqual(self.scheduler.snapshot()['status']['last_result']['outcome'], 'generated')
        self.now += 900
        self.make_scheduler().tick()
        self.runner.assert_called_once()

    def test_settings_compare_and_swap_and_disable_preserve_inflight_lease(self):
        self.enable()
        with self.assertRaises(research.ResearchError) as raised:
            self.scheduler.configure({'expected_revision': 0, 'enabled': False})
        self.assertEqual(raised.exception.status_code, 409)
        self.scheduler._claim()
        disabled = self.scheduler.configure({'expected_revision': 1, 'enabled': False})
        self.assertTrue(disabled['status']['running'])
        self.assertIsNone(disabled['status']['next_run_at'])
        self.make_scheduler().tick()
        self.runner.assert_not_called()

    def test_disabled_or_deleted_provider_does_not_block_pause(self):
        self.enable()
        self.provider_for.side_effect = research.ResearchError('provider missing')
        result = self.scheduler.configure({'expected_revision': 1, 'enabled': False})
        self.assertFalse(result['settings']['enabled'])
        self.assertIsNone(result['status']['next_run_at'])

    def test_valid_report_auto_activates_with_existing_validations(self):
        self.enable(auto_activate=True)
        self.scheduler.tick()
        result = self.scheduler.snapshot()['status']['last_result']
        self.assertTrue(result['activated'])
        self.assertEqual(research.snapshot(self.db, self.now)['active_brief_id'], result['brief_id'])

    def test_bad_citation_is_saved_but_never_activated(self):
        self.enable(auto_activate=True)
        self.runner.side_effect = lambda settings, evidence, provider: research.save_report(
            self.db, report='unknown [post:999]', evidence=evidence, provider_id='fake',
            model='fake-model', scope={}, now=self.now)
        self.scheduler.tick()
        result = self.scheduler.snapshot()['status']['last_result']
        self.assertEqual(result['outcome'], 'generated')
        self.assertFalse(result['activated'])
        self.assertIsNotNone(result['activation_note'])
        self.assertIsNone(research.snapshot(self.db, self.now)['active_brief_id'])

    def test_manual_selection_during_request_wins_over_automatic_activation(self):
        self.enable(auto_activate=True)
        initial = self.save({'provider_id': 'fake', 'params': {}}, EVIDENCE, self.provider)
        initial_id = initial['created_brief_id']
        def manual_change(settings, evidence, provider):
            research.activate(self.db, initial_id, initial['revision'], self.now)
            return self.save(settings, evidence, provider)
        self.runner.side_effect = manual_change
        self.scheduler.tick()
        result = self.scheduler.snapshot()['status']['last_result']
        self.assertFalse(result['activated'])
        self.assertEqual(research.snapshot(self.db, self.now)['active_brief_id'], initial_id)

    def test_settings_change_during_request_prevents_activation_and_reschedules(self):
        self.enable(auto_activate=True)
        def disable(settings, evidence, provider):
            self.scheduler.configure({'expected_revision': 1, 'enabled': False})
            return self.save(settings, evidence, provider)
        self.runner.side_effect = disable
        self.scheduler.tick()
        status = self.scheduler.snapshot()['status']
        self.assertFalse(status['last_result']['activated'])
        self.assertIsNone(status['next_run_at'])
        self.assertIsNone(research.snapshot(self.db, self.now)['active_brief_id'])

    def test_pause_during_preparation_does_not_start_model(self):
        self.enable()
        def pause(settings):
            self.scheduler.configure({'expected_revision': 1, 'enabled': False})
            return EVIDENCE
        self.prepare.side_effect = pause
        self.scheduler.tick()
        self.runner.assert_not_called()
        self.assertEqual(self.scheduler.snapshot()['status']['last_result']['outcome'], 'cancelled')

    def test_corrupt_settings_fail_closed_without_replacing_state(self):
        with closing(connect(self.db)) as conn, conn:
            conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)',
                         (KEY, '{broken', '2026-10-20T01:00:00Z'))
        with self.assertRaises(research.ResearchError):
            self.scheduler.tick()
        with closing(connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT value FROM checkpoints WHERE key=?', (KEY,)).fetchone()[0],
                             '{broken')
        self.runner.assert_not_called()


class ResearchAutomationEndpointTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / 'research.db'
        self.app = create_app(self.db)
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.token = login(self.client)
        add_post(self.db)
        self.provider = patch.object(self.app.state.ai_providers, 'get', return_value={
            'id': 'fake', 'protocol': 'openai_chat', 'model': 'fake-model', 'enabled': True}).start()
        self.complete = patch('ctn_data.ai_providers.complete', return_value={
            'text': '参考 [post:7]', 'model': 'fake-model', 'usage': {}}).start()
        self.evidence = patch('ctn_data.ai_evidence.build_evidence', return_value=EVIDENCE).start()
        self.addCleanup(patch.stopall)

    def post(self, payload):
        return self.client.post('/api/ai/research/automation', json=payload,
                                headers={'X-CTN-Token': self.token})

    def test_get_save_and_enable_do_not_call_ai_until_scheduler_runs(self):
        default = self.client.get('/api/ai/research/automation')
        self.assertEqual(default.status_code, 200)
        self.assertFalse(default.json()['settings']['enabled'])
        saved = self.post({'expected_revision': 0, 'enabled': True, 'provider_id': 'fake',
                           'params': {'own': 'all'}, 'max_output_tokens': 512, 'auto_activate': True,
                           'mode': 'popular'})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.complete.assert_not_called()
        self.app.state.research_automation.tick()
        self.complete.assert_called_once()
        self.assertIsNone(self.complete.call_args.kwargs['max_output_tokens'])
        self.assertNotIn('embedding_backend', self.evidence.call_args.kwargs)
        result = self.client.get('/api/ai/research/automation').json()
        self.assertTrue(result['status']['last_result']['activated'])

    def test_validation_and_auth_reject_before_model_calls(self):
        for change in ({'interval_seconds': 899}, {'sample_size': True}, {'auto_activate': 'yes'},
                       {'enabled': None}, {'max_output_tokens': True}, {'params': {'since': 'invalid'}}):
            response = self.post({'expected_revision': 0, **change})
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.post({'expected_revision': 0, 'enabled': True}).status_code, 400)
        self.assertEqual(self.post({'expected_revision': 0, 'max_output_tokens': None}).status_code, 200)
        self.assertEqual(self.post({'expected_revision': 0, 'enabled': False}).status_code, 409)
        self.assertEqual(self.client.post('/api/ai/research/automation', json={
            'expected_revision': 1, 'enabled': True, 'provider_id': 'fake'}).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/ai/research/automation').status_code, 401)
        self.complete.assert_not_called()

    def test_frontend_workspace_routes_are_served(self):
        for path in ('/ai', '/ai/analysis', '/ai/reading', '/ai/agents', '/ai/settings',
                     '/ai/agents/12345678-1234-1234-1234-123456789012'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn('text/html', response.headers['content-type'])


if __name__ == '__main__':
    unittest.main()
