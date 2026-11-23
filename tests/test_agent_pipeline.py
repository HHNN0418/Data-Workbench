"""Staged decision: decide -> write -> check, merged into the ordinary draft flow."""
import json
from pathlib import Path
import random
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ctn_data import agent_pipeline, agent_writing, ai_providers, fact_bank
from ctn_data.agent_control import AgentController, AgentError


class StagedPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fact_bank.invalidate()
        self.addCleanup(fact_bank.invalidate)
        self.db = Path(temporary.name) / 'test.db'
        self.controller = AgentController(self.db, Path(temporary.name) / 'secrets',
            provider_store=SimpleNamespace(get=lambda _: {'id': 'fake', 'protocol': 'openai_chat', 'model': 'fake'}),
            clock=lambda: 1792450000.0)
        self.addCleanup(self.controller.stop)
        self.controller.save({'name': 'Reader', 'username': 'fictional_reader', 'provider_id': 'fake',
                              'stance': 'Supports Yes.', 'voice': 'Warm and plain.'})
        self.agent_id = self.controller.snapshot()['agents'][0]['id']
        self.source = {'id': 'post:7', 'post_id': 7, 'author_id': 99, 'kind': 'post', 'version': 'v1',
                       'text': "I'm not sure. Won't the national ID card track my health?",
                       'notification_ids': [], 'target': {'stance': 'undecided', 'concerns': ['id_privacy'],
                                                          'score': 3, 'notes': []}}
        self.controller.store.change(lambda data, conn: (
            data['agents'][self.agent_id].update(account_id=10, account_state='ready'),
            data['settings'].__setitem__(fact_bank.SETTINGS_KEY, {**fact_bank.DEFAULT_SETTINGS, 'pipeline': 'staged'})))
        patcher = patch.object(self.controller, '_read_sources', return_value=[self.source])
        patcher.start()
        self.addCleanup(patcher.stop)

    def model(self, decision, bodies, verdicts):
        """Answer each stage by its schema, recording the prompts."""
        calls = {'decide': [], 'write': [], 'check': []}
        bodies, verdicts = list(bodies), list(verdicts)

        def answer(controller, provider, messages, response_schema=None, **options):
            if response_schema is agent_pipeline.WRITE_SCHEMA:
                calls['write'].append(messages)
                body = bodies.pop(0)
                if isinstance(body, Exception):
                    raise body
                return {'text': json.dumps({'body': body}), 'usage': {'total_tokens': 5}}
            if response_schema is agent_pipeline.CHECK_SCHEMA:
                calls['check'].append(messages)
                verdict = verdicts.pop(0)
                if isinstance(verdict, Exception):
                    raise verdict
                return {'text': json.dumps(verdict), 'usage': {'total_tokens': 3}}
            calls['decide'].append((messages, response_schema))
            return {'text': json.dumps(decision), 'model': 'fake', 'usage': {'total_tokens': 20}}
        return patch('ctn_data.agent_budget.model_call', side_effect=answer), calls

    def decision(self, **changes):
        return {'action': 'reply', 'target_post_id': 7, 'target_account_id': None, 'source_ids': ['post:7'],
                'notification_ids': [], 'memory': '', 'reason': 'Undecided citizen worried about privacy.',
                'memory_updates': {'viewpoints': [], 'questions': [], 'people': []},
                'concern': 'id_privacy', 'fact_ids': ['certificate_separate', 'id_law_no_health'],
                'angle': 'Explain the certificate is separate from the ID card.', **changes}

    GOOD = {'direction': 'toward_yes', 'concedes_no': False, 'unsupported': [], 'issues': []}

    def test_reply_is_decided_written_checked_and_saved_with_facts(self):
        body = ('Fair worry. The certificate is separate from the new ID card, and the law does not allow '
                'health data on it. That is why I am comfortable voting Yes. What else would you want to know?')
        mock, calls = self.model(self.decision(), [body], [self.GOOD])
        with mock:
            self.controller.run(self.agent_id)
        draft = self.controller.snapshot()['drafts'][0]
        self.assertEqual((draft['action'], draft['body'], draft['target_post_id']), ('reply', body, 7))
        self.assertEqual(draft['pipeline'], 'staged')
        self.assertEqual(draft['fact_ids'], ['certificate_separate', 'id_law_no_health'])
        self.assertEqual(draft['target_author_id'], 99)
        self.assertEqual(draft['usage']['total_tokens'], 28)
        messages, schema = calls['decide'][0]
        self.assertNotIn('body', schema['properties'])
        offered = json.loads(messages[1]['content'])['facts']
        self.assertIn('certificate_separate', [f['id'] for f in offered])
        writer = json.loads(calls['write'][0][1]['content'])
        self.assertEqual(writer['TARGET']['post_id'], 7)
        self.assertEqual([f['id'] for f in writer['FACTS']], ['certificate_separate', 'id_law_no_health'])
        # DO_NOT_CLAIM is part of the shared system prefix now (prompt cache, plan B), not the payload.
        self.assertNotIn('DO_NOT_CLAIM', writer)
        write_system = calls['write'][0][0]['content']
        self.assertTrue(write_system.startswith(agent_pipeline.WRITE_RULES + '\n\nDO_NOT_CLAIM:\n- '))
        # The checker sees every active fact (sorted, at the start of the system message) and the persona.
        check_system = calls['check'][0][0]['content']
        self.assertTrue(check_system.startswith(agent_pipeline.CHECK_FACTS_HEADER))
        self.assertIn('free_vaccine: ', check_system)
        ids = [line.split(':')[0] for line in check_system.split('\n\n')[0].split('\n')[1:]]
        self.assertEqual(ids, sorted(ids))
        checked = json.loads(calls['check'][0][1]['content'])
        self.assertEqual(checked['PERSONA']['stance'], 'Supports Yes.')
        self.assertNotIn('FACTS', checked)

    def test_checker_prefix_is_identical_between_calls(self):
        db = str(self.db)
        first = agent_pipeline.check_messages('A', 'post', [], None, db_path=db, persona={'stance': 'x'})
        second = agent_pipeline.check_messages('B', 'reply', [], 'their post', db_path=db, persona={'stance': 'y'})
        self.assertEqual(first[0]['content'], second[0]['content'])

    def test_checker_sees_the_mentioned_figure_of_a_post(self):
        # D97: the writer is told who the figure is; the checker must not call that unsupported.
        db = str(self.db)
        figure = {'username': 'DrAmaraSingh', 'who': 'Dr Amara Singh, GP', 'how': 'responsible for hospital capacity'}
        post = agent_pipeline.check_messages('A', 'post', [], None, db_path=db, figure=figure)
        reply = agent_pipeline.check_messages('B', 'reply', [], 'their post', db_path=db, figure=figure)
        self.assertEqual(json.loads(post[1]['content'])['MENTIONED_FIGURE'], figure)
        self.assertNotIn('MENTIONED_FIGURE', json.loads(reply[1]['content']))
        self.assertIn('MENTIONED_FIGURE (who that public figure is', post[0]['content'])
        self.assertEqual(post[0]['content'], reply[0]['content'])

    def test_checker_noise_is_not_an_unsupported_claim(self):
        verdict = {'direction': 'toward_yes', 'concedes_no': False, 'unsupported': ['', 'n/a'], 'issues': ['x']}
        self.assertEqual(agent_pipeline.check_issues(verdict), [])
        verdict['unsupported'] = ['Nobody has been named to run the checks yet.']
        self.assertEqual(len(agent_pipeline.check_issues(verdict)), 2)

    def test_failed_direction_check_rewrites_once_with_feedback(self):
        hedged = 'I would only vote Yes if they publish the venue list first.'
        better = 'The certificate is separate from the ID card and the law bars health data on it. I am voting Yes.'
        mock, calls = self.model(self.decision(), [hedged, better], [self.GOOD])
        with mock:
            self.controller.run(self.agent_id)
        draft = self.controller.snapshot()['drafts'][0]
        self.assertEqual(draft['body'], better)
        self.assertEqual(draft['rewrites'], 1)
        retry = json.loads(calls['write'][1][1]['content'])
        self.assertTrue(any('conditional' in issue for issue in retry['FIX_THESE_ISSUES']))
        # The hedged text never reached the checker.
        self.assertEqual(len(calls['check']), 1)

    def test_a_rewrite_repeats_the_first_prompt_and_adds_only_the_issues(self):
        hedged = 'I would only vote Yes if they publish the venue list first.'
        better = 'The certificate is separate from the ID card and the law bars health data on it. I am voting Yes.'
        mock, calls = self.model(self.decision(), [hedged, better], [self.GOOD])
        with mock:
            self.controller.run(self.agent_id)
        first, second = calls['write']
        self.assertEqual(first[0], second[0])  # the same writing guide
        retry = json.loads(second[1]['content'])
        self.assertEqual(list(retry)[-1], 'FIX_THESE_ISSUES')
        # The prompt cache matches on the prefix: the rewrite starts with the whole first request.
        self.assertTrue(second[1]['content'].startswith(first[1]['content'][:-1]))

    def test_each_draft_still_draws_its_own_writing_guide(self):
        mock, calls = self.model(self.decision(), ['The certificate is separate from the ID card.'], [self.GOOD])
        with mock, patch('ctn_data.agent_control.random.getrandbits', return_value=1) as seed:
            self.controller.run(self.agent_id)
        seed.assert_called_once_with(64)  # one fresh seed per draft, from the shared generator
        template, drawn = agent_writing.reply_guide_parts(random.Random(1))
        self.assertIn(template, calls['write'][0][0]['content'])
        self.assertIn(drawn, calls['write'][0][0]['content'])
        # Another draft gets another seed, and with it another guide.
        self.assertNotEqual(agent_writing.reply_guide(random.Random(1)), agent_writing.reply_guide(random.Random(2)))

    def test_two_failed_checks_create_no_action(self):
        bad = {'direction': 'neutral', 'concedes_no': True, 'unsupported': [], 'issues': []}
        mock, _ = self.model(self.decision(), ['Text one about the vote.', 'Text two about the vote.'], [bad, bad])
        with mock, self.assertRaises(Exception):
            self.controller.run(self.agent_id)
        self.assertEqual(self.controller.snapshot()['drafts'], [])

    def test_checker_outage_does_not_discard_the_action(self):
        from ctn_data import ai_providers
        mock, _ = self.model(self.decision(), ['The certificate is separate from the ID card. I am voting Yes.'],
                             [ai_providers.ProviderError('down', 502)])
        with mock:
            self.controller.run(self.agent_id)
        draft = self.controller.snapshot()['drafts'][0]
        self.assertEqual(draft['content_check'], {'status': 'unavailable'})

    def test_advisory_notes_do_not_fail_a_passing_text(self):
        verdict = {**self.GOOD, 'issues': ['Could mention the free vaccine too.']}
        mock, calls = self.model(self.decision(), ['The certificate is separate from the ID card. I am voting Yes.'],
                                 [verdict])
        with mock:
            self.controller.run(self.agent_id)
        draft = self.controller.snapshot()['drafts'][0]
        self.assertEqual(draft['rewrites'], 0)
        self.assertEqual(len(calls['write']), 1)

    def test_length_cut_decision_is_retried_once_concisely(self):
        from ctn_data import ai_providers
        mock, calls = self.model(self.decision(action='like', concern='none', fact_ids=[], angle=''), [], [])
        cut = ai_providers.ProviderError('cut', 502, diagnostics={'error_code': 'output_truncated'})
        with mock as model:
            answer, first = model.side_effect, [True]

            def cut_first(*args, **kwargs):
                if first:
                    first.clear()
                    raise cut
                return answer(*args, **kwargs)
            model.side_effect = cut_first
            self.controller.run(self.agent_id)
        self.assertEqual(self.controller.snapshot()['drafts'][0]['action'], 'like')
        system = calls['decide'][0][0][0]['content']
        self.assertIn('single retry after the service cut', system)

    def test_like_needs_no_writer_or_checker(self):
        mock, calls = self.model(self.decision(action='like', concern='none', fact_ids=[], angle=''), [], [])
        with mock:
            self.controller.run(self.agent_id)
        draft = self.controller.snapshot()['drafts'][0]
        self.assertEqual((draft['action'], draft['body']), ('like', ''))
        self.assertEqual((len(calls['write']), len(calls['check'])), (0, 0))

    def test_single_pipeline_keeps_the_original_one_call(self):
        self.controller.store.change(lambda data, conn: data['settings'][fact_bank.SETTINGS_KEY].update(pipeline='single'))
        output = {'action': 'like', 'body': '', 'target_post_id': 7, 'target_account_id': None, 'memory': '',
                  'reason': 'ok', 'source_ids': [], 'notification_ids': []}
        with patch('ctn_data.agent_budget.model_call', return_value={'text': json.dumps(output)}) as call:
            self.controller.run(self.agent_id)
        call.assert_called_once()
        self.assertNotIn('pipeline', self.controller.snapshot()['drafts'][0])


class AllowedActionsOnlyTests(StagedPipelineTests):
    """With post and reply only, no prompt or schema mentions another action."""

    def restrict(self, actions):
        self.controller.store.change(lambda data, conn: data['agents'][self.agent_id].update(allowed_actions=actions))

    def test_staged_prompt_and_schema_offer_only_allowed_actions(self):
        self.restrict(['post', 'reply'])
        mock, calls = self.model(self.decision(), ['A body about the separate certificate.'], [self.GOOD])
        with mock:
            self.controller.run(self.agent_id)
        messages, schema = calls['decide'][0]
        system = messages[0]['content']
        for word in ('like', 'repost', 'follow', 'skip'):
            self.assertNotRegex(system, rf'\b{word}\b', word)
        self.assertIn('allowed_actions (post, reply)', system)
        self.assertEqual(schema['properties']['action']['enum'], ['post', 'reply'])
        self.assertEqual(schema['properties']['target_account_id']['enum'], [None])
        competition = json.loads(messages[1]['content']).get('competition_feedback') or {}
        self.assertNotIn('like', competition.get('objective', ''))

    def test_single_prompt_offers_only_allowed_actions(self):
        self.restrict(['post', 'reply'])
        self.controller.store.change(lambda data, conn: data['settings'][fact_bank.SETTINGS_KEY].update(pipeline='single'))
        output = {'action': 'post', 'body': 'An original post.', 'target_post_id': None, 'target_account_id': None,
                  'memory': '', 'reason': 'ok', 'source_ids': [], 'notification_ids': []}
        with patch('ctn_data.agent_budget.model_call', return_value={'text': json.dumps(output)}) as call:
            self.controller.run(self.agent_id)
        system = call.call_args.args[2][0]['content']
        for word in ('like', 'unlike', 'repost', 'follow', 'unfollow', 'skip'):
            self.assertNotRegex(system, rf'\b{word}\b', word)
        for word in ('点赞', '转发', '取消关注'):
            self.assertNotIn(word, system, word)
        self.assertIn('{"action":"post|reply"', system)
        description = json.dumps(call.call_args.kwargs['response_schema'], ensure_ascii=False)
        for word in ('like', 'repost', 'follow', 'skip'):
            self.assertNotRegex(description, rf'\b{word}\b', word)

    def test_reply_to_a_bot_like_author_is_not_offered_or_saved(self):
        self.restrict(['post', 'reply'])
        self.source['notification_ids'] = [31]
        mock, calls = self.model(self.decision(), ['A body.'], [self.GOOD])
        from ctn_data.agent_control import AgentError
        with mock, patch('ctn_data.agent_targeting.behavior_bots', return_value=frozenset({99})), \
                self.assertRaises(AgentError):
            self.controller.run(self.agent_id)
        _, schema = calls['decide'][0]
        self.assertEqual(schema['properties']['target_post_id']['enum'], [None])
        self.assertEqual(schema['properties']['action']['enum'], ['post'])
        self.assertEqual(self.controller.snapshot()['drafts'], [])
        self.assertIn('target_blocked', calls['decide'][0][0][1]['content'])
        # The failed round still records the blocked source, so it stops taking reading slots.
        memory = self.controller.store.read()['memory'][self.agent_id]
        self.assertEqual(memory['decision_versions']['post:7'], 'v1')
        self.assertIn(31, memory['decision_notification_ids'])

    def decide_sequence(self, decisions):
        """Like model(), but each decide call returns the next decision."""
        mock, calls = self.model(None, ['A body about the separate certificate.'], [self.GOOD])
        queue = list(decisions)
        original = mock.kwargs['side_effect']

        def answer(controller, provider, messages, response_schema=None, **options):
            if response_schema in (agent_pipeline.WRITE_SCHEMA, agent_pipeline.CHECK_SCHEMA):
                return original(controller, provider, messages, response_schema, **options)
            calls['decide'].append((messages, response_schema))
            return {'text': json.dumps(queue.pop(0)), 'model': 'fake', 'usage': {'total_tokens': 20}}
        return patch('ctn_data.agent_budget.model_call', side_effect=answer), calls

    def test_invalid_target_retries_only_the_decision(self):
        self.restrict(['post', 'reply'])
        mock, calls = self.decide_sequence([self.decision(target_post_id=999), self.decision()])
        with mock:
            self.controller.run(self.agent_id)
        self.assertEqual((len(calls['decide']), len(calls['write']), len(calls['check'])), (2, 1, 1))
        self.assertIn('previous choice was invalid', calls['decide'][1][0][0]['content'])
        self.assertEqual(self.controller.snapshot()['drafts'][0]['action'], 'reply')

    def test_two_invalid_targets_stop_before_writing(self):
        self.restrict(['post', 'reply'])
        mock, calls = self.decide_sequence([self.decision(target_post_id=999)] * 2)
        from ctn_data.agent_control import AgentError
        with mock, self.assertRaises(AgentError):
            self.controller.run(self.agent_id)
        self.assertEqual((len(calls['decide']), len(calls['write']), len(calls['check'])), (2, 0, 0))

    def test_no_effective_action_makes_no_model_call(self):
        self.restrict(['reply'])
        with patch('ctn_data.agent_budget.model_call') as call, \
                patch('ctn_data.agent_targeting.behavior_bots', return_value=frozenset({99})):
            self.controller.run(self.agent_id)
        call.assert_not_called()
        self.assertEqual(self.controller.snapshot()['drafts'], [])
        events = self.controller.store.read()['events']
        self.assertIn('没有可互动的对象', events[-1]['message'])

    def test_decision_problem(self):
        agent = {'account_id': 10, 'allowed_actions': ['post', 'reply']}
        sources = [{'id': 'post:7', 'post_id': 7, 'author_id': 99},
                   {'id': 'post:8', 'post_id': 8, 'author_id': 98, 'target_blocked': 'thread_too_deep'}]
        problem = lambda **d: agent_pipeline.decision_problem(agent, d, sources, {})
        self.assertIsNone(problem(action='reply', target_post_id=7, target_account_id=None))
        self.assertIsNone(problem(action='post', target_post_id=None, target_account_id=None))
        self.assertIn('not an offered target', problem(action='reply', target_post_id=8, target_account_id=None))
        self.assertIn('not in allowed_actions', problem(action='like', target_post_id=7, target_account_id=None))
        self.assertIn('must be null', problem(action='post', target_post_id=None, target_account_id=99))

    def test_automatic_reply_waits_a_random_delay_but_manual_run_does_not(self):
        self.restrict(['post', 'reply'])
        self.controller.store.change(lambda data, conn: (
            data['agents'][self.agent_id].update(mode='auto', enabled=True),
            data['settings'].update(enabled=True)))
        mock, _ = self.model(self.decision(), ['A body about the separate certificate.'], [self.GOOD])
        with mock, patch('ctn_data.agent_control.random.uniform', return_value=120.0), \
                patch.object(self.controller, '_publish') as publish:
            self.controller._run(self.agent_id, automatic=True)
        draft = self.controller.store.read()['drafts']
        (only,) = draft.values()
        self.assertEqual(only['next_attempt_at'], 1792450000.0 + 120.0)
        publish.assert_not_called()

    def aged(self, seconds):
        """Give the reading's one source a creation time ``seconds`` before the controller clock."""
        from datetime import datetime, timezone
        self.source['created_at'] = datetime.fromtimestamp(1792450000.0 - seconds, timezone.utc).isoformat()

    def test_a_reply_in_the_last_two_writes_makes_the_turn_an_original(self):
        self.restrict(['post', 'reply'])
        self.history(['post', 'reply'])
        self.aged(120)
        post = self.decision(action='post', target_post_id=None, source_ids=['post:7'])
        mock, calls = self.model(post, ['An original post about the separate certificate.'], [self.GOOD])
        with mock, patch('ctn_data.agent_targeting.figure_mention', return_value=None):
            self.controller.run(self.agent_id)
        _, schema = calls['decide'][0]
        self.assertEqual(schema['properties']['action']['enum'], ['post'])
        draft = next(d for d in self.controller.store.read()['drafts'].values() if not d['id'].startswith('old'))
        self.assertEqual((draft['action'], draft.get('mix_forced')), ('post', 'post'))

    def history(self, actions):
        self.controller.store.change(lambda data, conn: data['drafts'].update({
            f'old{i}': {'id': f'old{i}', 'agent_id': self.agent_id, 'action': action, 'state': 'published',
                        'created_at': f'2026-10-21T10:0{i}:00Z'} for i, action in enumerate(actions)}))

    def test_after_two_originals_a_fresh_citizen_post_gets_the_reply(self):
        self.restrict(['post', 'reply'])
        self.history(['reply', 'post', 'post'])
        self.aged(120)
        mock, calls = self.model(self.decision(), ['A reply about the separate certificate.'], [self.GOOD])
        with mock:
            self.controller.run(self.agent_id)
        self.assertEqual(calls['decide'][0][1]['properties']['action']['enum'], ['reply'])
        draft = next(d for d in self.controller.store.read()['drafts'].values() if not d['id'].startswith('old'))
        self.assertEqual((draft['action'], draft.get('mix_forced')), ('reply', 'reply'))

    def test_reply_turn_without_a_valid_target_posts_instead(self):
        self.restrict(['post', 'reply'])
        self.history(['post', 'post'])
        self.aged(120)
        post = self.decision(action='post', target_post_id=None, source_ids=['post:7'])
        mock, calls = self.model(post, ['An original post about 30 ICU beds.'], [self.GOOD])
        with mock, patch('ctn_data.agent_targeting.behavior_bots', return_value=frozenset({99})), \
                patch('ctn_data.agent_targeting.figure_mention', return_value=None):
            self.controller.run(self.agent_id)
        self.assertEqual(calls['decide'][0][1]['properties']['action']['enum'], ['post'])

    def test_original_posts_mention_a_public_figure_and_replies_do_not(self):
        post = self.decision(action='post', target_post_id=None, source_ids=['post:7'], concern='hospital')
        mock, calls = self.model(post, ['@DrAmaraSingh, thank you: our 30 ICU beds need protecting.'], [self.GOOD])
        with mock:
            self.controller.run(self.agent_id)
        draft = self.controller.snapshot()['drafts'][0]
        self.assertEqual(draft.get('mention_figure'), 'DrAmaraSingh')
        self.assertNotIn('mention_figure', draft.get('reason', ''))
        writer = json.loads(calls['write'][0][1]['content'])
        self.assertEqual(writer['MENTION_FIGURE']['username'], 'DrAmaraSingh')
        self.assertEqual(json.loads(calls['check'][0][1]['content'])['MENTIONED_FIGURE'], writer['MENTION_FIGURE'])
        mock, _ = self.model(self.decision(), ['A reply.'], [self.GOOD])
        with mock, patch('ctn_data.agent_targeting.figure_mention') as found:
            self.controller.run(self.agent_id)
        found.assert_not_called()

    def test_the_fact_cap_also_counts_the_store_drafts(self):
        # post_attribution lags about 10 minutes; the decision hands the store's drafts to the cap.
        mock, _ = self.model(self.decision(), ['A reply.'], [self.GOOD])
        with mock, patch('ctn_data.agent_pipeline.offered_facts', wraps=agent_pipeline.offered_facts) as offered:
            self.controller.run(self.agent_id)
        self.assertIsInstance(offered.call_args.kwargs['drafts'], dict)

    def test_a_pasted_fact_is_retried_once_with_a_different_fact(self):
        pasted = {'id': 'free_vaccine', 'claim': 'The vaccine is free at hospitals, GP clinics and pop-up sites.',
                  'source': 'Health ministry', 'concerns': ['cost']}
        spare = {'id': 'icu_beds', 'claim': 'Kingstonia has about 30 ICU beds across its two hospitals.',
                 'source': 'Health ministry', 'concerns': ['hospital']}
        copy = 'Good to know: the vaccine is free at hospitals, GP clinics and pop-up sites. Voting Yes.'
        mock, calls = self.model(self.decision(fact_ids=['free_vaccine']),
                                 [copy, copy, 'With about 30 ICU beds for two hospitals, I am voting Yes.'], [self.GOOD])
        with mock, patch('ctn_data.agent_pipeline.offered_facts', return_value=[pasted, spare]):
            self.controller.run(self.agent_id)
        self.assertEqual(len(calls['write']), 3)
        # The rewrite is told which text it copied.
        self.assertEqual(json.loads(calls['write'][1][1]['content'])['FIX_THESE_ISSUES'], [
            agent_pipeline.VERBATIM_ISSUE + ' Copied text: "vaccine is free at hospitals, GP clinics and pop-up '
            'sites." (fact free_vaccine).'])
        self.assertEqual([f['id'] for f in json.loads(calls['write'][2][1]['content'])['FACTS']], ['icu_beds'])
        draft = self.controller.snapshot()['drafts'][0]
        self.assertEqual((draft['action'], draft.get('fact_ids'), draft.get('rewrites')), ('reply', ['icu_beds'], 2))

    def test_a_repeated_phrase_is_quoted_for_the_rewrite_but_not_in_the_failure(self):
        self.controller.store.change(lambda data, conn: data['drafts'].update({'old0': {
            'id': 'old0', 'agent_id': self.agent_id, 'action': 'reply', 'state': 'published',
            'body': 'I run a café near the wharf and I watched the queue grow.', 'created_at': '2026-10-21T10:00:00Z'}}))
        repeat = ('The certificate is separate from the ID card, and the law keeps health data off it. '
                  'I run a café near the wharf and I watched it fill.')
        mock, calls = self.model(self.decision(), [repeat, repeat], [])
        with mock, self.assertRaises(AgentError) as raised:
            self.controller.run(self.agent_id)
        self.assertEqual(json.loads(calls['write'][1][1]['content'])['FIX_THESE_ISSUES'], [
            agent_pipeline.PERSONAL_ISSUE + ' Already in your recent posts, do not reuse: '
            '"I run a café near the wharf and I watched".'])
        # Failure messages stay one plain line per issue, so the failure page still groups them.
        self.assertTrue(str(raised.exception).endswith('：' + agent_pipeline.PERSONAL_ISSUE), str(raised.exception))
        # The rejected text and the quoted issue travel with the error to the failure event.
        self.assertEqual(raised.exception.rejected, {'action': 'reply', 'body': repeat, 'issues': [
            agent_pipeline.PERSONAL_ISSUE + ' Already in your recent posts, do not reuse: '
            '"I run a café near the wharf and I watched".']})

    @staticmethod
    def reasoning_only(finish='stop'):
        return ai_providers.ProviderError('AI 服务只返回了推理过程，尚无最终回答；请检查服务商模型限制', 502, diagnostics={
            'error_code': 'reasoning_only', 'finish_reason': finish, 'has_reasoning': True, 'answer_chars': 0})

    def test_a_reasoning_only_answer_is_requested_once_more_unchanged(self):
        good = 'The certificate is separate from the ID card and the law bars health data on it.'
        mock, calls = self.model(self.decision(), [self.reasoning_only(), good], [self.GOOD])
        with mock:
            self.controller.run(self.agent_id)
        self.assertEqual(len(calls['write']), 2)
        self.assertEqual(calls['write'][0], calls['write'][1])
        draft = self.controller.snapshot()['drafts'][0]
        self.assertEqual((draft['body'], draft['rewrites']), (good, 0))

    def test_a_second_reasoning_only_answer_fails_as_before_and_other_errors_are_not_retried(self):
        mock, calls = self.model(self.decision(), [self.reasoning_only(), self.reasoning_only()], [])
        with mock, self.assertRaisesRegex(AgentError, '^AI 服务只返回了推理过程，尚无最终回答；请检查服务商模型限制$'):
            self.controller.run(self.agent_id)
        self.assertEqual(len(calls['write']), 2)
        for error in (self.reasoning_only(finish='content_filter'),
                      ai_providers.ProviderError('AI 服务未返回最终文字回答，请检查模型和接口协议设置', 502)):
            with self.subTest(error=error.diagnostics):
                mock, calls = self.model(self.decision(), [error], [])
                with mock, self.assertRaises(AgentError):
                    self.controller.run(self.agent_id)
                self.assertEqual(len(calls['write']), 1)

    def test_other_issues_still_end_the_run_after_two_writes(self):
        mock, calls = self.model(self.decision(), ['我支持 Yes', '我支持 Yes'], [])
        with mock, self.assertRaises(AgentError):
            self.controller.run(self.agent_id)
        self.assertEqual(len(calls['write']), 2)
        self.assertFalse([d for d in self.controller.snapshot()['drafts'] if d['action'] == 'reply'])

    def test_overlong_reason_is_truncated_not_rejected(self):
        mock, _ = self.model(self.decision(reason='r' * 900), ['A body about the separate certificate.'], [self.GOOD])
        with mock:
            self.controller.run(self.agent_id)
        draft = self.controller.snapshot()['drafts'][0]
        self.assertEqual(len(draft['reason']), 500)

    def test_mark_decided_merges_without_touching_other_memory(self):
        self.controller.store.change(lambda data, conn: data['memory'].setdefault(self.agent_id, {}).update(
            decision_versions={'post:1': 'a'}, decision_notification_ids=[1], inbox=[{'id': 9}]))
        self.controller.store.mark_decided(self.agent_id, {'post:2': 'b'}, [2, 1])
        memory = self.controller.store.read()['memory'][self.agent_id]
        self.assertEqual(memory['decision_versions'], {'post:1': 'a', 'post:2': 'b'})
        self.assertEqual(memory['decision_notification_ids'], [1, 2])
        self.assertEqual(memory['inbox'], [{'id': 9}])


class FixRegressionTests(unittest.TestCase):
    def test_liked_posts_are_offered_only_to_roles_that_may_unlike(self):
        from ctn_data import agent_decision
        sources = [{'id': 'post:1', 'post_id': 1, 'author_id': 5}]
        context = {'relations': [{'kind': 'like', 'present': True, 'target_post_id': 45}]}
        without = agent_decision.response_schema({'allowed_actions': ['reply', 'like']}, sources, context)
        self.assertEqual(without['properties']['target_post_id']['enum'], [None, 1])
        allowed = agent_decision.response_schema({'allowed_actions': ['reply', 'unlike']}, sources, context)
        self.assertEqual(allowed['properties']['target_post_id']['enum'], [None, 1, 45])
        self.assertIn('valid only for unlike', allowed['properties']['target_post_id']['description'])

    def test_actions_without_a_valid_target_are_not_offered(self):
        from ctn_data import agent_decision
        agent = {'account_id': 10, 'allowed_actions': ['post', 'reply', 'like', 'follow', 'unlike']}
        open_post = {'id': 'post:1', 'post_id': 1, 'author_id': 5}
        blocked = {'id': 'post:2', 'post_id': 2, 'author_id': 6, 'target_blocked': 'bot_like_author'}
        self.assertEqual(agent_decision.effective_actions(agent, [open_post]),
                         ['post', 'reply', 'like', 'follow', 'unlike'])
        # A blocked post still names its author, so follow stays; post targets do not.
        self.assertEqual(agent_decision.effective_actions(agent, [blocked]), ['post', 'follow'])
        self.assertEqual(agent_decision.effective_actions(agent, []), ['post'])
        liked = {'relations': [{'kind': 'like', 'present': True, 'target_post_id': 45}]}
        self.assertEqual(agent_decision.effective_actions(agent, [], liked), ['post', 'unlike'])
        self.assertEqual(agent_decision.effective_actions({'allowed_actions': ['reply']}, [blocked]), [])
        schema = agent_decision.response_schema({'allowed_actions': ['post', 'reply']}, [blocked])
        self.assertEqual(schema['properties']['action']['enum'], ['post'])
        # Nothing effective: the schema keeps the allowed list; the caller decides not to call.
        schema = agent_decision.response_schema({'allowed_actions': ['reply']}, [blocked])
        self.assertEqual(schema['properties']['action']['enum'], ['reply'])

    def test_next_mix_answers_citizens_and_otherwise_writes_two_originals_per_reply(self):
        from ctn_data import agent_decision
        now = 1792450000.0
        def post(post_id, age=None, **extra):
            from datetime import datetime, timezone
            created = datetime.fromtimestamp(now - age, timezone.utc).isoformat() if age is not None else None
            return {'id': f'post:{post_id}', 'post_id': post_id, 'kind': 'post', 'created_at': created, **extra}
        def drafts(actions):
            return {str(i): {'id': str(i), 'agent_id': 'a', 'action': a, 'state': 'published',
                             'created_at': f'2026-10-21T10:{i:02d}:00Z'} for i, a in enumerate(actions)}
        mix = lambda sources, actions: agent_decision.next_mix(sources, drafts(actions), 'a', now)
        fresh, stale = post(2, 300), post(1, 3600)
        # A citizen answering us always gets the reply, whatever the history.
        self.assertEqual(mix([post(1, 3600, notification_ids=[5])], ['reply', 'reply']), 'reply')
        # Otherwise a reply in the last two writes means an original.
        self.assertEqual(mix([fresh], ['post', 'reply']), 'post')
        self.assertEqual(mix([fresh], ['reply', 'post']), 'post')
        # After two originals the reply goes to a precise target: a fresh post or a queue pick.
        self.assertEqual(mix([stale, fresh], ['post', 'post']), 'reply')
        self.assertEqual(mix([post(1, 3600, opportunity={'score': 4})], ['post', 'post']), 'reply')
        self.assertEqual(mix([stale], ['post', 'post']), 'post')
        # Blocked, policy-blocked and context-only sources are never targets.
        for extra in ({'target_blocked': 'not_a_citizen'}, {'interaction_blocked': True}, {'context_only': True}):
            self.assertEqual(mix([post(1, 60, notification_ids=[5], **extra)], ['post', 'post']), 'post', extra)
        # With too little history the model chooses; other roles and discarded drafts do not count.
        self.assertIsNone(mix([fresh], ['post']))
        rows = drafts(['post', 'post'])
        rows['x'] = {'id': 'x', 'agent_id': 'b', 'action': 'reply', 'state': 'published', 'created_at': 'z'}
        rows['y'] = {'id': 'y', 'agent_id': 'a', 'action': 'reply', 'state': 'discarded', 'created_at': 'z'}
        self.assertEqual(agent_decision.next_mix([fresh], rows, 'a', now), 'reply')

    def test_writer_sees_our_earlier_answers_to_the_same_person(self):
        drafts = {str(i): {'state': 'published', 'target_author_id': 99, 'body': f'answer {i}', 'concern': 'cost',
                           'fact_ids': ['free_vaccine'], 'published_at': f'2026-10-21T0{i}:00:00Z'} for i in range(5)}
        drafts['other'] = {'state': 'published', 'target_author_id': 7, 'body': 'x', 'published_at': '2026-10-21T09:00:00Z'}
        history = agent_pipeline.history_with(drafts, 99)
        self.assertEqual([row['body'] for row in history], ['answer 2', 'answer 3', 'answer 4'])
        self.assertEqual(agent_pipeline.history_with(drafts, None), [])


class LocalChecksTests(unittest.TestCase):
    def test_templates_hand_out_no_stock_lines(self):
        # 23 Oct: "Name the cost they are afraid of" and the quoted openers came back as stock lines.
        for guide in (agent_writing.REPLY_GUIDE, agent_writing.POST_GUIDE):
            self.assertNotIn('Name the cost', guide)
        self.assertNotIn('("The question I keep hearing is who pays', agent_writing.POST_GUIDE)
        self.assertIn('the cost you\'re afraid of', agent_writing.REPLY_GUIDE)  # named as a line not to use

    def test_local_issues(self):
        self.assertEqual(agent_pipeline.local_issues('Free jab, phased in. Voting Yes.', 'post', []), [])
        self.assertTrue(agent_pipeline.local_issues('x' * 501, 'post', []))
        self.assertEqual(agent_pipeline.local_issues('x' * 500, 'post', []), [])
        self.assertIn('at most {limit} characters', agent_pipeline.WRITE_SYSTEM)
        self.assertEqual((agent_pipeline.WRITE_TARGET, agent_pipeline.REPLY_WRITE_TARGET), (480, 320))
        self.assertEqual(agent_pipeline.WRITE_SCHEMA['properties']['body']['maxLength'], 500)
        self.assertTrue(agent_pipeline.local_issues('我支持 Yes', 'post', []))
        self.assertTrue(agent_pipeline.local_issues('@a @b @c @d hi', 'reply', []))
        self.assertTrue(agent_pipeline.local_issues("I'll reconsider my vote if they don't publish it.", 'post', []))
        self.assertTrue(agent_pipeline.local_issues('Same words here today.', 'post', ['Same words here today!']))

    def test_local_issues_block_concessions_venue_lists_and_pasted_facts(self):
        for body in ("You're right that it's rushed, but the jab is free.", 'Fair point. Still voting Yes.',
                     "Honest answer: I don't have the name of the body that signs it off.",
                     "I haven't seen the list either.", "I won't pretend it doesn't say that.",
                     'The ballot does name schools and public transport.',
                     'It covers shops, schools, workplaces and parks.'):
            self.assertTrue(agent_pipeline.local_issues(body, 'reply', []), body)
        # Acknowledging the worry and ordinary persona detail are fine.
        for body in ('I hear you, the school run matters. Nothing starts until every child is eligible.',
                     "I don't have kids, but my choir does, and I'm voting Yes."):
            self.assertEqual(agent_pipeline.local_issues(body, 'reply', []), [], body)
        fact = {'id': 'free_vaccine', 'claim': 'The vaccine is free at hospitals, GP clinics and pop-up sites.'}
        pasted = 'Good to know: the vaccine is free at hospitals, GP clinics and pop-up sites. Voting Yes.'
        reworded = 'You can get the jab for nothing at any hospital or GP, says the health ministry. Voting Yes.'
        self.assertTrue(agent_pipeline.local_issues(pasted, 'reply', [], [fact]))
        self.assertEqual(agent_pipeline.local_issues(reworded, 'reply', [], [fact]), [])

    def test_replies_carry_no_mentions_and_posts_at_most_one(self):
        self.assertTrue(agent_pipeline.local_issues('@maya the jab is free. Voting Yes.', 'reply', []))
        self.assertEqual(agent_pipeline.local_issues('@DrAmaraSingh the jab is free. Voting Yes.', 'post', []), [])
        self.assertTrue(agent_pipeline.local_issues('@maya @ethan the jab is free.', 'post', []))

    def test_checker_notes_about_concessions_fail_the_text(self):
        verdict = {'direction': 'toward_yes', 'concedes_no': False, 'unsupported': [],
                   'issues': ['Leaves the accountability question unanswered.', 'Could be warmer.']}
        # The promoted note fails the text; the remaining note rides along as a rewrite hint.
        self.assertEqual(agent_pipeline.check_issues(verdict),
                         ['Leaves the accountability question unanswered.', 'Could be warmer.'])
        verdict['issues'] = ['Nicely avoids the No side framing.', 'Could be warmer.']
        self.assertEqual(agent_pipeline.check_issues(verdict), [])

    def test_negated_concession_notes_are_praise_not_failures(self):
        passing = {'direction': 'toward_yes', 'concedes_no': False, 'unsupported': []}
        for note in ('Acknowledges the venue-list worry without conceding it, then pivots to the phase-in.',
                     "Works well; it doesn't concede anything.", 'Leaves nothing unanswered.',
                     'Leaves no question unanswered.', 'Reads as answering rather than conceding.',
                     'Avoid conceding the venue list is unpublished.', 'Firm, and concedes nothing to the No side.',
                     'The post does not echo the No side.', 'Works well — it never concedes the point.'):
            self.assertEqual(agent_pipeline.check_issues({**passing, 'issues': [note]}), [], note)
        for note in ('Concedes that the venue list is undecided.', 'Leaves the worry unanswered.',
                     'The post leaves the core venue-scope worry unanswered.',
                     'The tone is not harsh, but it concedes the venue list is undecided.',
                     'Restates the no case and concedes the point.', 'Repeats the No side and concedes it.',
                     'It echoes the No side.', "Opening with 'a rule can't fix a short rota' concedes a No doubt.",
                     "The post's opener ‘we can’t wait’ concedes the timing worry.",
                     'The worry is acknowledged but not actually answered; add a line without conceding.',
                     'The venue worry is raised but not resolved; avoid leaving it unanswered.'):
            self.assertEqual(agent_pipeline.check_issues({**passing, 'issues': [note]}), [note], note)
        # One note can praise and fail: the real concession still fails it.
        mixed = "Doesn't concede the cost point, but leaves the venue worry unanswered."
        self.assertEqual(agent_pipeline.check_issues({**passing, 'issues': [mixed, 'Could be warmer.']}),
                         [mixed, 'Could be warmer.'])
        # The structured verdict decides on its own, whatever the notes say.
        self.assertEqual(agent_pipeline.check_issues({**passing, 'concedes_no': True,
                                                      'issues': ['Answers it without conceding.']}),
                         ['It concedes a No argument. Answer the worry instead of agreeing with it.',
                          'Answers it without conceding.'])


class WritingTemplateTests(unittest.TestCase):
    """P1-3: posts use the Trending guide, replies the truth-sandwich guide plus the author's recent posts."""

    def setUp(self):
        import tempfile
        from ctn_data.db import connect, initialize
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = str(Path(temporary.name) / 'test.db')
        initialize(self.db)
        conn = connect(self.db)
        with conn:
            conn.execute('INSERT INTO accounts(account_id,username,private,first_seen,last_seen) VALUES(?,?,?,?,?)',
                         (77, 'citizen', 0, 'x', 'x'))
            for post_id, body, at, visibility in ((1, 'Worried about the ID card.', '2026-10-21T10:00:00Z', None),
                                                  (2, 'My kids start school soon.', '2026-10-21T09:00:00Z', None),
                                                  (3, 'Followers only note.', '2026-10-21T11:00:00Z', 'followers'),
                                                  (4, 'Ferry was late again.', '2026-10-21T08:00:00Z', None)):
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen,visibility) '
                             'VALUES(?,?,?,?,?,?,?)', (post_id, 77, body, at, at, at, visibility))
        conn.close()
        self.agent = {'name': 'Leah', 'stance': "I'm voting Yes.", 'voice': 'Direct.'}
        self.sources = [{'id': 'post:1', 'post_id': 1, 'author_id': 77, 'username': 'citizen',
                         'text': 'Worried about the ID card.'}]

    def messages(self, action):
        decision = {'action': action, 'target_post_id': 1 if action == 'reply' else None, 'concern': 'id_privacy',
                    'angle': ''}
        return agent_pipeline.write_messages(self.agent, decision, self.sources, [], db_path=self.db,
                                             memory_view={}, recent_posts=[], tags=None, feedback=None, strategy=None)

    def test_post_uses_the_trending_guide(self):
        system, user = self.messages('post')
        self.assertIn('original post meant to reach Trending', system['content'])
        self.assertNotIn('This is a reply to a citizen', system['content'])
        self.assertIn('at most 480 characters', system['content'])
        self.assertIn('Worry to answer: the worry about id privacy.', system['content'])
        self.assertNotIn('AUTHOR_RECENT_POSTS', json.loads(user['content']))

    def test_post_gets_the_public_figure_and_reply_does_not(self):
        figure = {'username': 'RealVictorHawthorne', 'who': 'the President', 'how': 'ask the government'}
        for action in ('post', 'reply'):
            decision = {'action': action, 'target_post_id': 1 if action == 'reply' else None, 'concern': 'id_privacy',
                        'angle': '', 'mention_figure': figure}
            system, user = agent_pipeline.write_messages(self.agent, decision, self.sources, [], db_path=self.db,
                                                         memory_view={}, recent_posts=[], tags=None, feedback=None,
                                                         strategy=None)
            payload = json.loads(user['content'])
            if action == 'post':
                self.assertEqual(payload['MENTION_FIGURE'], figure)
                self.assertIn('MENTION_FIGURE is a public figure', system['content'])
                self.assertIn('MENTION_FIGURE.how is what they are responsible for', system['content'])
                self.assertIn('do not restate it or describe their role', system['content'])
                self.assertIn('At most 1 @mentions', system['content'])
            else:
                self.assertNotIn('MENTION_FIGURE', payload)
                self.assertIn('At most 0 @mentions', system['content'])

    def test_post_format_asks_once_for_the_missing_figure(self):
        figure = {'username': 'DrAmaraSingh'}
        fact = {'id': 'icu', 'claim': 'Kingstonia has about 30 ICU beds.'}
        body, issues = agent_pipeline.post_format('About 30 ICU beds. #VoteYes', [fact], None, figure)
        self.assertEqual(issues, ['@mention @DrAmaraSingh once, as MENTION_FIGURE says.'])
        _, issues = agent_pipeline.post_format('@dramarasingh, about 30 ICU beds. #VoteYes', [fact], None, figure)
        self.assertEqual(issues, [])

    def test_copy_check_counts_names_and_numbers_as_one_character(self):
        claim = ('Chief Medical Officer Dr Eleanor Whitlock warned that waiting for widespread transmission '
                 'could leave the health system dangerously exposed.')
        named = 'Chief Medical Officer Dr Eleanor Whitlock warned us; waiting would leave hospitals exposed.'
        self.assertEqual(agent_pipeline.copied_facts(named, [{'claim': claim}]), [])
        pasted = 'As she warned, waiting for widespread transmission could leave the health system dangerously exposed.'
        self.assertEqual(len(agent_pipeline.copied_facts(pasted, [{'claim': claim}])), 1)
        self.assertEqual(agent_pipeline.copied_text(pasted, claim),
                         'waiting for widespread transmission could leave the health system dangerously exposed.')

    def test_copied_text_maps_names_and_numbers_back_to_the_body(self):
        claim = 'Kingstonia has about 30 ICU beds across its two hospitals.'
        body = 'Did you know Kingstonia has about 30 ICU beds across its two hospitals? Voting Yes.'
        self.assertEqual(agent_pipeline.copied_text(body, claim), 'Kingstonia has about 30 ICU beds across its two hospitals')
        self.assertEqual(agent_pipeline.copied_text('', claim), '')
        issue = agent_pipeline.verbatim_issue(body, [{'id': 'icu_beds', 'claim': claim}])
        self.assertEqual(issue, agent_pipeline.VERBATIM_ISSUE + ' Copied text: "Kingstonia has about 30 ICU beds across '
                                'its two hospitals" (fact icu_beds).')

    def test_quoted_issues_are_clipped_and_compare_as_the_plain_issue(self):
        long = ' '.join(['word'] * 60)
        clipped = agent_pipeline._clip(long)
        self.assertLessEqual(len(clipped), agent_pipeline.SNIPPET_CHARS + 1)
        self.assertTrue(clipped.endswith('word…'))
        self.assertEqual(agent_pipeline._clip('a\n  b'), 'a b')
        quoted = [agent_pipeline.VERBATIM_ISSUE + ' Copied text: "x".', agent_pipeline.PERSONAL_ISSUE + ' Already ...',
                  'Write only in English.']
        self.assertEqual(agent_pipeline.generic_issues(quoted),
                         [agent_pipeline.VERBATIM_ISSUE, agent_pipeline.PERSONAL_ISSUE, 'Write only in English.'])
        self.assertEqual(agent_pipeline.generic_issues(None), [])

    def test_swap_facts_replaces_only_the_copied_fact(self):
        a, b, c, d = ({'id': key} for key in 'abcd')
        self.assertEqual(agent_pipeline.swap_facts([a, b], [a], [a, b, c, d]), [c, b])
        self.assertEqual(agent_pipeline.swap_facts([a], [a], [a]), [])

    def test_reply_uses_the_reply_guide_and_author_recent_posts(self):
        system, user = self.messages('reply')
        self.assertIn('This is a reply to a citizen', system['content'])
        self.assertIn('at most 320 characters', system['content'])
        self.assertNotIn('{', system['content'].split('Return JSON')[0])
        recent = json.loads(user['content'])['AUTHOR_RECENT_POSTS']
        # The target itself and the non-public post are excluded; newest first.
        self.assertEqual([row['text'] for row in recent], ['My kids start school soon.', 'Ferry was late again.'])


if __name__ == '__main__':
    unittest.main()
