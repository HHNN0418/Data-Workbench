"""D48 staged-pipeline changes: reply-back priority, Trending-ready originals, a lean decide prompt."""
from collections import Counter
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ctn_data import agent_pipeline, agent_targeting, fact_bank
from ctn_data.agent_control import AgentController

GOOD = {'direction': 'toward_yes', 'concedes_no': False, 'unsupported': [], 'issues': []}


class StagedFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fact_bank.invalidate()
        self.addCleanup(fact_bank.invalidate)
        agent_targeting._cache.clear()
        self.addCleanup(agent_targeting._cache.clear)
        self.db = Path(temporary.name) / 'test.db'
        self.controller = AgentController(self.db, Path(temporary.name) / 'secrets',
            provider_store=SimpleNamespace(get=lambda _: {'id': 'fake', 'protocol': 'openai_chat', 'model': 'fake'}),
            clock=lambda: 1792450000.0)
        self.addCleanup(self.controller.stop)
        self.controller.save({'name': 'Reader', 'username': 'fictional_reader', 'provider_id': 'fake',
                              'stance': 'Supports Yes.', 'voice': 'Warm and plain.',
                              'allowed_actions': ['post', 'reply']})
        self.agent_id = self.controller.snapshot()['agents'][0]['id']
        self.controller.store.change(lambda data, conn: (
            data['agents'][self.agent_id].update(account_id=10, account_state='ready'),
            data['settings'].__setitem__(fact_bank.SETTINGS_KEY, {**fact_bank.DEFAULT_SETTINGS, 'pipeline': 'staged'})))
        self.sources = [
            {'id': 'post:7', 'post_id': 7, 'author_id': 99, 'kind': 'post', 'version': 'v1',
             'text': "I'm not sure. Won't the national ID card track my health?", 'notification_ids': []},
            {'id': 'post:8', 'post_id': 8, 'author_id': 98, 'kind': 'post', 'version': 'v1',
             'reply_to_post_id': 500, 'root_post_id': 500, 'notification_ids': [41],
             'text': "Not sure about that. Will the hospital cope with H2D2?"}]
        patcher = patch.object(self.controller, '_read_sources', side_effect=lambda *a, **k: [dict(s) for s in self.sources])
        patcher.start()
        self.addCleanup(patcher.stop)

    def history(self, actions, original_post_id=500):
        def change(data, conn):
            for index, action in enumerate(actions):
                data['drafts'][f'old{index}'] = {
                    'id': f'old{index}', 'agent_id': self.agent_id, 'action': action, 'state': 'published',
                    'created_at': f'2026-10-21T10:0{index}:00Z', 'post_id': original_post_id + index}
        self.controller.store.change(change)

    def model(self, decision, bodies, verdicts=None):
        calls = {'decide': [], 'write': [], 'check': []}
        bodies, verdicts = list(bodies), list(verdicts or [GOOD] * 3)

        def answer(controller, provider, messages, response_schema=None, **options):
            if response_schema is agent_pipeline.WRITE_SCHEMA:
                calls['write'].append(messages)
                return {'text': json.dumps({'body': bodies.pop(0)}), 'usage': {'total_tokens': 5}}
            if response_schema is agent_pipeline.CHECK_SCHEMA:
                calls['check'].append(messages)
                return {'text': json.dumps(verdicts.pop(0)), 'usage': {'total_tokens': 3}}
            calls['decide'].append((messages, response_schema))
            return {'text': json.dumps(decision), 'model': 'fake', 'usage': {'total_tokens': 20}}
        return patch('ctn_data.agent_budget.model_call', side_effect=answer), calls

    def decision(self, **changes):
        return {'action': 'reply', 'target_post_id': 8, 'target_account_id': None, 'source_ids': ['post:8'],
                'notification_ids': [41], 'memory': '', 'reason': 'A citizen answered our post.',
                'memory_updates': {'viewpoints': [], 'questions': [], 'people': []},
                'concern': 'hospital', 'fact_ids': ['icu_capacity'], 'angle': 'Hospital capacity.', **changes}

    def new_draft(self):
        return next(d for d in self.controller.store.read()['drafts'].values() if not d['id'].startswith('old'))


class ReplyBackPriorityTests(StagedFixture):
    def test_reply_turn_answers_the_citizen_on_our_original_first(self):
        # post 500 is one of our published originals (the first 'post' below); post 8 answers it.
        self.history(['reply', 'post', 'post'], original_post_id=499)
        mock, calls = self.model(self.decision(), ['ICU has about 30 beds, so a surge matters. Voting Yes.'])
        with mock:
            self.controller.run(self.agent_id)
        messages, schema = calls['decide'][0]
        self.assertEqual(schema['properties']['action']['enum'], ['reply'])
        self.assertEqual(schema['properties']['target_post_id']['enum'], [None, 8])
        prompt = json.loads(messages[1]['content'])
        self.assertEqual(prompt['reply_first'], [8])
        self.assertIn('reply_first lists citizens answering one of our own original posts', messages[0]['content'])
        draft = self.new_draft()
        self.assertEqual((draft['action'], draft['target_post_id'], draft.get('reply_back')), ('reply', 8, True))
        self.assertEqual(draft['notification_ids'], [41])

    def test_without_a_reply_back_every_target_stays_open(self):
        self.history(['reply', 'post', 'post'], original_post_id=900)
        mock, calls = self.model(self.decision(target_post_id=7, notification_ids=[], source_ids=['post:7'],
                                               concern='id_privacy'),
                                 ['The certificate is separate from the ID card. Voting Yes.'])
        with mock:
            self.controller.run(self.agent_id)
        _, schema = calls['decide'][0]
        self.assertEqual(schema['properties']['target_post_id']['enum'], [None, 7, 8])
        self.assertNotIn('reply_first', json.loads(calls['decide'][0][0][1]['content']))
        self.assertNotIn('reply_back', self.new_draft())


class TrendingOriginalTests(StagedFixture):
    def post_decision(self, **changes):
        return self.decision(action='post', target_post_id=None, notification_ids=[], source_ids=['post:7'],
                             **changes)

    def test_original_gets_one_hashtag_and_the_live_topic(self):
        hot = [{'concern': 'hospital', 'citizens': 4, 'radar': True}, {'concern': 'freedom', 'citizens': 2}]
        mock, calls = self.model(self.post_decision(),
                                 ['Kingstonia has about 30 ICU beds for two hospitals. I am voting Yes.'])
        with mock, patch('ctn_data.agent_opportunities.hot_concerns', return_value=hot), \
                patch('ctn_data.agent_targeting.figure_mention', return_value=None), \
                patch('ctn_data.agent_decision.next_mix', return_value='post'):
            self.controller.run(self.agent_id)
        messages, _ = calls['decide'][0]
        prompt = json.loads(messages[1]['content'])
        self.assertEqual(prompt['citizen_activity'], hot)
        self.assertIn('citizen_activity counts the worries', messages[0]['content'])
        self.assertEqual(prompt['pacing']['next'], 'post')
        self.assertIn('peak_hour', prompt['pacing'])
        # Facts for the live concern are offered.
        self.assertTrue(any('hospital' in f['concerns'] for f in prompt['facts']))
        writer = json.loads(calls['write'][0][1]['content'])
        self.assertEqual(writer['TAGS'], ['#H2D2'])
        self.assertIn('Exactly one hashtag', calls['write'][0][0]['content'])
        draft = self.new_draft()
        self.assertEqual(draft['body'], 'Kingstonia has about 30 ICU beds for two hospitals. I am voting Yes. #H2D2')
        self.assertNotIn('format_warnings', draft)
        # The checker saw the final text with its hashtag.
        self.assertEqual(json.loads(calls['check'][0][1]['content'])['MESSAGE'], draft['body'])

    def test_original_without_a_fact_is_rewritten_once_and_never_dropped(self):
        vague = 'People keep asking me about the vote. I am voting Yes. #H2D2'
        mock, calls = self.model(self.post_decision(), [vague, vague])
        with mock, patch('ctn_data.agent_opportunities.hot_concerns', return_value=[]), \
                patch('ctn_data.agent_targeting.figure_mention', return_value=None), \
                patch('ctn_data.agent_decision.next_mix', return_value='post'):
            self.controller.run(self.agent_id)
        self.assertEqual(len(calls['write']), 2)
        retry = json.loads(calls['write'][1][1]['content'])
        self.assertTrue(any('concrete number or fact' in issue for issue in retry['FIX_THESE_ISSUES']))
        draft = self.new_draft()
        self.assertEqual(draft['body'], vague)
        self.assertTrue(any('concrete number or fact' in w for w in draft['format_warnings']))

    def test_post_format_rules(self):
        facts = [{'claim': 'The vaccine is free to the person receiving it, at hospitals, GP clinics and pop-up sites.'}]
        body, issues = agent_pipeline.post_format('The jab costs nothing at GP clinics and pop-up sites.', facts,
                                                  {'tags': [{'name': 'kingstonia'}, {'name': 'H2D2'}]})
        self.assertEqual((body, issues), ('The jab costs nothing at GP clinics and pop-up sites. #H2D2', []))
        _, issues = agent_pipeline.post_format('Two #tags here #H2D2 and 30 beds.', facts, None)
        self.assertEqual(issues, ['Use exactly one hashtag; remove the others.'])
        _, issues = agent_pipeline.post_format('Nobody knows anything. #H2D2', facts, None)
        self.assertEqual(len(issues), 1)
        self.assertEqual(agent_pipeline.post_format('x' * 497, facts, None)[1][0],
                         'Add exactly one hashtag from TAGS and keep the text under the limit.')


class LeanDecidePromptTests(unittest.TestCase):
    def context(self):
        long_text = 'Not sure about the referendum. ' * 80
        sources = [{'id': f'post:{i}', 'kind': 'post', 'post_id': i, 'author_id': 100 + i, 'text': long_text,
                    'version': 'x' * 64, 'url': f'https://social.legitreal.com/posts/{i}', 'observed_at': 'now',
                    'first_seen': 'now', 'freshness_at': 'now', 'thread_context': {'complete': True},
                    'target': {'stance': 'undecided', 'concerns': [], 'score': 3, 'notes': []},
                    'notification_ids': []} for i in range(12)]
        memory = {'interpretation': 'x' * 300, 'long_term': {'status': 'recalled'},
                  'subjective_notes': {'summary': 's' * 3000, 'viewpoints': [{'text': 'v' * 300}] * 3,
                                       'people': [{'account_id': 101, 'text': 'Asked about ICU beds.'},
                                                  {'account_id': 5, 'text': 'Not in this reading.'}]},
                  'observed_reading': [{'source_id': 'x', 'version': 'y' * 64}] * 10,
                  'completed_actions': [{'draft_id': 'd' * 36, 'at': '2026-10-22T00:00:00Z', 'action': 'reply',
                                         'state': 'published', 'post_id': 1, 'target_post_id': 2,
                                         'target_account_id': None}] * 6,
                  'pending_intentions': [{'draft_id': 'p', 'action': 'post', 'state': 'pending'}],
                  'evidence_sources': {'e1': {'source_id': 'x' * 200}}, 'other_outcomes': [{'x': 'y' * 300}] * 3}
        return {'sources': sources, 'allowed_actions': ['post', 'reply'],
                'autobiographical_memory': memory,
                'operational_constraints': [{'draft_id': 'z', 'action': 'reply', 'state': 'discarded',
                                             'target_post_id': 2, 'source_ids': ['post:2'] * 5,
                                             'instruction': 'Do not recreate.'}] * 4,
                'relationship_context': {'account_id': 10, 'relations': [
                    {'kind': 'like', 'present': True, 'target_post_id': 9000 + i, 'observed_at': 'now'}
                    for i in range(150)]},
                'competition_feedback': {'objective': 'o' * 1500},
                'recent_own_posts': ['r' * 450] * 5,
                'action_availability': {'post': {'available': True}}}

    def test_decide_prompt_keeps_targets_and_drops_unneeded_material(self):
        facts = [{'id': 'icu_capacity', 'claim': 'About 30 ICU beds.', 'concerns': ['hospital']}]
        context = self.context()
        old_size = len(json.dumps({key: context[key] for key in (
            'autobiographical_memory', 'operational_constraints', 'relationship_context', 'competition_feedback',
            'recent_own_posts', 'sources')}, ensure_ascii=False))
        messages = agent_pipeline.decide_messages({'name': 'Reader', 'stance': 'Yes.'}, context, facts,
                                                  team_goal='Vote Yes', blocked=True)
        prompt = json.loads(messages[1]['content'])
        for key in ('competition_feedback', 'relationship_context', 'autobiographical_memory'):
            self.assertNotIn(key, prompt)
        self.assertEqual(len(prompt['sources']), 12)
        first = prompt['sources'][0]
        self.assertEqual((first['id'], first['post_id'], first['author_id']), ('post:0', 0, 100))
        self.assertEqual(first['target']['stance'], 'undecided')
        self.assertEqual(len(first['text']), agent_pipeline.DECIDE_TEXT)
        self.assertTrue(first['text_truncated'])
        for key in ('version', 'url', 'observed_at', 'thread_context'):
            self.assertNotIn(key, first)
        self.assertEqual(prompt['memory']['people'], [{'account_id': 101, 'text': 'Asked about ICU beds.'}])
        self.assertEqual(len(prompt['memory']['recent_actions']), 5)
        self.assertEqual(prompt['operational_constraints']['rule'], 'Do not recreate.')
        self.assertEqual(len(prompt['recent_own_posts']), 3)
        self.assertEqual(prompt['facts'][0]['id'], 'icu_capacity')
        self.assertEqual(prompt['allowed_actions'], ['post', 'reply'])
        self.assertLess(len(messages[1]['content']), old_size * 0.45)

    def test_relation_actions_keep_only_relevant_relations(self):
        context = {**self.context(), 'allowed_actions': ['post', 'reply', 'follow']}
        context['relationship_context']['relations'].append({'kind': 'follow', 'present': True,
                                                             'target_account_id': 101})
        prompt = json.loads(agent_pipeline.decide_messages({'name': 'R'}, context, [], team_goal='Vote Yes',
                                                           blocked=True)[1]['content'])
        self.assertEqual(prompt['relationship_context']['relations'],
                         [{'kind': 'follow', 'present': True, 'target_account_id': 101}])


class FactRotationTests(StagedFixture):
    def test_offered_facts_pass_the_account_for_its_cap_and_rotation(self):
        with patch('ctn_data.fact_bank.usage', return_value=(Counter({'id_law_no_health': 3}), Counter())) as usage:
            facts = agent_pipeline.offered_facts(str(self.db), [self.sources[0] | {
                'target': {'concerns': ['id_privacy']}}], agent_id='role-1')
            plain = agent_pipeline.offered_facts(str(self.db), [self.sources[0] | {
                'target': {'concerns': ['id_privacy']}}])
        usage.assert_called_once_with(str(self.db), 'role-1')
        ids, plain_ids = [f['id'] for f in facts], [f['id'] for f in plain]
        self.assertEqual(plain_ids[:2], ['id_law_no_health', 'certificate_separate'])
        # Used three times by this account in six hours: offered after the unused one.
        self.assertEqual(ids[:2], ['certificate_separate', 'id_law_no_health'])

    def test_staged_decision_offers_facts_for_this_role(self):
        mock, _ = self.model(self.decision(action='post', target_post_id=None, notification_ids=[],
                                           source_ids=['post:7']),
                             ['About 30 ICU beds serve both hospitals. I am voting Yes.'])
        with mock, patch('ctn_data.agent_pipeline.offered_facts', wraps=agent_pipeline.offered_facts) as offered, \
                patch('ctn_data.agent_opportunities.hot_concerns', return_value=[]), \
                patch('ctn_data.agent_targeting.figure_mention', return_value=None), \
                patch('ctn_data.agent_decision.next_mix', return_value='post'):
            self.controller.run(self.agent_id)
        self.assertEqual(offered.call_args.kwargs['agent_id'], self.agent_id)


if __name__ == '__main__':
    unittest.main()
