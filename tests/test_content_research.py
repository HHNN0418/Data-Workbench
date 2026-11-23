"""Manual research and optional actor context, with isolated DBs and fake AI."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from ctn_data import content_research as research
from ctn_data.agent_control import AgentController, AgentError, PERSONA_FIELDS
from ctn_data.db import connect, initialize
from ctn_data.server import create_app
from tests.auth_helpers import login, completed_job

NOW = '2026-10-20T01:00:00Z'
SOURCE = {'id': 'post:7', 'kind': 'post', 'post_id': 7, 'author_id': 99,
          'title': 'Library discussion', 'text': 'A public fictional library discussion.',
          'version': 'v1', 'notification_ids': [], 'group': 'popular'}
EVIDENCE = {'sources': [SOURCE], 'summary': {'scope_count': 1}, 'features': [],
            'caveats': [], 'retrieval': {'method': 'lexical'}}


def add_post(db):
    with closing(connect(db)) as conn, conn:
        conn.execute('INSERT INTO posts(post_id,author_id,body,kind,visibility,created_at,first_seen,last_seen) '
                     'VALUES(?,?,?,?,?,?,?,?)',
                     (7, 99, SOURCE['text'], 'post', 'public', NOW, NOW, NOW))


def report(db, text='具体开头值得比较，但不能推断因果。[post:7]', now=NOW):
    return research.save_report(db, report=text, evidence=EVIDENCE, provider_id='fake',
                                model='fake-model', scope={'own': 'all'}, now=now)


class ResearchStorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / 'research.db'
        initialize(self.db)
        add_post(self.db)

    def test_report_durable_but_inactive_until_explicit_activation(self):
        saved = report(self.db)
        self.assertEqual(saved['revision'], 1)
        self.assertIsNone(saved['active_brief_id'])
        self.assertIsNone(research.context_for(self.db, NOW))
        fresh = research.snapshot(str(self.db), NOW)
        self.assertEqual(fresh['briefs'][0]['report'], saved['briefs'][0]['report'])
        activated = research.activate(self.db, saved['created_brief_id'], 1, NOW)
        self.assertEqual(activated['revision'], 2)
        self.assertEqual(research.context_for(self.db, NOW)['brief_id'], saved['created_brief_id'])
        self.assertEqual(research.activate(self.db, None, 2, NOW)['active_brief_id'], None)

    def test_concurrent_activation_has_exactly_one_cas_winner(self):
        saved = report(self.db)
        barrier = threading.Barrier(2)

        def activate():
            barrier.wait()
            try:
                return research.activate(self.db, saved['created_brief_id'], 1, NOW)['revision']
            except research.ResearchError as exc:
                return exc.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: activate(), range(2)))
        self.assertEqual(sorted(results), [2, 409])

    def test_expiry_blocks_activation_and_context_without_erasing_history(self):
        saved = report(self.db)
        research.activate(self.db, saved['created_brief_id'], 1, NOW)
        expired = '2026-10-20T03:00:00Z'
        self.assertIsNone(research.context_for(self.db, expired))
        view = research.snapshot(self.db, expired)
        self.assertTrue(view['briefs'][0]['expired'])
        self.assertFalse(view['briefs'][0]['can_activate'])
        self.assertEqual(view['briefs'][0]['report'], saved['briefs'][0]['report'])
        with self.assertRaises(research.ResearchError):
            research.activate(self.db, saved['created_brief_id'], 2, expired)

    def test_unknown_original_citation_cannot_be_activated_or_replace_active(self):
        saved = report(self.db)
        active = saved['created_brief_id']
        research.activate(self.db, active, 1, NOW)
        bad = report(self.db, 'Unknown claim [post:999]')
        self.assertEqual(bad['active_brief_id'], active)
        self.assertEqual(bad['briefs'][0]['citation_errors'], ['post:999'])
        self.assertIn('[post:999]', bad['briefs'][0]['report'])
        with self.assertRaises(research.ResearchError):
            research.activate(self.db, bad['created_brief_id'], bad['revision'], NOW)

    def test_private_or_remotely_deleted_sources_stop_future_injection(self):
        saved = report(self.db)
        research.activate(self.db, saved['created_brief_id'], 1, NOW)
        with closing(connect(self.db)) as conn, conn:
            conn.execute("UPDATE posts SET visibility='private' WHERE post_id=7")
        self.assertIsNone(research.context_for(self.db, NOW))
        with self.assertRaises(research.ResearchError):
            research.activate(self.db, saved['created_brief_id'], 2, NOW)
        with closing(connect(self.db)) as conn, conn:
            conn.execute("UPDATE posts SET visibility='public' WHERE post_id=7")
            conn.execute('INSERT INTO meta(key,value) VALUES(?,?)',
                         ('agent_control_v1:rate', json.dumps({'remote_deleted_posts': {
                             '7': {'post_id': 7, 'confirmed_at': NOW}}})))
        self.assertIsNone(research.context_for(self.db, NOW))

    def test_history_and_references_are_bounded_without_evicting_active(self):
        first = report(self.db)
        active = first['created_brief_id']
        research.activate(self.db, active, 1, NOW)
        for _ in range(12):
            report(self.db)
        state = research.snapshot(self.db, NOW)
        self.assertEqual(len(state['briefs']), 10)
        self.assertIn(active, {row['brief_id'] for row in state['briefs']})
        revision = state['revision']
        for _ in range(35):
            research.record_reference(self.db, 'actor', active, outcome='succeeded', now=NOW)
        state = research.snapshot(self.db, NOW)
        self.assertEqual(state['revision'], revision)
        self.assertEqual(len(state['references']), 30)
        self.assertEqual({row['status'] for row in state['references']}, {'provided'})

    def test_context_is_bounded_and_corrupt_state_is_not_overwritten(self):
        saved = report(self.db, '建议仅供参考。[post:7]' * 1000)
        research.activate(self.db, saved['created_brief_id'], 1, NOW)
        context = research.context_for(self.db, NOW)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), research.MAX_CONTEXT_CHARS)
        self.assertTrue(context['report_truncated'])
        with closing(connect(self.db)) as conn, conn:
            conn.execute('UPDATE checkpoints SET value=? WHERE key=?', ('{bad', research.KEY))
        with self.assertRaises(research.ResearchError):
            report(self.db)
        with closing(connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT value FROM checkpoints WHERE key=?',
                                          (research.KEY,)).fetchone()[0], '{bad')

    def test_failed_reference_retains_safe_reason_and_diagnostics_only(self):
        saved = report(self.db)
        research.record_reference(self.db, 'actor', saved['created_brief_id'], outcome='failed',
            now=NOW, error='模型失败 Bearer synthetic-private-token',
            diagnostics={'stage': 'body', 'received_bytes': 300, 'response_body': 'private'},
            generation_job_id='test-job')
        ref = research.snapshot(self.db, NOW)['references'][0]
        self.assertIn('模型失败', ref['error'])
        self.assertNotIn('synthetic-private-token', json.dumps(ref))
        self.assertNotIn('response_body', ref['diagnostics'])
        self.assertEqual(ref['diagnostics']['received_bytes'], 300)
        self.assertEqual(ref['generation_job_id'], 'test-job')

    def test_legacy_failure_enriches_from_audit_without_writing_checkpoint(self):
        from ctn_data import agent_audit
        saved = report(self.db)
        research.record_reference(self.db, 'actor', saved['created_brief_id'], outcome='failed', now=NOW)
        with closing(connect(self.db)) as conn, conn:
            agent_audit.initialize(conn)
            agent_audit.record(conn, [{'id': 'legacy-error', 'agent_id': 'actor',
                'at': '2026-10-20T01:00:20Z', 'kind': 'error',
                'message': 'AI 输出预算已耗尽，最终回答未完成'}])
            before = conn.execute('SELECT value FROM checkpoints WHERE key=?', (research.KEY,)).fetchone()[0]
        ref = research.snapshot(self.db, NOW)['references'][0]
        self.assertEqual(ref['error_code'], 'output_truncated')
        self.assertEqual(ref['error_source'], 'audit')
        self.assertIn('预算已耗尽', ref['error'])
        with closing(connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT value FROM checkpoints WHERE key=?',
                                          (research.KEY,)).fetchone()[0], before)

    def test_legacy_failure_never_borrows_error_after_completed_or_cancelled_call(self):
        from ctn_data import agent_audit
        saved = report(self.db)
        with closing(connect(self.db)) as conn, conn:
            agent_audit.initialize(conn)
            for aid, kind in [('completed', 'generation_completed'), ('cancelled', 'generation_cancelled'),
                              ('new-job', 'generation_started')]:
                agent_audit.record(conn, [
                    {'id': aid+'-boundary', 'agent_id': aid, 'at': '2026-10-20T01:00:10Z',
                     'kind': kind, 'message': 'Previous call ended'},
                    {'id': aid+'-unrelated', 'agent_id': aid, 'at': '2026-10-20T01:01:00Z',
                     'kind': 'error', 'message': 'Unrelated later failure'}])
        for aid in ('completed', 'cancelled', 'new-job'):
            research.record_reference(self.db, aid, saved['created_brief_id'], outcome='failed', now=NOW)
        self.assertTrue(all('error' not in ref for ref in research.snapshot(self.db, NOW)['references']))


class ResearchEndpointTests(unittest.TestCase):
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
            'id': 'fake', 'protocol': 'openai_chat', 'model': 'fake-model',
            'api_key': 'synthetic-test-only', 'enabled': True}).start()
        self.evidence = patch('ctn_data.ai_evidence.build_evidence', return_value=EVIDENCE).start()
        self.complete = patch('ctn_data.ai_providers.complete', return_value={
            'text': '有证据的表达建议 [post:7]', 'model': 'fake-model', 'usage': {'total_tokens': 12}}).start()
        self.addCleanup(patch.stopall)

    def post(self, endpoint, payload):
        return completed_job(self.client, self.client.post('/api/ai/research' + endpoint, json=payload,
                                headers={'X-CTN-Token': self.token}))

    def test_manual_run_saves_server_result_and_activation_does_not_call_model(self):
        response = self.post('/run', {'mode': 'popular', 'provider_id': 'fake',
                                      'params': {'own': 'all'}, 'instruction': '比较开头'})
        self.assertEqual(response.status_code, 200, response.text)
        state = response.json()
        self.assertIsNone(state['active_brief_id'])
        self.assertEqual(state['briefs'][0]['scope'], {'own': 'all'})
        self.assertEqual(state['briefs'][0]['usage'], {'total_tokens': 12})
        self.assertNotIn('synthetic-test-only', response.text)
        active = self.post('/active', {'brief_id': state['created_brief_id'], 'expected_revision': 1})
        self.assertEqual(active.status_code, 200, active.text)
        self.assertEqual(self.client.get('/api/ai/research').json()['active_brief_id'], state['created_brief_id'])
        self.complete.assert_called_once()
        self.assertEqual(self.post('/active', {'brief_id': None, 'expected_revision': 1}).status_code, 409)

    def test_no_data_bad_mode_and_read_only_requests_never_call_model(self):
        self.assertEqual(self.client.get('/api/ai/research').status_code, 200)
        self.assertEqual(self.post('/active', {'brief_id': None, 'expected_revision': 0}).status_code, 200)
        self.assertEqual(self.post('/run', {'mode': 'write', 'provider_id': 'fake'}).status_code, 400)
        self.evidence.return_value = {**EVIDENCE, 'sources': []}
        self.assertEqual(self.post('/run', {'mode': 'popular', 'provider_id': 'fake'}).status_code, 400)
        self.complete.assert_not_called()

    def test_research_output_budget_is_per_request_and_does_not_change_provider(self):
        self.provider.return_value['max_output_tokens'] = 1800
        before = dict(self.provider.return_value)
        with patch.object(self.app.state.ai_providers, 'save') as save:
            overridden = self.post('/run', {'mode': 'popular', 'provider_id': 'fake',
                                            'max_output_tokens': 8192})
            self.assertEqual(overridden.status_code, 200, overridden.text)
            self.assertIsNone(self.complete.call_args.kwargs['max_output_tokens'])
            self.assertEqual(self.provider.return_value, before)
            inherited = self.post('/run', {'mode': 'popular', 'provider_id': 'fake'})
            self.assertEqual(inherited.status_code, 200, inherited.text)
            self.assertIsNone(self.complete.call_args.kwargs['max_output_tokens'])
            self.assertEqual(self.complete.call_args.args[0]['max_output_tokens'], 1800)
            save.assert_not_called()
        self.assertEqual(self.provider.return_value, before)

    def test_invalid_research_output_budget_is_rejected_before_model_call(self):
        for invalid in (True, '8192', 8192.0):
            with self.subTest(value=invalid):
                response = self.post('/run', {'mode': 'popular', 'provider_id': 'fake',
                                              'max_output_tokens': invalid})
                self.assertEqual(response.status_code, 400, response.text)
        self.complete.assert_not_called()
        self.provider.assert_not_called()

    def test_output_budget_override_does_not_expand_other_ai_interfaces(self):
        response = self.client.post('/api/ai/run', json={
            'mode': 'popular', 'provider_id': 'fake', 'max_output_tokens': 8192},
            headers={'X-CTN-Token': self.token})
        self.assertEqual(response.status_code, 400, response.text)
        self.complete.assert_not_called()

    def test_unknown_citations_remain_invalid_and_overlong_results_do_not_replace_state(self):
        self.complete.return_value = {'text': '错误来源 [post:999]', 'model': 'fake-model'}
        result = self.post('/run', {'mode': 'popular', 'provider_id': 'fake'}).json()
        self.assertEqual(result['briefs'][0]['citation_status'], 'invalid')
        self.assertFalse(result['briefs'][0]['can_activate'])
        self.assertEqual(self.post('/active', {'brief_id': result['created_brief_id'],
                                             'expected_revision': 1}).status_code, 400)
        self.complete.return_value = {'text': 'x' * 50000 + ' [post:7]'}
        self.assertEqual(self.post('/run', {'mode': 'popular', 'provider_id': 'fake'}).status_code, 200)
        self.assertEqual(self.client.get('/api/ai/research').json()['revision'], 2)

    def test_research_routes_require_member_and_csrf(self):
        self.assertEqual(self.client.post('/api/ai/research/run',
                                          json={'mode': 'popular', 'provider_id': 'fake'}).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/ai/research').status_code, 401)
        self.assertEqual(self.post('/run', {'mode': 'popular', 'provider_id': 'fake'}).status_code, 401)
        self.complete.assert_not_called()


class ResearchActorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / 'actor.db'
        self.now = research._now(NOW).timestamp()
        self.controller = AgentController(self.db, Path(directory.name) / 'test-secrets',
            provider_store=SimpleNamespace(get=lambda _: {
                'id': 'fake', 'protocol': 'openai_chat', 'model': 'fake-model', 'max_output_tokens': 1800}),
            clock=lambda: self.now)
        self.addCleanup(self.controller.stop)
        self.controller.save({'name': 'Reader', 'username': 'fictional_reader',
                              'provider_id': 'fake', 'background': 'A fictional librarian.'})
        self.agent_id = self.controller.snapshot()['agents'][0]['id']
        self.controller.store.change(lambda data, conn: data['agents'][self.agent_id].update(
            account_id=10, account_state='ready'))
        add_post(self.db)
        self.read = patch.object(self.controller, '_read_sources', return_value=[SOURCE]).start()
        self.model = patch('ctn_data.agent_budget.model_call', return_value={
            'text': json.dumps({'action': 'post', 'body': 'A question about our library.',
                               'memory': '', 'reason': 'Relevant local discussion.'}),
            'model': 'fake-model', 'usage': {}}).start()
        self.addCleanup(patch.stopall)

    def enable(self):
        saved = report(self.db)
        research.activate(self.db, saved['created_brief_id'], saved['revision'], NOW)
        return saved['created_brief_id']

    def test_injection_preserves_persona_schema_and_records_provision_only(self):
        identifier = self.enable()
        before = self.controller.store.read()['agents'][self.agent_id]
        self.controller.run(self.agent_id)
        state = self.controller.store.read()
        after = state['agents'][self.agent_id]
        self.assertEqual({key: before[key] for key in PERSONA_FIELDS},
                         {key: after[key] for key in PERSONA_FIELDS})
        self.assertEqual(before['provider_id'], after['provider_id'])
        context = json.loads(self.model.call_args.args[2][1]['content'])
        self.assertEqual(context['strategy_context']['brief_id'], identifier)
        self.assertIn('competition_feedback', context)
        self.assertEqual(context['competition_feedback']['mode'], 'off')
        system = self.model.call_args.args[2][0]['content']
        for instruction in ('争取更多得分', '内容分析参考', '只在 allowed_actions 内自主选择动作', '文字的方法', '反馈迭代'):
            self.assertIn(instruction, system)
        self.assertIn('不是人设', context['strategy_context']['interpretation'])
        self.assertNotIn('strategy_decision', self.model.call_args.kwargs['response_schema']['properties'])
        self.assertEqual(self.model.call_count, 1)
        references = research.snapshot(self.db, NOW)['references']
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]['status'], 'provided')
        self.assertEqual(references[0]['call_outcome'], 'succeeded')
        self.assertIn(references[0]['draft_id'], state['drafts'])
        self.assertEqual(state['drafts'][references[0]['draft_id']]['state'], 'pending')
        self.assertEqual(state['drafts'][references[0]['draft_id']]['competition_feedback'], context['competition_feedback'])

    def test_inactive_expired_or_corrupt_report_does_not_break_actor(self):
        for condition in ('inactive', 'expired', 'corrupt'):
            with self.subTest(condition=condition):
                if condition == 'expired':
                    self.enable()
                    self.now += 7200
                elif condition == 'corrupt':
                    with closing(connect(self.db)) as conn, conn:
                        conn.execute('UPDATE checkpoints SET value=? WHERE key=?', ('broken', research.KEY))
                self.model.return_value['text'] = json.dumps({
                    'action': 'skip', 'body': '', 'memory': '', 'reason': 'Waiting for evidence.'})
                self.controller.run(self.agent_id)
                context = json.loads(self.model.call_args.args[2][1]['content'])
                self.assertNotIn('strategy_context', context)
        events = self.controller.store.read()['events']
        self.assertTrue(any(row['kind'] == 'research_reference_error' for row in events))

    def test_no_sources_or_budget_deferral_does_not_record_provision(self):
        from ctn_data.agent_budget import Deferred
        self.enable()
        self.read.return_value = []
        self.controller.run(self.agent_id)
        self.model.assert_not_called()
        self.assertEqual(research.snapshot(self.db, NOW)['references'], [])
        self.read.return_value = [SOURCE]
        self.model.side_effect = Deferred('llm:fake', 2)
        with self.assertRaises(AgentError):
            self.controller.run(self.agent_id)
        self.assertEqual(research.snapshot(self.db, NOW)['references'], [])

    def test_failure_after_request_started_records_failed_reference_without_draft(self):
        from ctn_data.ai_providers import ProviderError
        self.enable()

        def failure(*args, **kwargs):
            kwargs['on_progress']({'stage': 'headers'})
            raise ProviderError('Synthetic failure', 502)

        self.model.side_effect = failure
        with self.assertRaises(AgentError):
            self.controller.run(self.agent_id)
        references = research.snapshot(self.db, NOW)['references']
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]['call_outcome'], 'failed')
        self.assertEqual(references[0]['error'], 'Synthetic failure')
        self.assertEqual(self.model.call_count, 1)
        self.assertNotIn('draft_id', references[0])

    def test_truncation_recovery_accounts_for_both_calls_and_saves_only_complete_result(self):
        from ctn_data.ai_providers import ProviderError
        self.enable()
        complete = self.model.return_value

        def decide(*args, **kwargs):
            kwargs['on_progress']({'stage': 'headers'})
            if self.model.call_count == 1:
                raise ProviderError('输出被截断，未保存部分结果', diagnostics={
                    'error_code': 'output_truncated', 'finish_reason': 'length', 'answer_chars': 18})
            return complete

        self.model.side_effect = decide
        self.controller.run(self.agent_id)
        self.assertEqual(self.model.call_count, 2)
        first, second = self.model.call_args_list
        self.assertEqual(first.args[2][1], second.args[2][1])
        self.assertEqual(first.kwargs['response_schema'], second.kwargs['response_schema'])
        self.assertIn('完整', second.args[2][0]['content'])
        self.assertNotIn('max_output_tokens', second.kwargs)
        refs = research.snapshot(self.db, NOW)['references']
        self.assertEqual([row['call_outcome'] for row in refs], ['succeeded', 'failed'])
        self.assertEqual(refs[1]['error_code'], 'output_truncated')
        self.assertNotIn('draft_id', refs[1])
        self.assertIn('draft_id', refs[0])
        state = self.controller.store.read()
        self.assertEqual(len(state['drafts']), 1)
        self.assertTrue(any(event['kind'] == 'generation_retry' for event in state['events']))

    def test_persistent_truncation_stops_after_one_recovery_and_keeps_no_draft(self):
        from ctn_data.ai_providers import ProviderError
        self.enable()

        def truncated(*args, **kwargs):
            kwargs['on_progress']({'stage': 'headers'})
            raise ProviderError('输出被截断', diagnostics={
                'error_code': 'output_truncated', 'finish_reason': 'length', 'reasoning_tokens': 3000})

        self.model.side_effect = truncated
        with self.assertRaises(AgentError):
            self.controller.run(self.agent_id)
        self.assertEqual(self.model.call_count, 2)
        self.assertEqual(len(research.snapshot(self.db, NOW)['references']), 2)
        self.assertFalse(self.controller.store.read()['drafts'])
        with closing(connect(self.db)) as conn:
            event = conn.execute("SELECT details_json FROM agent_audit_events WHERE kind='error'").fetchone()
        self.assertEqual(json.loads(event['details_json'])['reasoning_tokens'], 3000)

    def test_cancellation_after_truncation_prevents_recovery_request(self):
        from ctn_data.ai_providers import ProviderError
        self.enable()
        cancelled = False

        def check(*args, **kwargs):
            if cancelled:
                raise AgentError('AI 请求已取消', 499)

        def truncated(*args, **kwargs):
            nonlocal cancelled
            kwargs['on_progress']({'stage': 'headers'})
            cancelled = True
            raise ProviderError('输出被截断', diagnostics={
                'error_code': 'output_truncated', 'finish_reason': 'length'})

        self.model.side_effect = truncated
        with patch.object(self.controller.generations, 'check_cancelled', side_effect=check):
            with self.assertRaises(AgentError) as raised:
                self.controller.run(self.agent_id)
        self.assertEqual(raised.exception.status_code, 499)
        self.assertEqual(self.model.call_count, 1)
        self.assertFalse(self.controller.store.read()['drafts'])


if __name__ == '__main__':
    unittest.main()
