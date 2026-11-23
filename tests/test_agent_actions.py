"""Offline tests for autonomous agent actions and delivery recovery."""
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ctn_data.agent_control import AgentController, AgentError
from ctn_data.agent_budget import Deferred


class Account:
    def __init__(self, social, username, account_id):
        self.social, self.username, self.account_id = social, username, account_id
        self.token, self.expires_at = username, ''
        self.likes, self.reposts, self.resolved = set(), {}, []

    def post(self, body):
        return self.social.write(self, body)

    def reply(self, post_id, body, root_post_id=None):
        return self.social.write(self, body, reply_to_post_id=post_id, root_post_id=root_post_id)

    def repost(self, post_id, comment=None):
        result = self.social.write(self, comment or '', repost_of_post_id=post_id, kind='repost')
        self.reposts[post_id] = result['post_id']
        if self.social.timeout == 'repost':
            raise TimeoutError('accepted repost')
        return result

    def like(self, post_id):
        self.likes.add(post_id)
        self.social.like_calls.append((self.account_id, post_id))
        if self.social.timeout == 'like':
            raise TimeoutError('accepted like')
        return None

    def my_likes(self, ids):
        return self.likes.intersection(ids)

    def my_reposts(self, ids):
        return {key: value for key, value in self.reposts.items() if key in ids}

    def resolve_notification(self, notification_id):
        self.resolved.append(notification_id)


class Social:
    def __init__(self):
        self.accounts, self.writes, self.like_calls = {}, [], []
        self.posts = {7: {'post_id': 7, 'author_id': 99, 'body': 'Recent discussion', 'root_post_id': 3},
                      8: {'post_id': 8, 'author_id': 98, 'body': 'Another discussion'}}
        self.timeout = ''

    def factory(self, **kwargs):
        assert kwargs['max_retries'] == 0
        return self

    def register(self, username, password, team_code, display_name=None):
        self.accounts[username] = Account(self, username, len(self.accounts) + 10)
        return self.accounts[username]

    def account_from_token(self, token):
        return self.accounts[token]

    def login(self, username, password):
        return self.accounts[username]

    def get_post(self, post_id):
        return self.posts[post_id]

    def write(self, account, body, **relations):
        result = {'post_id': 1000 + len(self.writes), 'author_id': account.account_id, 'body': body, **relations}
        self.posts[result['post_id']] = result
        self.writes.append(result)
        if self.timeout == 'post' and not relations:
            raise TimeoutError('accepted post')
        return result


class AutonomousActions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1792450000.0
        self.social = Social()
        self.provider = {'id': 'fake', 'protocol': 'openai_chat', 'model': 'fake', 'max_output_tokens': 1800}
        self.controller = AgentController(Path(self.temp.name) / 'test.db', Path(self.temp.name) / 'keys',
            provider_store=SimpleNamespace(get=lambda _: self.provider), sdk_factory=self.social.factory,
            clock=lambda: self.now)
        self.buckets = []
        def call(controller, bucket, method, *args, **kwargs):
            self.buckets.append(bucket)
            return method(*args, **kwargs)
        self.call = patch('ctn_data.agent_budget.call', side_effect=call).start()
        self.addCleanup(patch.stopall)
        self.model = patch('ctn_data.agent_budget.model_call').start()
        self.controller.settings({'team_code': 'synthetic-team'})
        self.controller.save({'name': 'Fictional local', 'username': 'fictional_local', 'provider_id': 'fake',
                              'background': 'Fictional resident and librarian', 'stance': 'Vote Yes'})
        self.agent_id = self.controller.snapshot()['agents'][0]['id']
        self.controller.register(self.agent_id)
        self.account = self.social.accounts['fictional_local']
        self.sources = [{'id': 'post:7', 'kind': 'post', 'post_id': 7, 'author_id': 99,
                         'text': 'Recent discussion', 'notification_ids': [101]},
                        {'id': 'post:8', 'kind': 'post', 'post_id': 8, 'author_id': 98,
                         'text': 'Another discussion', 'notification_ids': [102]}]
        def observe(controller, agent, token, refresh=True, for_decision=False):
            def save(data, conn):
                memory = data['memory'].setdefault(agent['id'], {})
                memory.setdefault('inbox', [{'id': 101, 'notification_id': 101, 'post_id': 7, 'status': 'unresolved'},
                                            {'id': 102, 'notification_id': 102, 'post_id': 8, 'status': 'unresolved'}])
                memory['last_sources'] = self.sources
            controller.store.change(save)
            return self.sources
        patch('ctn_data.agent_observation.observe', side_effect=observe).start()

    def decide(self, action='like', **kwargs):
        self.now += 1000
        output = {'action': action, 'body': '' if action in ('skip', 'like', 'repost') else 'A considered view.',
                  'target_post_id': 7 if action in ('reply', 'repost', 'like') else None,
                  'source_ids': ['post:7'], 'notification_ids': [], 'memory': 'I considered a recent discussion.',
                  'reason': 'Matches my interests.', **kwargs}
        self.model.return_value = {'text': json.dumps(output), 'model': 'fake', 'usage': {}}
        self.controller.run(self.agent_id)
        return self.controller.snapshot()['drafts'][0]

    def test_review_like_waits_for_explicit_publish_and_sdk_none_is_success(self):
        draft = self.decide(notification_ids=[101])
        self.assertEqual(draft['state'], 'pending')
        self.assertEqual(self.social.like_calls, [])
        self.assertEqual(self.account.resolved, [])
        result = self.controller.publish(draft['id'])['drafts'][0]
        self.assertEqual(result['state'], 'published')
        self.assertEqual(result['post_id'], 7)
        self.assertEqual(self.social.like_calls, [(10, 7)])
        self.assertEqual(self.account.resolved, [101])
        self.assertIn('like', self.buckets)
        self.assertIn('resolve', self.buckets)

    def test_tags_are_separate_context_and_do_not_create_action_targets(self):
        context = {'tags': [{'tag_id': 5, 'name': 'kingstonia', 'post_count': 281,
                             'popular_rank': 2, 'observed_at': '2026-10-20T01:00:00Z'}]}
        with patch('ctn_data.generation_topics.tag_context', return_value=context):
            draft = self.decide('post', body='A local view. #kingstonia')
        messages = self.model.call_args.args[2]
        prompt = json.loads(messages[1]['content'])
        self.assertEqual(prompt['tag_context'], context)
        self.assertEqual(draft['tag_context'], context)
        self.assertEqual(prompt['sources'], self.sources)
        self.assertIn('0–2', messages[0]['content'])
        self.assertTrue(all(source.get('kind') != 'tag' for source in draft['sources']))
        self.assertEqual(self.social.writes, [])

    def test_tag_snapshot_alone_never_calls_a_model_or_creates_a_draft(self):
        self.sources = []
        with patch('ctn_data.generation_topics.tag_context') as topics:
            self.controller.run(self.agent_id)
        topics.assert_not_called()
        self.model.assert_not_called()
        self.assertEqual(self.controller.snapshot()['drafts'], [])

    def test_like_accepted_then_timeout_is_uncertain_until_exact_relationship_check(self):
        draft = self.decide()
        self.social.timeout = 'like'
        with self.assertRaises(AgentError):
            self.controller.publish(draft['id'])
        self.assertEqual(self.controller.snapshot()['drafts'][0]['state'], 'uncertain')
        with self.assertRaises(AgentError):
            self.controller.publish(draft['id'])
        self.assertEqual(len(self.social.like_calls), 1)
        self.assertEqual(self.controller.reconcile(draft['id'])['drafts'][0]['state'], 'published')
        self.assertEqual(len(self.social.like_calls), 1)

    def test_repost_accepted_then_timeout_reconciles_mapping_without_resend(self):
        draft = self.decide('repost', body='My own commentary.')
        self.social.timeout = 'repost'
        with self.assertRaises(AgentError):
            self.controller.publish(draft['id'])
        self.assertEqual(self.controller.snapshot()['drafts'][0]['state'], 'uncertain')
        state = self.controller.reconcile(draft['id'])
        self.assertEqual(state['drafts'][0]['state'], 'published')
        self.assertEqual(self.social.writes[0]['body'], 'My own commentary.')
        self.assertEqual(len(self.social.writes), 1)

    def test_existing_remote_like_is_verified_without_mutation(self):
        self.account.likes.add(7)
        result = self.controller.publish(self.decide()['id'])['drafts'][0]
        self.assertTrue(result['result']['already_applied'])
        self.assertEqual(self.social.like_calls, [])

    def test_existing_repost_does_not_claim_a_new_unsubmitted_comment(self):
        old = self.account.repost(7, comment='An earlier remote comment.')
        draft = self.decide('repost', body='A newly suggested comment.')
        result = self.controller.publish(draft['id'])['drafts'][0]
        self.assertEqual(result['state'], 'published')
        self.assertEqual(result['post_id'], old['post_id'])
        self.assertEqual(result['requested_body'], 'A newly suggested comment.')
        self.assertEqual(result['body'], '')
        self.assertTrue(result['result']['already_applied'])
        self.assertEqual(len(self.social.writes), 1)

    def test_reply_preserves_original_thread_root(self):
        draft = self.decide('reply')
        self.controller.publish(draft['id'])
        self.assertEqual(self.social.writes[0]['reply_to_post_id'], 7)
        self.assertEqual(self.social.writes[0]['root_post_id'], 3)

    def test_invalid_target_and_unknown_notification_are_rejected(self):
        for kwargs in ({'target_post_id': 999}, {'notification_ids': [999]}, {'source_ids': ['post:999']}):
            with self.subTest(kwargs=kwargs), self.assertRaises(AgentError):
                self.decide(**kwargs)
        self.assertEqual(self.controller.snapshot()['drafts'], [])
        self.assertEqual(self.account.resolved, [])

    def test_role_allowed_actions_are_enforced_before_outbox(self):
        self.controller.save({'id': self.agent_id, 'name': 'Fictional local', 'allowed_actions': ['post', 'skip']})
        with self.assertRaises(AgentError):
            self.decide('like')
        self.assertEqual(self.controller.snapshot()['drafts'], [])

    def test_review_skip_marks_only_selected_notification_locally(self):
        draft = self.decide('skip', notification_ids=[101])
        inbox = self.controller.snapshot()['agents'][0]['inbox']
        self.assertEqual(draft['state'], 'skipped')
        self.assertEqual(inbox[0]['handled_decision_id'], draft['id'])
        self.assertEqual(inbox[0]['status'], 'unresolved')
        self.assertNotIn('handled_decision_id', inbox[1])
        self.assertEqual(self.account.resolved, [])

    def test_auto_skip_acknowledges_only_selected_notification(self):
        self.controller.save({'id': self.agent_id, 'name': 'Fictional local', 'mode': 'auto', 'enabled': True})
        self.controller.settings({'enabled': True})
        self.decide('skip', notification_ids=[101])
        self.assertEqual(self.account.resolved, [101])
        self.assertEqual(self.controller.snapshot()['agents'][0]['inbox'][1]['status'], 'unresolved')

    def test_successful_actions_feed_persona_memory_and_actual_outcomes_to_next_decision(self):
        self.controller.memory(self.agent_id, 'My independent earlier memory.')
        draft = self.decide()
        context = json.loads(self.model.call_args.args[2][1]['content'])
        self.assertIn('librarian', context['persona']['background'])
        self.assertEqual(context['autobiographical_memory']['subjective_notes']['operator_notes'],
                         'My independent earlier memory.')
        self.assertNotIn('memory', context)
        self.controller.publish(draft['id'])
        self.decide('skip')
        context = json.loads(self.model.call_args.args[2][1]['content'])
        completed = context['autobiographical_memory']['completed_actions']
        self.assertEqual(completed[0]['action'], 'like')
        self.assertEqual(completed[0]['state'], 'published')
        self.assertNotIn('recent_activity', context)
        self.assertNotIn('I considered a recent discussion.', json.dumps(context))
        decisions = self.controller.store.read()['memory'][self.agent_id]['episodes']['decisions']
        self.assertEqual(next(row for row in decisions if row['draft_id'] == draft['id'])['reflection'],
                         'I considered a recent discussion.')

    def test_uncertain_survives_controller_restart_and_cannot_replay(self):
        draft = self.decide()
        self.social.timeout = 'like'
        with self.assertRaises(AgentError):
            self.controller.publish(draft['id'])
        restarted = AgentController(self.controller.db_path, Path(self.temp.name) / 'keys',
            provider_store=self.controller.providers, sdk_factory=self.social.factory, clock=lambda: self.now)
        with self.assertRaises(AgentError):
            restarted.publish(draft['id'])
        self.assertEqual(len(self.social.like_calls), 1)
        self.assertEqual(restarted.reconcile(draft['id'])['drafts'][0]['state'], 'published')

    def test_budget_deferral_keeps_pending_and_does_not_consume_daily_action(self):
        draft = self.decide()
        original = self.call.side_effect
        def defer(controller, bucket, method, *args, **kwargs):
            if bucket == 'like':
                raise Deferred('like', 3)
            return original(controller, bucket, method, *args, **kwargs)
        self.call.side_effect = defer
        with self.assertRaises(AgentError):
            self.controller.publish(draft['id'])
        data = self.controller.store.read()
        self.assertEqual(data['drafts'][draft['id']]['state'], 'pending')
        self.assertEqual(data['rate']['accounts'][self.agent_id].get('attempts', 0), 0)
        self.assertEqual(self.social.like_calls, [])

    def test_failed_ack_does_not_change_primary_success(self):
        draft = self.decide(notification_ids=[101])
        with patch('ctn_data.agent_observation.resolve_selected', return_value=False):
            result = self.controller.publish(draft['id'])['drafts'][0]
        self.assertEqual(result['state'], 'published')
        self.assertFalse(result['notifications_resolved'])
        self.assertEqual(len(self.social.like_calls), 1)

    def test_fair_scheduler_rotates_even_when_prior_role_remains_due(self):
        self.controller.save({'id': self.agent_id, 'name': 'Fictional local', 'enabled': True})
        self.controller.save({'name': 'Second role', 'enabled': True})
        self.controller.settings({'enabled': True})
        with patch.object(self.controller.generations, 'submit') as run, patch('ctn_data.agent_observation.refresh'), patch('ctn_data.agent_observation.drain_resolutions'):
            self.controller.tick()
            self.controller.tick()
        self.assertEqual(run.call_count, 2)
        self.assertNotEqual(run.call_args_list[0].args[0], run.call_args_list[1].args[0])

    def test_due_action_is_sent_before_another_role_starts_deciding(self):
        draft = self.decide()
        self.controller.save({'name': 'Second role', 'enabled': True})
        self.controller.settings({'enabled': True})
        def rotate_past_first(data, conn):
            data['agents'][self.agent_id].update(enabled=True, mode='auto', next_run_at=self.now + 86400)
        self.controller.store.change(rotate_past_first)
        # The rotation would serve the second role next.
        self.controller.store.save_cursors({'scheduler_cursor': self.agent_id})
        with patch.object(self.controller.generations, 'submit') as submit, \
                patch('ctn_data.agent_observation.refresh'), patch('ctn_data.agent_observation.drain_resolutions'):
            self.controller.tick()
        self.assertEqual(self.controller.store.read()['drafts'][draft['id']]['state'], 'published')
        submit.assert_not_called()

    def test_new_notifications_are_polled_before_next_model_interval(self):
        self.controller.save({'id': self.agent_id, 'name': 'Fictional local', 'enabled': True})
        self.controller.settings({'enabled': True})
        self.controller.store.change(lambda data, conn: data['agents'][self.agent_id].update(next_run_at=self.now + 10000))
        with patch('ctn_data.agent_observation.refresh') as refresh, patch('ctn_data.agent_observation.drain_resolutions'):
            self.controller.tick()
        refresh.assert_called_once()
        self.model.assert_not_called()

    def test_decision_consumes_only_selected_sources_and_notifications(self):
        self.decide(notification_ids=[101])
        memory = self.controller.store.read()['memory'][self.agent_id]
        self.assertEqual(set(memory['decision_versions']), {'post:7'})
        self.assertEqual(memory['decision_notification_ids'], [101])
        self.assertNotIn('post:8', memory['decision_versions'])
        self.assertNotIn(102, memory['decision_notification_ids'])

    def test_failed_model_does_not_consume_decision_sources_or_mutate_memory(self):
        self.controller.memory(self.agent_id, 'Keep this memory.')
        self.model.return_value = {'text': 'invalid json'}
        with self.assertRaises(AgentError):
            self.controller.run(self.agent_id)
        memory = self.controller.store.read()['memory'][self.agent_id]
        self.assertNotIn('decision_versions', memory)
        self.assertEqual(memory['summary'], 'Keep this memory.')
        self.assertEqual(self.controller.snapshot()['drafts'], [])

    def test_pause_during_skip_generation_prevents_notification_mutation(self):
        self.controller.save({'id': self.agent_id, 'name': 'Fictional local', 'mode': 'auto', 'enabled': True})
        self.controller.settings({'enabled': True})
        def paused(*args, **kwargs):
            self.controller.settings({'enabled': False})
            return {'text': json.dumps({'action': 'skip', 'body': '', 'notification_ids': [101],
                                       'source_ids': ['post:7'], 'memory': '', 'reason': 'Skip'})}
        self.model.side_effect = paused
        self.controller.run(self.agent_id)
        self.assertEqual(self.account.resolved, [])

    def test_old_saved_profiles_get_same_action_defaults_as_new_profiles(self):
        def old_profile(data, conn):
            for key in ('allowed_actions', 'freshness_minutes', 'news_freshness_hours', 'notification_interval_seconds'):
                data['agents'][self.agent_id].pop(key, None)
        self.controller.store.change(old_profile)
        agent = self.controller.snapshot()['agents'][0]
        self.assertEqual(agent['allowed_actions'], ['post', 'reply', 'repost', 'like', 'unlike', 'follow', 'unfollow', 'skip'])
        self.assertEqual((agent['freshness_minutes'], agent['news_freshness_hours']), (60, 24))

    def test_like_does_not_wait_for_legacy_post_only_interval(self):
        draft = self.decide()
        self.controller.store.change(lambda data, conn: data['rate'].update(next_post_at=self.now + 1000))
        self.assertEqual(self.controller.publish(draft['id'])['drafts'][0]['state'], 'published')

    def test_legacy_daily_quota_and_role_interval_do_not_limit_execution(self):
        draft = self.decide()
        day = datetime.fromtimestamp(self.now, ZoneInfo('Australia/Sydney')).date().isoformat()
        def full_legacy_quota(data, conn):
            data['agents'][self.agent_id].update(daily_limit=1, interval_seconds=86400)
            data['rate'].setdefault('accounts', {})[self.agent_id] = {
                'day': day, 'attempts': 50, 'last_at': self.now}
        self.controller.store.change(full_legacy_quota)
        self.controller.publish(draft['id'])
        data = self.controller.store.read()
        self.assertEqual(data['drafts'][draft['id']]['state'], 'published')
        self.assertEqual(data['rate']['accounts'][self.agent_id]['attempts'], 51)
        self.assertEqual(self.social.like_calls, [(10, 7)])

    def test_scheduler_executes_pending_without_waiting_for_role_or_idle_timer(self):
        draft = self.decide()
        day = datetime.fromtimestamp(self.now, ZoneInfo('Australia/Sydney')).date().isoformat()
        def enable(data, conn):
            data['settings']['enabled'] = True
            data['agents'][self.agent_id].update(enabled=True, mode='auto', daily_limit=1,
                interval_seconds=86400, next_run_at=self.now + 86400)
            data['rate'].setdefault('accounts', {})[self.agent_id] = {
                'day': day, 'attempts': 50, 'last_at': self.now}
        self.controller.store.change(enable)
        with patch('ctn_data.agent_observation.refresh'), patch('ctn_data.agent_observation.drain_resolutions'):
            self.controller.tick()
        self.assertEqual(self.controller.store.read()['drafts'][draft['id']]['state'], 'published')
        self.assertEqual(self.social.like_calls, [(10, 7)])

    def test_new_notification_wakes_early_once_and_preserves_review_draft(self):
        draft = self.decide()
        from ctn_data import agent_observation
        raw = {**self.social.posts[7], 'created_at': agent_observation._at(self.now)}
        source = agent_observation._post(raw, origin='notification', at=agent_observation._at(self.now),
                                         notification_ids=[103])
        def notice(data, conn):
            data['settings']['enabled'] = True
            data['agents'][self.agent_id].update(enabled=True, next_run_at=self.now + 1800)
            memory = data['memory'][self.agent_id]
            memory['decision_versions'] = {'post:7': source['version']}
            memory['decision_notification_ids'] = [101]
            memory['inbox'] = [{'id': 103, 'post_id': 7, 'status': 'unresolved', 'source': source}]
        self.controller.store.change(notice)
        with patch.object(self.controller.generations, 'submit') as submit, \
                patch('ctn_data.agent_observation.refresh'), patch('ctn_data.agent_observation.drain_resolutions'):
            self.controller.tick()
            self.controller.tick()
        submit.assert_called_once_with(self.agent_id, automatic=True)
        self.assertEqual(self.controller.store.read()['drafts'][draft['id']]['state'], 'pending')
        self.assertEqual(self.social.like_calls, [])

    def test_waiting_post_draft_does_not_hold_back_a_ready_like(self):
        post = self.decide('post')
        like = self.decide('like')
        def enable(data, conn):
            data['settings']['enabled'] = True
            data['agents'][self.agent_id].update(enabled=True, mode='auto',
                next_publish_at=self.now + 1800, next_run_at=self.now + 1800)
            data['drafts'][post['id']]['created_at'] = '2026-10-19T00:00:00Z'
        self.controller.store.change(enable)
        original = self.call.side_effect
        def wait_for_post(controller, bucket, method, *args, **kwargs):
            if bucket == 'post':
                raise Deferred('post', 20)
            return original(controller, bucket, method, *args, **kwargs)
        self.call.side_effect = wait_for_post
        with patch('ctn_data.agent_observation.refresh'), patch('ctn_data.agent_observation.drain_resolutions'):
            self.controller.tick()
            self.controller.tick()
        data = self.controller.store.read()
        self.assertEqual(data['drafts'][post['id']]['state'], 'pending')
        self.assertEqual(data['drafts'][post['id']]['next_attempt_at'], self.now + 20)
        self.assertEqual(data['drafts'][like['id']]['state'], 'published')
        self.assertEqual(self.social.like_calls, [(10, 7)])

    def auto(self, **drafts):
        def enable(data, conn):
            data['settings']['enabled'] = True
            data['agents'][self.agent_id].update(enabled=True, mode='auto', next_run_at=self.now + 86400)
            for draft_id, fields in drafts.items():
                data['drafts'][draft_id].update(fields)
        self.controller.store.change(enable)

    def ticks(self, count=1):
        with patch.object(self.controller.generations, 'submit'), \
                patch('ctn_data.agent_observation.refresh'), patch('ctn_data.agent_observation.drain_resolutions'):
            for _ in range(count):
                self.controller.tick()
        return self.controller.store.read()

    def test_reply_to_a_removed_post_is_skipped_and_frees_the_role(self):
        # 22 Oct: three roles retried replies to taken-down bot posts all night.
        reply = self.decide('reply')
        like = self.decide('like', target_post_id=8, source_ids=['post:8'])
        self.auto()
        removed = type('NotFound', (Exception,), {'status': 404})
        original = self.social.get_post
        def get_post(post_id):
            if post_id == 7:
                raise removed('post not found')
            return original(post_id)
        self.social.get_post = get_post
        data = self.ticks(2)
        skipped = data['drafts'][reply['id']]
        self.assertEqual((skipped['state'], skipped['target_unavailable'], skipped['target_status']), ('skipped', True, 404))
        self.assertNotIn('next_attempt_at', skipped)
        self.assertEqual(self.social.writes, [])
        self.assertEqual(data['drafts'][like['id']]['state'], 'published')
        self.assertIn('target_unavailable', [event['kind'] for event in data['events']])

    def test_repeated_failure_waits_longer_and_lets_later_actions_through(self):
        reply = self.decide('reply')
        like = self.decide('like', target_post_id=8, source_ids=['post:8'])
        self.auto()
        original = self.social.get_post
        def get_post(post_id):
            if post_id == 7:
                raise ConnectionError('upstream unavailable')
            return original(post_id)
        self.social.get_post = get_post
        data = self.ticks(2)
        failing = data['drafts'][reply['id']]
        self.assertEqual((failing['state'], failing['auto_failures']), ('pending', 1))
        self.assertEqual(failing['next_attempt_at'], self.now + 60)
        self.assertTrue(failing['error'])
        self.assertEqual(data['drafts'][like['id']]['state'], 'published')
        self.now += 61
        failing = self.ticks()['drafts'][reply['id']]
        self.assertEqual((failing['auto_failures'], failing['next_attempt_at']), (2, self.now + 120))

    def test_automatic_reply_unsent_for_two_hours_is_discarded_not_deleted(self):
        stale = self.decide('reply')['id']
        stamp = lambda seconds: datetime.fromtimestamp(self.now - seconds, timezone.utc).isoformat().replace('+00:00', 'Z')
        def copies(data, conn):
            data['drafts'][stale].update(created_at=stamp(2 * 3600 + 1), next_attempt_at=self.now + 600)
            data['drafts']['held'] = {**data['drafts'][stale], 'id': 'held', 'created_at': stamp(5 * 3600),
                                      'requires_review': True}
            data['drafts']['fresh'] = {**data['drafts'][stale], 'id': 'fresh', 'created_at': stamp(60)}
            # Answering someone who replied to us is worth sending late.
            data['drafts']['answer'] = {**data['drafts'][stale], 'id': 'answer', 'created_at': stamp(5 * 3600),
                                        'notification_ids': [101]}
        self.controller.store.change(copies)
        self.auto()
        data = self.ticks()
        expired = data['drafts'][stale]
        self.assertEqual((expired['state'], expired['expired']), ('discarded', True))
        self.assertNotIn('next_attempt_at', expired)
        self.assertEqual(data['drafts']['held']['state'], 'pending')
        self.assertEqual(data['drafts']['fresh']['state'], 'pending')
        self.assertEqual(data['drafts']['answer']['state'], 'pending')
        self.assertEqual(self.social.writes, [])
        event = next(event for event in data['events'] if event.get('draft_id') == stale and event['kind'] == 'discard')
        self.assertEqual(event['details']['reason'], 'reply_expired')

    def test_official_model_retry_is_not_truncated_or_bypassed_by_new_input(self):
        error = Deferred('llm:fake', 3600)
        self.model.side_effect = error
        with self.assertRaises(AgentError):
            self.controller.run(self.agent_id)
        data = self.controller.store.read()
        self.assertEqual(data['agents'][self.agent_id]['decision_retry_at'], self.now + 3600)
        self.controller.store.change(lambda state, conn: (
            state['settings'].update(enabled=True), state['agents'][self.agent_id].update(enabled=True)))
        with patch.object(self.controller.generations, 'submit') as submit, \
                patch('ctn_data.agent_observation.pending_revision', return_value='new-notice'), \
                patch('ctn_data.agent_observation.refresh'), patch('ctn_data.agent_observation.drain_resolutions'):
            self.controller.tick()
        submit.assert_not_called()

    def test_failed_submission_does_not_consume_a_new_input_wakeup(self):
        def enable(data, conn):
            data['settings']['enabled'] = True
            data['agents'][self.agent_id].update(enabled=True, next_run_at=self.now + 1800)
        self.controller.store.change(enable)
        with patch.object(self.controller.generations, 'submit', side_effect=[AgentError('busy', 409), None]) as submit, \
                patch('ctn_data.agent_observation.pending_revision', return_value='new-notice'), \
                patch('ctn_data.agent_observation.refresh'), patch('ctn_data.agent_observation.drain_resolutions'):
            self.controller.tick()
            self.controller.tick()
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(self.controller.store.read()['agents'][self.agent_id]['decision_wake_revision'], 'new-notice')

    def test_ten_review_drafts_do_not_block_a_new_decision(self):
        first = self.decide()
        def old_drafts(data, conn):
            for number in range(9):
                draft_id = 'pending-' + str(number)
                data['drafts'][draft_id] = {**data['drafts'][first['id']], 'id': draft_id}
        self.controller.store.change(old_drafts)
        self.decide('reply', target_post_id=8, source_ids=['post:8'])
        self.assertEqual(sum(draft['state'] == 'pending' for draft in self.controller.store.read()['drafts'].values()), 11)

    def test_changed_persona_is_rejected_before_any_sdk_preflight(self):
        draft = self.decide()
        self.controller.save({'id': self.agent_id, 'name': 'Changed fictional persona'})
        self.call.reset_mock()
        with self.assertRaises(AgentError):
            self.controller.publish(draft['id'])
        self.call.assert_not_called()

    def test_reconcile_like_requires_original_account_identity(self):
        draft = self.decide()
        self.social.timeout = 'like'
        with self.assertRaises(AgentError):
            self.controller.publish(draft['id'])
        self.controller.store.change(lambda data, conn: data['agents'][self.agent_id].update(account_id=999))
        self.call.reset_mock()
        with self.assertRaises(AgentError):
            self.controller.reconcile(draft['id'])
        self.call.assert_not_called()
        self.assertEqual(self.controller.snapshot()['drafts'][0]['state'], 'uncertain')

    def test_post_and_reply_still_reject_empty_body_in_domain(self):
        for action in ('post', 'reply'):
            with self.subTest(action=action):
                draft = self.decide(action)
                with self.assertRaises(AgentError):
                    self.controller.publish(draft['id'], '')
        self.assertEqual(self.social.writes, [])


if __name__ == '__main__':
    unittest.main()

