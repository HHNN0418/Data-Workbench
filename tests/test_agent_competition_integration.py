"""Offline boundaries of competition scheduling, delivery, models and collection."""
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from ctn_data import ai_providers, collector, task_queue
from ctn_data.agent_control import AgentController, AgentError
from ctn_data.ai_routes import ProviderRequest
from ctn_data.db import connect, initialize
from ctn_data.ingest import save_counters, save_posts
from ctn_data.server import create_app
from tests import test_agent_actions as action_fixture
from tests.auth_helpers import login


class CompetitionProviderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'providers.sqlite3'
        self.store = ai_providers.ProviderStore(self.path, self.root / 'keys')

    def save(self, **changes):
        payload = {'name': 'Local test', 'base_url': 'http://localhost:8000/v1',
                   'model': 'test-model'}
        payload.update(changes)
        return self.store.save(payload)['providers'][-1]

    def test_legacy_provider_defaults_to_standard_without_rewriting_storage(self):
        provider = self.save()
        conn = connect(self.path)
        self.addCleanup(conn.close)
        value = json.loads(conn.execute('SELECT value FROM meta WHERE key=?',
                                       (ai_providers.SETTINGS_KEY,)).fetchone()[0])
        value['providers'][0].pop('competition_tier', None)
        legacy = json.dumps(value)
        with conn:
            conn.execute('UPDATE meta SET value=? WHERE key=?', (legacy, ai_providers.SETTINGS_KEY))
        reopened = ai_providers.ProviderStore(self.path, self.root / 'keys')
        self.assertEqual(reopened.list_public()['providers'][0]['competition_tier'], 'standard')
        self.assertEqual(reopened.get(provider['id'])['competition_tier'], 'standard')
        self.assertEqual(conn.execute('SELECT value FROM meta WHERE key=?',
                                     (ai_providers.SETTINGS_KEY,)).fetchone()[0], legacy)

    def test_explicit_grade_persists_and_partial_update_keeps_it(self):
        for grade in ('enhanced', 'premium'):
            with self.subTest(grade=grade):
                row = self.save(competition_tier=grade, max_output_tokens=4096)
                self.store.save({'id': row['id'], 'name': 'Renamed connection'})
                reopened = ai_providers.ProviderStore(self.path, self.root / 'keys')
                result = reopened.get(row['id'])
                self.assertEqual(result['competition_tier'], grade)
                self.assertIsNone(result['max_output_tokens'])

    def test_invalid_grade_rejected_by_store_and_request_without_partial_save(self):
        self.save()
        before = self.store.list_public()
        for grade in ('automatic', '', True, 1, None):
            with self.subTest(grade=grade):
                with self.assertRaises((ValueError, ai_providers.ProviderError)):
                    self.save(competition_tier=grade)
                with self.assertRaises(ValidationError):
                    ProviderRequest(name='Test', base_url='http://localhost:8000/v1',
                                    model='test', competition_tier=grade)
        self.assertEqual(self.store.list_public(), before)


class CompetitionAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.app = create_app(root / 'api.sqlite3', backup_dir=root / 'backups')
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.controller = self.app.state.agent_control
        self.model = patch('ctn_data.ai_providers.complete',
                           side_effect=AssertionError('No live model calls')).start()
        self.addCleanup(patch.stopall)
        self.url = '/api/agents/competition'

    def authorize(self):
        return {'X-CTN-Token': login(self.client)}

    def test_read_requires_login_and_writes_require_csrf(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)
        headers = self.authorize()
        current = self.client.get(self.url)
        self.assertEqual(current.status_code, 200, current.text)
        before = current.json()
        response = self.client.post(self.url, json={'expected_revision': before['revision'], 'mode': 'observe'})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.client.get(self.url).json()['revision'], before['revision'])
        self.assertEqual(self.client.post(self.url, headers=headers,
            json={'expected_revision': before['revision'], 'mode': 'observe'}).status_code, 200)

    def test_default_off_get_is_read_only_and_never_calls_model_or_maintenance(self):
        self.authorize()
        before = self.controller.store.read()
        with patch('ctn_data.agent_competition.refresh') as refresh, \
                patch('ctn_data.agent_competition.sync') as sync:
            first = self.client.get(self.url)
            second = self.client.get(self.url)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()['settings']['mode'], 'off')
        self.assertEqual(second.json()['revision'], first.json()['revision'])
        self.assertEqual(self.controller.store.read(), before)
        refresh.assert_not_called()
        sync.assert_not_called()
        self.model.assert_not_called()

    def test_stale_revision_conflicts_and_mode_survives_controller_restart(self):
        headers = self.authorize()
        initial = self.client.get(self.url).json()
        accepted = self.client.post(self.url, headers=headers,
            json={'expected_revision': initial['revision'], 'mode': 'observe'})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        revision = accepted.json()['revision']
        self.assertGreater(revision, initial['revision'])
        rejected = self.client.post(self.url, headers=headers,
            json={'expected_revision': initial['revision'], 'mode': 'resources'})
        self.assertEqual(rejected.status_code, 409, rejected.text)
        current = self.client.get(self.url).json()
        self.assertEqual(current['settings']['mode'], 'observe')
        self.assertEqual(current['revision'], revision)
        reopened = AgentController(self.controller.db_path, Path(self.controller.db_path).parent / 'other-keys')
        self.assertEqual(reopened.competition()['revision'], revision)
        self.assertEqual(reopened.competition()['settings']['mode'], 'observe')

    def test_invalid_contract_does_not_modify_settings(self):
        headers = self.authorize()
        original = self.client.get(self.url).json()
        for payload in ({'mode': 'observe'},
                        {'expected_revision': True, 'mode': 'observe'},
                        {'expected_revision': original['revision'], 'mode': 'automatic'},
                        {'expected_revision': original['revision'], 'enhanced_hourly_calls': -1},
                        {'expected_revision': original['revision'], 'unexpected': 1}):
            with self.subTest(payload=payload):
                result = self.client.post(self.url, headers=headers, json=payload)
                self.assertEqual(result.status_code, 400, result.text)
        self.assertEqual(self.client.get(self.url).json()['revision'], original['revision'])


class CompetitionActionTests(unittest.TestCase):
    """Reuse the existing fake social fixture, without inheriting its test cases."""
    def setUp(self):
        action_fixture.AutonomousActions.setUp(self)
        self.provider['id'] = 'fake'

    def decide(self, *args, **kwargs):
        return action_fixture.AutonomousActions.decide(self, *args, **kwargs)

    def enable(self):
        self.controller.save({'id': self.agent_id, 'name': 'Fictional local',
                              'mode': 'auto', 'enabled': True})
        self.controller.settings({'enabled': True})

    def output(self):
        self.model.return_value = {'text': json.dumps({'action': 'post',
            'body': 'A considered local view.', 'source_ids': ['post:7'],
            'notification_ids': [], 'reason': 'Relevant topic.'}), 'model': 'actual-test-model', 'usage': {}}

    def test_off_mode_keeps_autonomous_generation_and_publication_working(self):
        self.enable()
        self.output()
        self.assertEqual(self.controller.competition()['settings']['mode'], 'off')
        self.controller._run(self.agent_id, automatic=True)
        draft = self.controller.snapshot()['drafts'][0]
        self.assertEqual(draft['state'], 'published')
        self.assertEqual(len(self.social.writes), 1)
        self.assertEqual(self.model.call_args.args[1]['id'], 'fake')
        self.assertEqual(draft['actual_provider_id'], 'fake')
        from ctn_data.agent_competition import PREFIX
        conn = connect(self.controller.db_path)
        try:
            receipt = conn.execute('SELECT value FROM checkpoints WHERE key=?',
                                   (PREFIX + str(draft['post_id']),)).fetchone()
            self.assertIsNotNone(receipt)
            attribution = json.loads(receipt[0])
            self.assertEqual(attribution['agent_id'], self.agent_id)
            self.assertEqual(attribution['provider_id'], 'fake')
            self.assertEqual(attribution['draft_id'], draft['id'])
        finally:
            conn.close()

    def test_generation_denial_prevents_model_call_and_draft_creation(self):
        self.enable()
        self.output()
        with patch('ctn_data.agent_competition.admit',
                   return_value={'allowed': False, 'retry_after': 9}) as gate, \
                patch('ctn_data.agent_competition.select_model') as selection:
            with self.assertRaises(AgentError) as denied:
                self.controller._run(self.agent_id, automatic=True)
        self.assertEqual(denied.exception.status_code, 429)
        self.assertEqual(gate.call_args.args[3], 'generate')
        selection.assert_not_called()
        self.model.assert_not_called()
        self.assertEqual(self.controller.snapshot()['drafts'], [])
        self.assertEqual(self.social.writes, [])

    def test_manual_generation_and_review_publication_do_not_take_competition_slots(self):
        with patch('ctn_data.agent_competition.admit',
                   side_effect=AssertionError('Manual work must not compete')), \
                patch('ctn_data.agent_competition.select_model',
                      side_effect=AssertionError('Manual work uses its selected provider')):
            draft = self.decide('post')
            result = self.controller.publish(draft['id'])['drafts'][0]
        self.assertEqual(result['state'], 'published')
        self.assertFalse(result['competition_automatic'])
        self.assertNotIn('competition_allocation', result)
        self.assertEqual(len(self.social.writes), 1)

    def test_automatic_publish_checks_gate_before_sdk_and_again_at_reservation(self):
        draft = self.decide('post')
        self.enable()
        with patch('ctn_data.agent_competition.admit',
                   return_value={'allowed': False, 'retry_after': 7}), \
                patch.object(self.controller, '_sdk') as sdk:
            with self.controller._claim(self.agent_id) as token, self.assertRaises(AgentError) as denied:
                self.controller._publish(draft['id'], None, token, automatic=True)
        self.assertEqual(denied.exception.status_code, 429)
        sdk.assert_not_called()
        with patch('ctn_data.agent_competition.admit', side_effect=[
                {'allowed': True}, {'allowed': False, 'retry_after': 7}]) as gate:
            with self.controller._claim(self.agent_id) as token, self.assertRaises(AgentError):
                self.controller._publish(draft['id'], None, token, automatic=True)
        self.assertEqual(gate.call_count, 2)
        self.assertEqual(gate.call_args_list[0].args[2:5], gate.call_args_list[1].args[2:5])
        current = self.controller.snapshot()['drafts'][0]
        self.assertEqual(current['state'], 'pending')
        self.assertEqual(self.social.writes, [])

    def test_selected_reward_provider_and_output_cap_are_frozen_on_draft(self):
        self.enable()
        self.output()
        reward = {**self.provider, 'id': 'reward', 'model': 'reward-config', 'max_output_tokens': 4096}
        self.controller.providers.get = lambda key: reward if key == 'reward' else self.provider
        selection = {'provider_id': 'reward', 'enhanced': True, 'max_output_tokens': 1024,
                     'round_id': 'test-round', 'tier': 'enhanced'}
        with patch('ctn_data.agent_competition.admit', return_value={'allowed': True}), \
                patch('ctn_data.agent_competition.select_model', return_value=selection.copy()), \
                patch.object(self.controller, '_publish'):
            self.controller._run(self.agent_id, automatic=True)
        draft = self.controller.snapshot()['drafts'][0]
        self.assertEqual(self.model.call_args.args[1]['id'], 'reward')
        self.assertNotIn('max_output_tokens', self.model.call_args.kwargs)
        self.assertEqual(draft['actual_provider_id'], 'reward')
        self.assertEqual(draft['model'], 'actual-test-model')
        self.assertEqual(draft['competition_model'], selection)
        self.assertEqual(self.controller.store.read()['agents'][self.agent_id]['provider_id'], 'fake')
        reward['model'] = 'later-config'
        self.assertEqual(self.controller.snapshot()['drafts'][0]['model'], 'actual-test-model')

    def test_unavailable_reward_falls_back_and_records_actual_standard_provider(self):
        self.enable()
        self.output()
        def get(key):
            if key == 'unavailable':
                raise ai_providers.ProviderError('Unavailable reward', 404)
            return self.provider
        self.controller.providers.get = get
        with patch('ctn_data.agent_competition.admit', return_value={'allowed': True}), \
                patch('ctn_data.agent_competition.select_model', return_value={
                    'provider_id': 'unavailable', 'enhanced': True, 'max_output_tokens': 256}), \
                patch.object(self.controller, '_publish'):
            self.controller._run(self.agent_id, automatic=True)
        draft = self.controller.snapshot()['drafts'][0]
        self.assertEqual(self.model.call_args.args[1]['id'], 'fake')
        self.assertEqual(draft['actual_provider_id'], 'fake')
        self.assertFalse(draft['competition_model']['enhanced'])
        self.assertTrue(draft['competition_model']['fallback_reason'])

    def test_competition_receipt_failure_cannot_undo_confirmed_delivery(self):
        draft = self.decide('post')
        with patch('ctn_data.agent_competition.record_published', side_effect=RuntimeError('Local ledger failed')):
            result = self.controller.publish(draft['id'])['drafts'][0]
        self.assertEqual(result['state'], 'published')
        self.assertEqual(len(self.social.writes), 1)
        self.assertTrue(any(event['kind'] == 'competition_error'
                            for event in self.controller.store.read()['events']))

    def test_reconciled_automatic_post_records_attribution_once_without_resend(self):
        from ctn_data.agent_competition import PREFIX
        draft = self.decide('post')
        self.enable()
        self.social.timeout = 'post'
        with self.controller._claim(self.agent_id) as token, self.assertRaises(AgentError):
            self.controller._publish(draft['id'], None, token, automatic=True)
        self.assertEqual(self.controller.snapshot()['drafts'][0]['state'], 'uncertain')
        conn = connect(self.controller.db_path)
        self.addCleanup(conn.close)
        self.assertIsNone(conn.execute('SELECT value FROM checkpoints WHERE key=?',
                                       (PREFIX + '1000',)).fetchone())
        self.social.timeout = ''
        confirmed = self.controller.reconcile(draft['id'], post_id=1000)['drafts'][0]
        self.assertEqual(confirmed['state'], 'published')
        receipt = json.loads(conn.execute('SELECT value FROM checkpoints WHERE key=?',
                                         (PREFIX + '1000',)).fetchone()[0])
        self.assertEqual(receipt['agent_id'], self.agent_id)
        self.assertEqual(receipt['draft_id'], draft['id'])
        self.controller.reconcile(draft['id'], post_id=1000)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM checkpoints WHERE key=?',
                                     (PREFIX + '1000',)).fetchone()[0], 1)
        self.assertEqual(len(self.social.writes), 1)

    def test_tick_persists_real_demand_and_preserves_weighted_generation_share(self):
        from ctn_data import agent_competition
        self.enable()
        self.controller.save({'name': 'Second local', 'username': 'second_local', 'provider_id': 'fake',
                              'mode': 'auto', 'enabled': True})
        second = next(agent['id'] for agent in self.controller.snapshot()['agents']
                      if agent['id'] != self.agent_id)
        self.controller.register(second)
        self.controller.competition({'expected_revision': 0, 'mode': 'resources'})
        def prepare(data, conn):
            agent_competition.sync(conn, data, self.now, ready_ids=[self.agent_id, second])
            competition = data['rate']['competition']
            competition['allocations'][self.agent_id]['share'] = .75
            competition['allocations'][second]['share'] = .25
            for aid in (self.agent_id, second):
                data['agents'][aid]['next_run_at'] = 0
                data['memory'].setdefault(aid, {})['observation'] = {'next_refresh_at': self.now + 3600}
        self.controller.store.change(prepare)
        self.controller._competition_next_refresh = self.now + 3600
        admitted = []
        def submit(aid, automatic=False):
            self.assertTrue(automatic)
            current = self.controller.store.read()
            # The scheduler must persist all live contenders before the worker
            # acquires its grant; sorting a detached snapshot gives false fairness.
            self.assertEqual(set(current['rate']['competition']['fair']['generate']['pending']),
                             {self.agent_id, second})
            with self.controller._claim(aid) as token:
                self.controller._competition_generation(current['agents'][aid], token, True)
            admitted.append(aid)
        with patch.object(self.controller.generations, 'submit', side_effect=submit), \
                patch('ctn_data.agent_observation.pending_revision', return_value=None):
            for _ in range(24):
                self.controller.tick()
                self.now += .25
        self.assertEqual(len(admitted), 24)
        self.assertLessEqual(abs(admitted.count(self.agent_id) - 18), 1)
        self.assertLessEqual(abs(admitted.count(second) - 6), 1)
        persisted = self.controller.store.read()['rate']['competition']['fair']['generate']
        self.assertGreater(persisted['served'][self.agent_id], 0)
        self.assertGreater(persisted['served'][second], 0)
        self.model.assert_not_called()
        self.assertEqual(self.social.writes, [])


class CompetitionSamplingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'sampling.sqlite3'
        self.now = datetime(2026, 10, 20, 2, 0, 0, tzinfo=timezone.utc).timestamp()
        initialize(self.path)
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        task_queue.ensure_schema(self.conn)
        save_posts(self.conn, [{'post_id': 7, 'author_id': 8, 'body': 'Test post'}],
                   'fixture', self.iso(self.now - 3600))
        self.conn.commit()

    @staticmethod
    def iso(at):
        return datetime.fromtimestamp(at, timezone.utc).isoformat().replace('+00:00', 'Z')

    def payload(self, post_id=7):
        return {'post_id': post_id, 'sampling_queue': 'tracked', 'service': 'social',
                'path': f'/api/posts/{post_id}/counters', 'requested_by': 'agent_competition',
                'target_at': self.now, 'deadline': (self.now + 120) / 86400 + 2440587.5}

    def task(self, payload):
        with self.conn:
            task_queue.enqueue(self.conn, 'counter:7', 'counter', payload, due_at=self.now)
        return task_queue.claim(self.conn, 'test-worker', ['counter'], now=self.now)

    def execute(self, task):
        transport = Mock(attempts=1)
        transport.get_json.return_value = {'like_count': 5, 'reply_count': 2,
                                          'repost_count': 1, 'view_count': 20}
        with patch('ctn_data.collector.time.time', return_value=self.now), \
                patch('ctn_data.collector.utc_now', return_value=self.iso(self.now)), \
                patch('ctn_data.collector.ReadOnlyTransport', return_value=transport):
            result = collector._execute_counter_task(self.path, {}, task)
        return result, transport

    def test_pre_target_fresh_counter_does_not_suppress_maturity_sample(self):
        save_counters(self.conn, 7, {'like_count': 1, 'reply_count': 0,
                                    'repost_count': 0, 'view_count': 10}, self.iso(self.now - 20))
        self.conn.commit()
        result, transport = self.execute(self.task(self.payload()))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['counters'], 1)
        transport.get_json.assert_called_once_with('social', '/api/posts/7/counters', entity_id=7)

    def test_ordinary_queue_keeps_existing_freshness_guard(self):
        save_counters(self.conn, 7, {'like_count': 1}, self.iso(self.now - 20))
        self.conn.commit()
        ordinary = self.payload()
        ordinary.pop('requested_by')
        result, transport = self.execute(self.task(ordinary))
        self.assertEqual(result['status'], 'already_sampled')
        transport.get_json.assert_not_called()

    def test_missed_deadline_never_makes_late_network_request(self):
        payload = self.payload()
        payload['deadline'] = (self.now - 1) / 86400 + 2440587.5
        result, transport = self.execute(self.task(payload))
        self.assertEqual(result['status'], 'window_expired')
        transport.get_json.assert_not_called()

    def test_maintenance_promotes_pending_samples_but_never_replaces_inflight_payload(self):
        controller = AgentController(self.path, self.root / 'keys', clock=lambda: self.now)
        with self.conn:
            task_queue.enqueue(self.conn, 'counter:8', 'counter', {'post_id': 8, 'original': True}, due_at=self.now - 10)
        held = task_queue.claim(self.conn, 'other-worker', ['counter'], now=self.now)
        with self.conn:
            task_queue.enqueue(self.conn, 'counter:7', 'counter', {'post_id': 7}, due_at=self.now - 5)
        pending = [{'post_id': post_id, 'due_at': self.now, 'target_at': self.now,
                    'deadline': self.now + 120} for post_id in (7, 8)]
        # Sampling runs only when competition is not off; with it off the tick skips the store pass.
        controller.store.change_budget(lambda data, conn: data['rate'].setdefault('competition', {}).setdefault(
            'settings', {}).update(mode='observe'))
        with patch('ctn_data.agent_competition.sync'), patch('ctn_data.agent_competition.refresh'), \
                patch('ctn_data.agent_competition.prepare_refresh', return_value=None), \
                patch('ctn_data.agent_competition.snapshot', return_value={
                    'settings': {'mode': 'observe'}, 'pending_samples': pending}):
            controller._competition_tick()
        promoted = self.conn.execute("SELECT * FROM collection_tasks WHERE task_key='counter:7'").fetchone()
        inflight = self.conn.execute("SELECT * FROM collection_tasks WHERE task_key='counter:8'").fetchone()
        self.assertEqual(promoted['priority'], 100)
        self.assertEqual(json.loads(promoted['payload_json'])['requested_by'], 'agent_competition')
        self.assertEqual(inflight['lease_token'], held['lease_token'])
        self.assertEqual(json.loads(inflight['payload_json']), {'post_id': 8, 'original': True})
        self.assertEqual(task_queue.claim(self.conn, 'next-worker', ['counter'], now=self.now)['task_key'], 'counter:7')


if __name__ == '__main__':
    unittest.main()
