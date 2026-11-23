"""Review protocol, publication language and fencing tests, with no remote IO."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from ctn_data import agent_review as review
from ctn_data.agent_budget import Deferred
from ctn_data.agent_control import AgentError, AgentStore
from ctn_data.db import connect


class Reviews(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'test.db'
        self.store = AgentStore(self.path)
        self.agent = str(uuid.uuid4())
        self.provider = {'id': 'review-provider', 'name': 'Review model', 'model': 'offline-test',
                         'protocol': 'openai_chat', 'enabled': True, 'has_key': True,
                         'base_url': 'https://review.example.test'}
        self.controller = SimpleNamespace(store=self.store, owner='test-owner', clock=lambda: 1000,
            providers=SimpleNamespace(get=lambda selected: dict(self.provider),
                list_public=lambda: {'providers': [dict(self.provider)]}),
            _interaction_dispatch_lock=threading.RLock(),
            _draft=lambda data, key: data['drafts'][key])
        self.store.change(lambda data, conn: data['agents'].update({self.agent: {
            'id': self.agent, 'name': 'Fictional citizen', 'account_id': 17, 'version': 1,
            'persona_revision': 1, 'username': 'fictional_citizen'}}))
        self.model = patch('ctn_data.agent_budget.model_call', side_effect=AssertionError('No live model')).start()
        self.addCleanup(patch.stopall)

    def draft(self, body='Hello world 🙂', action='post', **values):
        draft = {'id': str(uuid.uuid4()), 'agent_id': self.agent, 'body': body, 'action': action,
                 'state': 'pending', 'version': 1, 'created_at': '2026-10-21T00:00:00Z',
                 'sources': [], **values}
        self.store.change(lambda data, conn: data['drafts'].update({draft['id']: draft}))
        return draft['id']

    def configure(self, enabled=False, **values):
        current = review.settings(self.controller)['settings']
        return review.settings(self.controller, {'expected_revision': current['revision'],
            'enabled': enabled, 'provider_id': 'review-provider', 'platform_policy': 'hold', **values})

    def answer(self, *, semantic=True, language='pass', checks=None, evidence=None):
        if checks is None:
            checks = [{'rule_id': rule['id'], 'status': 'pass', 'reason': '已按适用条件检查',
                       'evidence': [], 'missing': []} for rule in review.RULES if rule['id'] != 'U01'] if semantic else []
        return {'text': json.dumps({'language': {'status': language, 'reason': '语言符合适用要求',
                    'evidence': evidence or []}, 'checks': checks}), 'model': 'offline-test', 'usage': {}}

    def allow_model(self, result):
        self.model.side_effect = None
        self.model.return_value = result

    def gate(self, draft_id, body=None, target=None):
        return self.store.change(lambda data, conn: review.check(
            conn, data, data['drafts'][draft_id], body, target_result=target))

    def test_default_off_needs_no_approval_or_model(self):
        config = review.settings(self.controller)
        self.assertEqual(config['status'], 'disabled')
        self.assertFalse(config['settings']['enabled'])
        self.assertTrue(config['settings']['english_only'])
        self.assertEqual({r['id'] for r in config['rules'] if r['layer'] == 'competition'},
                         {f'C{n:02}' for n in range(1, 21)})
        self.assertEqual(self.gate(self.draft())['status'], 'disabled')
        self.configure(True)
        with self.assertRaises(AgentError):
            self.gate(self.draft())
        self.model.assert_not_called()

    def test_positive_english_emoji_sequences_and_ascii_faces_need_no_model(self):
        for body in ('Hello world 🙂', '👨🏽‍⚕️ 🇦🇺 🏳️‍🌈 ❤️ 1️⃣', '123 + 45 = 168!', ':) :D ;-) <3',
                     r'¯\_(ツ)_/¯', '(｡･ω･｡)ﾉ♡', 'ʕ•ᴥ•ʔ', 'ಠ_ಠ', 'α + β = γ → ∞ ≠ √2'):
            with self.subTest(body=body):
                draft_id = self.draft(body)
                result = review.ensure(self.controller, draft_id, body)
                self.assertEqual(result['status'], 'disabled')
                self.assertNotIn('review', self.store.read()['drafts'][draft_id])
                self.gate(draft_id)
        self.model.assert_not_called()

    def test_chinese_mixed_hastag_and_non_english_repost_are_held(self):
        self.configure(True)
        for body in ('Hello 世界 🙂', '#中文', 'Good view “你好”'):
            with self.subTest(body=body), self.assertRaises(AgentError):
                review.ensure(self.controller, self.draft(body), body)
        draft_id = self.draft('', 'repost', target_post_id=7)
        with self.assertRaises(AgentError):
            review.ensure(self.controller, draft_id, '', target_result={'post_id': 7, 'body': '中文原帖'})
        result = self.store.read()['drafts'][draft_id]
        self.assertEqual(result['review']['language']['status'], 'blocked')
        self.assertTrue(result['requires_review'])
        self.model.assert_not_called()

    def test_latin_foreign_language_unknown_words_need_selected_model(self):
        draft_id = self.draft('Bonjour tout le monde')
        self.assertEqual(review.ensure(self.controller, draft_id, 'Bonjour tout le monde')['status'], 'disabled')
        self.assertNotIn('review', self.store.read()['drafts'][draft_id])
        self.model.assert_not_called()
        self.configure(True)
        evidence = [{'field': 'body', 'quote': 'Bonjour', 'start': 0, 'end': 7}]
        self.allow_model(self.answer(language='blocked', evidence=evidence))
        with self.assertRaises(AgentError):
            review.ensure(self.controller, draft_id, 'Bonjour tout le monde')
        self.assertEqual(self.model.call_args.args[1]['id'], 'review-provider')

    def test_borrowed_words_and_names_are_judged_by_context_not_rejected_for_accents(self):
        self.configure(True)
        self.allow_model(self.answer())
        body = 'Kingstonia IHO café'
        result = review.ensure(self.controller, self.draft(body), body)
        self.assertEqual(result['status'], 'pass')
        self.model.assert_called_once()

    def test_reply_parent_language_is_context_and_known_mention_is_identifier(self):
        body = 'Hello @fictional_citizen https://example.test/path'
        draft_id = self.draft(body, 'reply', target_post_id=7,
            sources=[{'post_id': 7, 'author_id': 13, 'username': 'fictional_citizen', 'text': '中文阅读资料'}])
        result = review.ensure(self.controller, draft_id, body,
            target_result={'post_id': 7, 'author_id': 13, 'body': '中文父帖'})
        self.assertEqual(result['status'], 'disabled')
        self.model.assert_not_called()

    def test_secret_anywhere_in_full_model_input_is_never_sent_or_recorded(self):
        self.configure(True)
        secret = 'sk-' + 'aB9c' * 9
        draft_id = self.draft(sources=[{'text': f'An accidental credential {secret}'}])
        with self.assertRaises(AgentError) as captured:
            review.ensure(self.controller, draft_id, 'Hello world 🙂')
        self.assertNotIn(secret, str(captured.exception))
        self.model.assert_not_called()
        conn = connect(self.path)
        try:
            rows = conn.execute('SELECT record_json FROM agent_reviews').fetchall()
            self.assertTrue(rows)
            self.assertNotIn(secret, ''.join(row[0] for row in rows))
        finally:
            conn.close()
        for nested in ({'source': {'team_code': 'synthetic-value'}}, ['Bearer abcdefghijklmn']):
            with self.assertRaises(AgentError):
                review.assert_no_secrets(nested)

    def test_cleanup_survives_unavailable_provider_and_sensitive_foreign_target(self):
        self.configure(True)
        self.controller.providers.get = lambda selected: (_ for _ in ()).throw(RuntimeError('offline'))
        self.controller.providers.list_public = lambda: {'providers': []}
        for action in ('unlike', 'unfollow', 'delete_post', 'skip'):
            with self.subTest(action=action):
                draft_id = self.draft('', action)
                target = {'post_id': 7, 'body': '中文 API_KEY=sk-' + 'x' * 30}
                result = review.ensure(self.controller, draft_id, '', target_result=target)
                self.assertEqual(result['status'], 'pass')
                self.gate(draft_id, '', target)
        self.model.assert_not_called()

    def test_enabled_review_checks_all_rules_and_runs_outside_transaction(self):
        self.configure(True)
        def answer(*args, **kwargs):
            conn = connect(self.path)
            try:
                conn.execute('BEGIN IMMEDIATE')
                conn.rollback()
            finally:
                conn.close()
            self.assertEqual(set(kwargs['response_schema']['properties']), {'language', 'checks'})
            metadata = json.loads(args[2][1]['content'])['input']['review_service']
            self.assertEqual(metadata, {'provider_id': 'review-provider', 'host': 'review.example.test',
                'model': 'offline-test', 'protocol': 'openai_chat', 'kind': 'external',
                'purpose': 'independent_publication_review_and_language_detection'})
            self.assertNotIn('api_key', metadata)
            return self.answer()
        self.model.side_effect = answer
        result = review.ensure(self.controller, self.draft(), 'Hello world 🙂')
        self.assertEqual(len(result['findings']), 29)
        self.assertEqual(result['review_kind'], 'ai')

    def test_missing_rules_invented_rule_and_fabricated_quote_fail_closed(self):
        self.configure(True)
        cases = [self.answer(), self.answer(), self.answer()]
        first = json.loads(cases[0]['text']); first['checks'].pop(); cases[0]['text'] = json.dumps(first)
        second = json.loads(cases[1]['text']); second['checks'][0]['rule_id'] = 'C99'; cases[1]['text'] = json.dumps(second)
        third = json.loads(cases[2]['text'])
        third['checks'][0].update(status='violation', evidence=[{'field': 'body', 'quote': 'Invented', 'start': 0, 'end': 8}])
        cases[2]['text'] = json.dumps(third)
        for value in cases:
            with self.subTest(value=value['text'][:100]):
                self.allow_model(value)
                draft_id = self.draft()
                with self.assertRaises(AgentError):
                    review.ensure(self.controller, draft_id, 'Hello world 🙂')
                self.assertEqual(self.store.read()['drafts'][draft_id]['review']['status'], 'error')

    def test_platform_warn_never_overrides_competition_block(self):
        self.configure(True, platform_policy='warn')
        result = self.answer()
        parsed = json.loads(result['text'])
        parsed['checks'][0].update(status='violation', evidence=[{'field': 'body', 'quote': 'Hello', 'start': 0, 'end': 5}])
        result['text'] = json.dumps(parsed)
        self.allow_model(result)
        draft_id = self.draft()
        with self.assertRaises(AgentError):
            review.ensure(self.controller, draft_id, 'Hello world 🙂')
        self.assertEqual(self.store.read()['drafts'][draft_id]['review']['status'], 'blocked')
        parsed['checks'][0].update(status='pass', evidence=[])
        parsed['checks'][20].update(status='violation', evidence=[{'field': 'body', 'quote': 'Hello', 'start': 0, 'end': 5}])
        result['text'] = json.dumps(parsed)
        self.allow_model(result)
        self.assertEqual(review.ensure(self.controller, self.draft(), 'Hello world 🙂')['status'],
                         'pass_with_platform_warning')

    def test_approval_binds_exact_case_target_account_and_connection_configuration(self):
        self.configure(True)
        self.allow_model(self.answer())
        draft_id = self.draft('', 'repost', target_post_id=7)
        target = {'post_id': 7, 'author_id': 19, 'body': 'Hello world'}
        review.ensure(self.controller, draft_id, '', target_result=target)
        self.gate(draft_id, '', target)
        with self.assertRaises(AgentError):
            self.gate(draft_id, '', {**target, 'body': 'Hello World'})
        self.store.change(lambda data, conn: data['agents'][self.agent].update(account_id=18))
        with self.assertRaises(AgentError):
            self.gate(draft_id, '', target)
        # The normal reserve transition binding an unset draft account and
        # incrementing draft.version does not invalidate a content approval.
        self.store.change(lambda data, conn: (data['agents'][self.agent].update(account_id=17),
            data['drafts'][draft_id].update(account_id=17, version=2)))
        self.gate(draft_id, '', target)

    def test_configuration_cas_and_disabling_cancels_review_without_leaving_hold(self):
        self.configure(True)
        self.allow_model(self.answer())
        draft_id = self.draft()
        def change_during_call(*args, **kwargs):
            self.configure(False)
            return self.answer()
        self.model.side_effect = change_during_call
        result = review.ensure(self.controller, draft_id, 'Hello world 🙂')
        self.assertEqual(result['status'], 'disabled')
        self.assertEqual(review.ensure(self.controller, draft_id, 'Hello world 🙂', automatic=True)['status'], 'disabled')
        self.assertFalse(self.store.read()['drafts'][draft_id].get('requires_review'))
        self.model.assert_called_once()
        with self.assertRaises(AgentError):
            review.settings(self.controller, {'expected_revision': 1, 'enabled': False})

    def test_disabling_releases_only_reviewer_hold_and_retains_cancellation(self):
        self.configure(True)
        own = self.draft('你好')
        cancelled = self.draft('你好', requires_review=True, review_reason='生成任务已取消')
        for draft_id in (own, cancelled):
            with self.assertRaises(AgentError):
                review.ensure(self.controller, draft_id, '你好')
        self.configure(False)
        state = self.store.read()['drafts']
        self.assertFalse(state[own].get('requires_review'))
        self.assertTrue(state[cancelled]['requires_review'])
        self.assertEqual(state[cancelled]['review_reason'], '生成任务已取消')
        self.model.assert_not_called()

    def test_disabled_local_language_rejection_has_no_durable_review_or_hold(self):
        for body in ('Hello 世界', '(你好)', 'Καλημέρα'):
            draft_id = self.draft(body)
            with self.assertRaises(AgentError):
                review.ensure(self.controller, draft_id, body)
            with self.assertRaises(AgentError):
                self.gate(draft_id)
            draft = self.store.read()['drafts'][draft_id]
            self.assertNotIn('review', draft)
            self.assertFalse(draft.get('requires_review'))
        conn = connect(self.path)
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_reviews').fetchone()[0], 0)
        finally:
            conn.close()
        self.model.assert_not_called()

    def test_disabled_review_ignores_provider_and_legacy_review_hold(self):
        draft_id = self.draft('An unfamiliar word', requires_review=True,
            review_hold_reason='旧审核失败', _review_job={'status': 'error'},
            review={'status': 'error', 'rule_version': review.RULE_VERSION})
        self.controller.providers.get = lambda selected: (_ for _ in ()).throw(AssertionError('must not resolve provider'))
        self.configure(False, provider_id='missing-connection')
        self.assertEqual(review.ensure(self.controller, draft_id, None, automatic=True)['status'], 'disabled')
        self.gate(draft_id)
        self.assertFalse(self.store.read()['drafts'][draft_id].get('requires_review'))
        self.model.assert_not_called()

    def test_disabled_hold_release_is_idempotent_and_preserves_later_cancellation(self):
        draft_id = self.draft('Hello world', state='rejected', requires_review=True,
            delivery_not_started=True, delivery_reason='review_approval_changed',
            review_reason='执行前检查未通过，操作尚未发送；请重新审阅后手动执行')
        self.assertEqual(self.store.read()['drafts'][draft_id]['state'], 'pending')
        self.store.change(lambda data, conn: data['drafts'][draft_id].update(
            requires_review=True, review_reason='生成任务已取消'))
        for _ in range(2):
            row = self.store.read()['drafts'][draft_id]
            self.assertTrue(row['requires_review'])
            self.assertEqual(row['review_reason'], '生成任务已取消')
        self.model.assert_not_called()

    def test_disabling_removes_only_review_quota_wait(self):
        self.configure(True)
        draft_id = self.draft()
        self.model.side_effect = Deferred('llm:review-provider', 600)
        with self.assertRaises(Deferred):
            review.ensure(self.controller, draft_id, None, automatic=True)
        self.store.change(lambda data, conn: data['drafts'][draft_id].update(
            next_attempt_at=1600, quota_wait_bucket='llm:review-provider'))
        self.configure(False)
        row = self.store.read()['drafts'][draft_id]
        self.assertNotIn('next_attempt_at', row)
        self.assertNotIn('quota_wait_bucket', row)
        self.assertEqual(review.ensure(self.controller, draft_id, None, automatic=True)['status'], 'disabled')
        self.model.assert_called_once()

    def test_same_input_late_attempt_cannot_overwrite_new_owner(self):
        self.configure(True)
        draft_id = self.draft()
        newer = str(uuid.uuid4())
        def new_owner(*args, **kwargs):
            self.store.change(lambda data, conn: data['drafts'][draft_id].update(
                _review_job={'id': newer, 'owner': 'new-owner', 'status': 'running', 'until': 5000},
                review={'id': newer, 'status': 'running', 'summary': 'new job'}))
            return self.answer()
        self.model.side_effect = new_owner
        with self.assertRaises(AgentError):
            review.ensure(self.controller, draft_id, 'Hello world 🙂')
        self.assertEqual(self.store.read()['drafts'][draft_id]['review']['id'], newer)

    def test_draft_edit_during_model_call_invalidates_candidate_even_with_override(self):
        self.configure(True)
        draft_id = self.draft()
        def edit(*args, **kwargs):
            self.store.change(lambda data, conn: data['drafts'][draft_id].update(body='A changed view'))
            return self.answer()
        self.model.side_effect = edit
        with self.assertRaises(AgentError):
            review.ensure(self.controller, draft_id, 'Hello world 🙂')
        self.assertEqual(self.store.read()['drafts'][draft_id]['review']['status'], 'stale')

    def test_like_and_follow_are_semantic_reviewed_when_enabled_without_language_gate(self):
        self.configure(True)
        self.allow_model(self.answer(language='not_applicable'))
        for action in ('like', 'follow'):
            target = {'post_id': 7, 'body': '中文背景'} if action == 'like' else {
                'account_id': 19, 'username': 'readable', 'bio': '中文资料'}
            result = review.ensure(self.controller, self.draft('', action), '', target_result=target)
            self.assertEqual(result['review_kind'], 'ai')
            self.assertEqual(result['language']['status'], 'not_applicable')
        self.assertEqual(self.model.call_count, 2)

    def test_budget_deferral_preserves_scheduler_retry_and_does_not_create_manual_hold(self):
        self.configure(True)
        draft_id = self.draft(next_attempt_at=1001)
        deferred = Deferred('llm:review-provider', 15)
        self.model.side_effect = deferred
        with self.assertRaises(Deferred) as captured:
            review.ensure(self.controller, draft_id, 'Hello world 🙂', automatic=True)
        self.assertIs(captured.exception, deferred)
        draft = self.store.read()['drafts'][draft_id]
        self.assertFalse(draft.get('requires_review'))
        self.assertEqual(draft['next_attempt_at'], 1001)
        self.assertEqual(draft['review']['status'], 'queued')
        with self.assertRaises(AgentError):
            self.gate(draft_id)
        self.allow_model(self.answer())
        review.ensure(self.controller, draft_id, 'Hello world 🙂', automatic=True)
        self.gate(draft_id)

    def test_provider_registry_compatibility_defaults_and_changed_endpoint_fence_approval(self):
        self.configure(True)
        self.allow_model(self.answer())
        raw = {**self.provider, 'thinking_mode': 'disabled', 'max_output_tokens': 1024}
        self.store.change(lambda data, conn: conn.execute('INSERT INTO meta(key,value) VALUES(?,?)',
            ('ai_provider_settings_v1', json.dumps({'providers': [raw]}))))
        draft_id = self.draft()
        review.ensure(self.controller, draft_id, 'Hello world 🙂')
        self.gate(draft_id)
        raw['base_url'] = 'https://changed.example.test'
        self.store.change(lambda data, conn: conn.execute('UPDATE meta SET value=? WHERE key=?',
            (json.dumps({'providers': [raw]}), 'ai_provider_settings_v1')))
        with self.assertRaises(AgentError):
            self.gate(draft_id)

    def test_real_provider_store_legacy_defaults_and_connection_change(self):
        from ctn_data.ai_providers import ProviderStore
        providers = ProviderStore(self.path, self.path.parent / 'isolated-test-keys')
        saved = providers.save({'name': 'Local test', 'base_url': 'http://127.0.0.1:12345/v1',
                                'model': 'legacy-model', 'protocol': 'openai_chat'})['providers'][0]
        self.controller.providers = providers
        # Reproduce an old on-disk connection. get() normalizes it on read.
        conn = connect(self.path)
        try:
            with conn:
                row = json.loads(conn.execute('SELECT value FROM meta WHERE key=?', ('ai_provider_settings_v1',)).fetchone()[0])
                row['providers'][0]['thinking_mode'] = 'disabled'
                row['providers'][0]['max_output_tokens'] = 1024
                conn.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(row), 'ai_provider_settings_v1'))
        finally:
            conn.close()
        review.settings(self.controller, {'expected_revision': 1, 'enabled': True,
            'provider_id': saved['id'], 'platform_policy': 'hold'})
        self.allow_model(self.answer())
        draft_id = self.draft()
        review.ensure(self.controller, draft_id, 'Hello world 🙂')
        self.gate(draft_id)
        providers.save({'id': saved['id'], 'model': 'different-model'})
        with self.assertRaises(AgentError):
            self.gate(draft_id)

    def test_missing_repost_original_or_unreadable_media_cannot_be_model_waived(self):
        self.configure(True)
        self.allow_model(self.answer())
        draft_id = self.draft('', 'repost', target_post_id=7)
        with self.assertRaises(AgentError):
            review.ensure(self.controller, draft_id, '', target_result={'post_id': 7})
        self.model.assert_not_called()
        for body in ('https://example.test/meme.png', 'Hello https://example.test/clip.mp4?x=1'):
            with self.subTest(body=body), self.assertRaises(AgentError):
                review.ensure(self.controller, self.draft(body), body)

    def test_nested_repost_without_expanded_original_needs_human_before_model(self):
        self.configure(True)
        self.allow_model(self.answer())
        draft_id = self.draft('', 'repost', target_post_id=7)
        with self.assertRaises(AgentError):
            review.ensure(self.controller, draft_id, '', target_result={
                'post_id': 7, 'body': 'Hello world', 'repost_of_post_id': 19})
        draft = self.store.read()['drafts'][draft_id]
        self.assertEqual(draft['review']['status'], 'needs_human')
        self.assertEqual(draft['review']['language']['status'], 'needs_human')
        self.assertTrue(draft['requires_review'])
        self.model.assert_not_called()

    def test_live_draft_version_may_advance_after_approval_but_not_during_model_call(self):
        self.configure(True)
        draft_id = self.draft()
        def edit(*args, **kwargs):
            self.store.change(lambda data, conn: data['drafts'][draft_id].update(version=2))
            return self.answer()
        self.model.side_effect = edit
        with self.assertRaises(AgentError):
            review.ensure(self.controller, draft_id, 'Hello world 🙂')

    def test_duplicate_json_keys_and_truncated_completion_are_not_approvals(self):
        self.configure(True)
        for value in ({'text': '{"language":{},"language":{},"checks":[]}'},
                      {**self.answer(), 'finish_reason': 'length'}):
            self.allow_model(value)
            draft_id = self.draft()
            with self.assertRaises(AgentError):
                review.ensure(self.controller, draft_id, 'Hello world 🙂')
            self.assertEqual(self.store.read()['drafts'][draft_id]['review']['status'], 'error')

    def test_compact_all_categories_fit_budget_and_expand_to_public_findings(self):
        self.configure(True)
        value = json.loads(self.answer()['text'])
        value['checks'] = {check['rule_id']: 'pass' for check in value['checks']}
        text = json.dumps(value, separators=(',', ':'), ensure_ascii=False)
        self.assertLess(len(text), 1000)
        self.assertLess(len(json.dumps(review._schema(True))), 20000)
        self.allow_model({'text': text})
        result = review.ensure(self.controller, self.draft(), 'Hello world 🙂')
        self.assertEqual(result['status'], 'pass')
        self.assertEqual({row['rule_id'] for row in result['findings']}, review._RULE_IDS)

    def test_compact_violation_still_requires_valid_quote_and_blocks_hard_rule(self):
        self.configure(True, platform_policy='warn')
        value = json.loads(self.answer()['text'])
        value['checks'] = {check['rule_id']: 'pass' for check in value['checks']}
        value['checks']['C20'] = {'status': 'violation', 'reason': '有依据的规则命中',
            'evidence': [{'field': 'body', 'quote': 'Hello', 'start': 0, 'end': 5}], 'missing': []}
        self.allow_model({'text': json.dumps(value)})
        draft_id = self.draft()
        with self.assertRaises(AgentError):
            review.ensure(self.controller, draft_id, 'Hello world 🙂')
        self.assertEqual(self.store.read()['drafts'][draft_id]['review']['status'], 'blocked')
        value['checks']['C20']['evidence'] = []
        self.allow_model({'text': json.dumps(value)})
        draft_id = self.draft()
        with self.assertRaises(AgentError):
            review.ensure(self.controller, draft_id, 'Hello world 🙂')
        self.assertEqual(self.store.read()['drafts'][draft_id]['review']['status'], 'error')

    def test_compact_missing_category_is_an_error_not_implicit_pass(self):
        self.configure(True)
        value = json.loads(self.answer()['text'])
        value['checks'] = {check['rule_id']: 'pass' for check in value['checks']}
        value['checks'].pop('C15')
        self.allow_model({'text': json.dumps(value)})
        draft_id = self.draft()
        with self.assertRaises(AgentError):
            review.ensure(self.controller, draft_id, 'Hello world 🙂')
        self.assertEqual(self.store.read()['drafts'][draft_id]['review']['status'], 'error')


if __name__ == '__main__':
    unittest.main()
