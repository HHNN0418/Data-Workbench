"""Offline checks of advisory writing hints and platform body validation."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ctn_data import agent_writing
from ctn_data.agent_control import AgentController, AgentError


class ReplyGuideMix(unittest.TestCase):
    def test_openings_and_endings_are_drawn_within_the_target_mix(self):
        import random
        rng = random.Random(3)
        guides = [agent_writing.reply_guide(rng) for _ in range(2000)]
        self.assertTrue(all('{' not in guide for guide in guides))
        question = sum('genuine question' in guide for guide in guides) / len(guides)
        yes_line = sum('why you are voting Yes' in guide for guide in guides) / len(guides)
        self.assertLess(question, 0.30)
        self.assertLess(yes_line, 0.40)
        self.assertGreater(len(set(guides)), 6)

    def test_fixed_guide_has_no_template_opener_or_stock_phrases(self):
        guide = agent_writing.REPLY_GUIDE
        self.assertIn('150 to 320 characters', guide)
        self.assertIn('Use the one fact from FACTS that answers them and leave the others out', guide)
        self.assertNotIn('{', guide)
        self.assertNotIn('feel heard', guide)


class WritingStructure(unittest.TestCase):
    """D97: no shared "worry -> the government says -> fact -> fact" skeleton for every role."""

    def test_posts_offer_two_different_shapes_and_news_only_with_a_news_fact(self):
        import random
        rng = random.Random(7)
        news = [shape for _, shape, needs_news in agent_writing.POST_SHAPES if needs_news]
        plain = [agent_writing.post_guide(rng) for _ in range(300)]
        with_news = [agent_writing.post_guide(rng, news=True) for _ in range(300)]
        self.assertFalse(any(shape in guide for guide in plain for shape in news))
        self.assertTrue(any(news[0] in guide for guide in with_news))
        for guide in plain + with_news:
            offered = [shape for _, shape, _ in agent_writing.POST_SHAPES if shape in guide]
            self.assertEqual(len(offered), 2, guide)
            self.assertNotIn('{', guide)
        # Every shape gets offered, not only the first ones.
        self.assertEqual({shape for _, shape, _ in agent_writing.POST_SHAPES},
                         {shape for _, shape, _ in agent_writing.POST_SHAPES if any(shape in g for g in with_news)})

    def test_replies_offer_two_different_moves_and_one_fact(self):
        import random
        rng = random.Random(8)
        for guide in (agent_writing.reply_guide(rng) for _ in range(300)):
            self.assertEqual(sum(move in guide for _, move in agent_writing.REPLY_MOVES), 2, guide)
            self.assertIn('answer what this person actually wrote', guide)
            self.assertIn('skip it only when they asked about something none of FACTS covers', guide)
            self.assertIn('never means taking over the No side\'s words', guide)

    def test_templates_drop_the_shared_skeleton(self):
        for guide in (agent_writing.POST_GUIDE, agent_writing.REPLY_GUIDE):
            self.assertIn('never as "The government says"', guide)
            self.assertNotIn('say who says so', guide)
            self.assertNotIn('why that settles it', guide)
        self.assertNotIn('First sentence names the specific claim', agent_writing.POST_GUIDE)
        self.assertNotIn('one or two concrete, checkable facts', agent_writing.POST_GUIDE)

    def test_every_shape_and_move_leaves_a_yes_reason(self):
        # 24 Oct: 9 of 15 neutral rejects only explained the ballot or asked a question.
        import random
        rng = random.Random(9)
        for guide in [agent_writing.post_guide(rng) for _ in range(50)]:
            self.assertIn('one concrete reason to vote Yes, as FACTS state it', guide)
        # Replies keep one fact: the same line there made them stack a second (A/B 24 Oct).
        self.assertNotIn('reason to vote Yes', agent_writing.reply_guide(rng))

    def test_venue_worries_are_answered_with_a_safeguard(self):
        # 24 Oct: 6 of 15 neutral rejects answered "which places" with "the list comes later".
        from ctn_data import agent_pipeline
        self.assertIn('Do not say the list will be set later or through regulations', agent_pipeline.WRITE_RULES)
        self.assertIn('the phased start, medical exemptions or the under-12 exemption', agent_pipeline.WRITE_RULES)

    def test_write_prompt_runs_from_shared_text_to_this_call(self):
        # Prompt cache, plan B: what every role shares comes first, the name and the drawn guide last.
        import random
        from ctn_data import agent_pipeline
        def system(name, action, seed):
            agent = {'id': name.lower(), 'name': name, 'stance': f'{name} supports Yes.', 'voice': 'plain'}
            decision = {'action': action, 'concern': 'access', 'target_post_id': None}
            return agent_pipeline.write_messages(agent, decision, [], [], db_path=None, memory_view={},
                                                 recent_posts=[], tags=[], feedback=None, strategy=None,
                                                 rng=random.Random(seed))[0]['content']
        for action, (template, drawn) in (('post', agent_writing.post_guide_parts(random.Random(3))),
                                          ('reply', agent_writing.reply_guide_parts(random.Random(3)))):
            text = system('Maya', action, 3)
            self.assertTrue(text.startswith(agent_pipeline.WRITE_RULES))
            order = [text.index(part) for part in (agent_pipeline.WRITE_RULES, template, 'as Maya,', 'TASK:', drawn)]
            self.assertEqual(order, sorted(order))
            # Two roles share everything up to the persona; the same draw gives the same prompt.
            other = system('Owen', action, 4)
            shared = len(agent_pipeline.WRITE_RULES) + len(template)
            self.assertEqual(text[:shared], other[:shared])
            self.assertEqual(system('Maya', action, 3), text)
            self.assertNotIn('{', text.split('TASK:')[1].replace('{"body"', ''))

    def test_the_writer_gets_the_news_shape_only_when_a_fact_has_a_link(self):
        import random
        from ctn_data import agent_pipeline
        agent = {'id': 'maya', 'name': 'Maya', 'stance': 'Yes', 'voice': 'plain'}
        news = next(shape for _, shape, needs_news in agent_writing.POST_SHAPES if needs_news)
        fact = {'id': 'n:1', 'claim': 'A second clinic opened in Port Ellis.', 'source': 'Herald'}

        def systems(facts):
            return [agent_pipeline.write_messages(
                agent, {'action': 'post', 'concern': 'access'}, [], facts, db_path=None, memory_view={},
                recent_posts=[], tags=[], feedback=None, strategy=None, rng=random.Random(seed))[0]['content']
                for seed in range(40)]
        self.assertFalse(any(news in system for system in systems([fact])))
        self.assertTrue(any(news in system for system in systems([{**fact, 'url': 'https://herald.example/a'}])))


class WritingRules(unittest.TestCase):
    def test_sentence_dashes_are_flagged_without_rewriting(self):
        for dash in ('—', '——', '–', '―', '⸺', '⸻', '--'):
            with self.subTest(dash=dash):
                body = f'I read the plan {dash} I still have a question.'
                self.assertEqual(len(agent_writing.warnings(body)), 1)
                self.assertIn(dash, body)

    def test_obvious_markdown_is_flagged(self):
        for body in ('# Library news', '- A library question', '* A library question',
                     '+ A library question', '1. A library question', '2) A library question',
                     '**Read the plan**', '__Read the plan__', '我**认真**考虑过', '我__认真__考虑过',
                     '```text\nRead this\n```',
                     '---', '***', '___', '> Read the plan'):
            with self.subTest(body=body):
                self.assertTrue(agent_writing.warnings(body))

    def test_plain_punctuation_links_hyphens_numbers_and_tags_are_allowed(self):
        body = ("@Avery, have you read the low-cost option? It was -5 yesterday, "
                "and the change was -2.5. #Library https://example.invalid/a–b "
                "www.example.invalid/a—b https://example.invalid/foo--bar "
                "foo--bar account__short__name\n- 5\n3 * 2 = 6. 1.5 is the estimate.")
        self.assertEqual(agent_writing.warnings(body), [])

    def test_empty_nonwriting_actions_need_no_style_changes(self):
        self.assertEqual(agent_writing.warnings('', 'repost'), [])
        self.assertEqual(agent_writing.warnings('', 'like'), [])
        self.assertEqual(agent_writing.warnings('', 'skip'), [])

    def test_tone_is_prompted_instead_of_guessed_by_phrase_blacklist(self):
        self.assertEqual(agent_writing.warnings('Great point. In summary, I agree.'), [])
        self.assertIn('不必故意制造错字或编造生活经历', agent_writing.SYSTEM_RULES)
        self.assertIn('角色语言、用词、句子长短和个人习惯', agent_writing.SYSTEM_RULES)


class WritingDraftGuard(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        self.now = 1792450000.0
        provider = {'id': 'fake', 'protocol': 'openai_chat', 'model': 'fake', 'max_output_tokens': 1800}
        self.controller = AgentController(Path(self.temp.name) / 'state.db', Path(self.temp.name) / 'keys',
            provider_store=SimpleNamespace(get=lambda _: provider), clock=lambda: self.now)
        self.controller.save({'name': 'Fictional reader', 'username': 'fictional_reader', 'provider_id': 'fake'})
        self.agent_id = self.controller.snapshot()['agents'][0]['id']
        def bind(data, conn):
            data['agents'][self.agent_id].update(account_id=10, account_state='ready')
        self.controller.store.change(bind)
        self.sources = [{'id': 'post:7', 'kind': 'post', 'post_id': 7, 'author_id': 99,
                         'text': 'A recent fictional library discussion', 'version': 'v1', 'notification_ids': []}]
        patch('ctn_data.agent_observation.observe', return_value=self.sources).start()
        patch('ctn_data.agent_observation.refresh').start()
        self.model = patch('ctn_data.agent_budget.model_call').start()
        self.sdk = patch.object(self.controller, '_sdk').start()
        self.account = SimpleNamespace(post=lambda body: {'post_id': 100, 'author_id': 10, 'body': body})
        patch.object(self.controller, '_account', return_value=self.account).start()
        patch('ctn_data.agent_budget.call', side_effect=lambda controller, bucket, method, *args, **kwargs: method(*args, **kwargs)).start()

    def decide(self, body='I read it — what happens next?'):
        self.model.return_value = {'text': json.dumps({'action': 'post', 'body': body, 'target_post_id': None,
            'source_ids': ['post:7'], 'notification_ids': [], 'reason': 'A specific question.',
            'memory': 'I am considering a question.'}), 'model': 'fake', 'usage': {}}
        self.controller.run(self.agent_id)
        return self.controller.snapshot()['drafts'][0]

    def test_generation_retains_exact_body_and_hints_in_an_editable_pending_draft(self):
        draft = self.decide()
        self.assertEqual(draft['body'], 'I read it — what happens next?')
        self.assertEqual(draft['state'], 'pending')
        self.assertTrue(draft['style_warnings'])
        self.assertEqual(draft['error'], '')
        self.sdk.assert_not_called()
        system = self.model.call_args.args[2][0]['content']
        self.assertIn(agent_writing.SYSTEM_RULES, system)

    def test_review_execution_keeps_style_hints_without_blocking_publication(self):
        draft = self.decide('I read it. What happens next?')
        self.controller.publish(draft['id'], 'I read it — what happens next?')
        state = self.controller.store.read()
        row = state['drafts'][draft['id']]
        self.assertEqual(row['state'], 'published')
        self.assertEqual(row['body'], 'I read it — what happens next?')
        self.assertTrue(row['style_warnings'])
        self.assertEqual(row['error'], '')
        self.assertEqual(state['rate']['accounts'][self.agent_id]['attempts'], 1)
        self.sdk.assert_called_once()

    def test_manual_edit_is_rechecked_and_published_without_old_warnings(self):
        draft = self.decide()
        result = self.controller.publish(draft['id'], 'I read it. What happens next?')['drafts'][0]
        self.assertEqual(result['state'], 'published')
        self.assertEqual(result['body'], 'I read it. What happens next?')
        self.assertEqual(result['style_warnings'], [])
        self.sdk.assert_called_once()

    def test_legacy_pending_draft_is_rechecked_at_publish(self):
        draft = self.decide()
        self.controller.store.change(lambda data, conn: data['drafts'][draft['id']].pop('style_warnings'))
        self.controller.publish(draft['id'])
        self.assertTrue(self.controller.store.read()['drafts'][draft['id']]['style_warnings'])
        self.assertEqual(self.controller.store.read()['drafts'][draft['id']]['state'], 'published')
        self.sdk.assert_called_once()

    def test_auto_generation_publishes_with_advisory_style_hints(self):
        self.controller.settings({'enabled': True})
        self.controller.save({'id': self.agent_id, 'name': 'Fictional reader', 'enabled': True, 'mode': 'auto'})
        draft = self.decide()
        self.assertEqual(self.controller.store.read()['drafts'][draft['id']]['state'], 'published')
        self.assertTrue(self.controller.store.read()['drafts'][draft['id']]['style_warnings'])
        self.assertEqual(self.model.call_count, 1)
        self.sdk.assert_called_once()
        self.assertFalse(any(e['kind'] in ('error', 'uncertain', 'rejected')
                             for e in self.controller.store.read()['events']))

    def test_direct_reservation_keeps_style_hints_and_reserves_delivery(self):
        draft = self.decide()
        with self.controller._claim(self.agent_id) as token:
            from ctn_data import agent_review
            agent_review.ensure(self.controller, draft['id'], draft['body'], token=token)
            self.controller._reserve(draft['id'], None, token, False)
        state = self.controller.store.read()
        self.assertEqual(state['drafts'][draft['id']]['state'], 'sending')
        self.assertTrue(state['drafts'][draft['id']]['style_warnings'])
        self.assertEqual(state['rate']['accounts'][self.agent_id]['attempts'], 1)

    def test_official_body_length_and_nonempty_checks_still_block(self):
        draft = self.decide('An ordinary sentence.')
        for body in ('', 'x' * 501):
            with self.subTest(body_length=len(body)), self.assertRaises(AgentError):
                self.controller.publish(draft['id'], body)
        self.sdk.assert_not_called()


if __name__ == '__main__':
    unittest.main()
