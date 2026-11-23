"""One offline loop through real observation, budgets, decisions and delivery."""
import json
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ctn_data import agent_observation
from ctn_data.agent_control import AgentController
from ctn_data.db import connect
from ctn_data.ingest import save_posts
from tests.test_agent_actions import Account, Social


class LoopAccount(Account):
    def notifications(self, **kwargs):
        self.social.check_transaction()
        return [notice for notice in self.social.notices if notice['notification_id'] not in self.resolved]

    def feed_home(self, **kwargs):
        self.social.check_transaction()
        return list(self.social.home)


class LoopSocial(Social):
    def __init__(self):
        super().__init__()
        self.notices, self.home = [], []
        self.controller = None

    def check_transaction(self):
        # Every simulated network step can acquire an independent writer.
        conn = connect(self.controller.db_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.rollback()
        finally:
            conn.close()

    def register(self, username, password, team_code, display_name=None):
        self.check_transaction()
        account = LoopAccount(self, username, len(self.accounts) + 10)
        self.accounts[username] = account
        return account

    def account_from_token(self, token):
        self.check_transaction()
        return super().account_from_token(token)

    def get_post(self, post_id):
        self.check_transaction()
        return super().get_post(post_id)


class AgentLoopTests(unittest.TestCase):
    def test_real_budget_observe_decide_like_ack_loop_is_single_delivery(self):
        now = [1792450000.0]
        def advance(delay):
            now[0] += delay

        with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
            root = Path(temporary)
            social = LoopSocial()
            provider = {'id': 'loop-fake', 'protocol': 'openai_chat', 'model': 'loop-fake',
                        'base_url': 'https://fake.invalid', 'max_output_tokens': 1800}
            controller = AgentController(root / 'loop.db', root / 'keys',
                provider_store=SimpleNamespace(get=lambda _: provider),
                sdk_factory=social.factory, clock=lambda: now[0])
            resources.callback(controller.stop)
            def wait_for_generation():
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    job = controller.store.read()['agents'][agent_id].get('generation', {})
                    if job.get('status') not in ('queued', 'running', 'cancelling'):
                        self.assertEqual(job.get('status'), 'completed', job)
                        return
                    threading.Event().wait(.02)
                self.fail('fake background decision did not finish')
            social.controller = controller
            controller.settings({'team_code': 'synthetic-team-code'})
            controller.save({'name': 'Fictional Librarian', 'username': 'loop_librarian',
                             'background': 'A fictional resident who reads local housing discussions.',
                             'stance': 'Vote Yes', 'interests': ['housing'], 'provider_id': 'loop-fake',
                             'mode': 'auto', 'enabled': True, 'interval_seconds': 1800,
                             'notification_interval_seconds': 30})
            agent_id = controller.snapshot()['agents'][0]['id']
            raw = {'post_id': 7, 'author_id': 99, 'kind': 'post', 'body': 'A new local housing discussion.',
                   'visibility': 'public', 'created_at': agent_observation._at(now[0] - 30)}
            social.posts[7] = raw
            social.home = [raw]
            social.notices = [{'notification_id': 101, 'post_id': 7, 'type': 'mention'}]
            conn = connect(controller.db_path)
            try:
                with conn:
                    save_posts(conn, [raw], 'trending', observed_at=agent_observation._at(now[0]))
            finally:
                conn.close()
            answer = {'text': json.dumps({'action': 'like', 'body': '', 'target_post_id': 7,
                        'source_ids': ['post:7'], 'notification_ids': [101],
                        'memory': 'I endorsed the recent housing discussion after reading the mention.',
                        'reason': 'Relevant to my housing interests.'}), 'model': 'loop-fake', 'usage': {}}
            def model_response(provider, messages, **kwargs):
                prompt = json.loads(messages[1]['content'])
                if 'directory' in prompt:
                    return {'text': json.dumps({'source_ids': [row['id'] for row in prompt['directory']],
                                               'reason': 'Relevant housing material.'})}
                return answer

            # Only the model and SDK are fakes. All budget permits, short waits,
            # observation queries, claims, queue commits and acknowledgements run.
            with patch('ctn_data.agent_budget.time.sleep', side_effect=advance), \
                    patch('ctn_data.ai_providers.complete', side_effect=model_response) as complete:
                controller.register(agent_id)
                controller.settings({'enabled': True})
                controller.tick()
                wait_for_generation()
                state = controller.snapshot()
                self.assertEqual(state['drafts'][0]['action'], 'like')
                self.assertEqual(state['drafts'][0]['state'], 'published')
                self.assertEqual(social.like_calls, [(10, 7)])
                self.assertEqual(social.accounts['loop_librarian'].resolved, [101])
                memory = controller.store.read()['memory'][agent_id]
                self.assertIn('post:7', memory['decision_versions'])
                self.assertEqual(memory['decision_notification_ids'], [101])
                self.assertEqual(memory['inbox'][0]['status'], 'resolved')
                self.assertEqual(memory['notification_outbox']['101']['state'], 'resolved')
                self.assertEqual(memory['activity'][0]['state'], 'published')
                self.assertFalse(memory.get('summary'))
                self.assertIn('housing', memory['episodes']['decisions'][0]['reflection'])
                self.assertEqual(memory['episodes']['readings'][0]['source_id'], 'post:7')
                self.assertEqual(memory['episodes']['outcomes'][0]['state'], 'published')
                view = state['agents'][0]['memory_view']
                self.assertEqual(view['observed_reading'][0]['post_id'], 7)
                self.assertEqual(view['completed_actions'][0]['action'], 'like')
                self.assertEqual(view['pending_intentions'], [])
                prompt = json.loads(complete.call_args.args[1][1]['content'])
                self.assertIn('autobiographical_memory', prompt)
                self.assertIn('pending_intentions', prompt['autobiographical_memory'])
                self.assertEqual(complete.call_count, 2)
                # Unchanged input does not spend another model request.
                now[0] += 26
                controller.tick()
                wait_for_generation()
                self.assertEqual(complete.call_count, 2)
                self.assertEqual(social.like_calls, [(10, 7)])
                self.assertEqual(social.accounts['loop_librarian'].resolved, [101])
                budgets = controller.store.read()['rate']['buckets']
                for bucket in ('auth', 'read', 'notification', 'feed', 'like', 'resolve', 'llm:loop-fake'):
                    matching = [state for key, state in budgets.items()
                                if key == bucket or key.startswith(bucket + ':')]
                    self.assertTrue(matching, bucket)
                    self.assertGreaterEqual(sum(state['granted'] for state in matching), 1)
                self.assertEqual(budgets['like']['granted'], 1)

                # A reply arriving during the 30 minute idle period wakes this
                # actor immediately, then the real outbox delivers it once.
                now[0] += 31
                new_post = {**raw, 'post_id': 8, 'author_id': 98,
                            'body': 'A reply with a new housing question.',
                            'created_at': agent_observation._at(now[0])}
                social.posts[8] = new_post
                social.home = [new_post]
                social.notices.append({'notification_id': 102, 'post_id': 8, 'type': 'reply'})
                answer = {**answer, 'text': json.dumps({
                    'action': 'like', 'body': '', 'target_post_id': 8,
                    'source_ids': ['post:8'], 'notification_ids': [102],
                    'memory': 'I read the new housing question.', 'reason': 'A relevant reply.'})}
                self.assertGreater(controller.store.read()['agents'][agent_id]['next_run_at'], now[0])
                controller.tick()
                wait_for_generation()
                self.assertEqual(complete.call_count, 4)
                self.assertEqual(social.like_calls, [(10, 7), (10, 8)])
                self.assertEqual(social.accounts['loop_librarian'].resolved, [101, 102])


if __name__ == '__main__':
    unittest.main()
