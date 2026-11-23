"""D90: the templates no longer hand every role the same lines or invented anecdotes."""
import json
import random
import unittest
from unittest.mock import patch

from ctn_data import agent_pipeline, agent_writing


class TemplateTests(unittest.TestCase):
    def test_no_shared_vote_date_fair_chance_line_or_life_moment(self):
        rng = random.Random(1)
        texts = [agent_writing.post_guide(rng) for _ in range(50)] + [agent_writing.reply_guide(rng) for _ in range(50)]
        texts.append(agent_pipeline.WRITE_SYSTEM)
        for text in texts:
            self.assertNotIn('2 November', text)
            self.assertNotIn('fair chance', text)
            self.assertNotIn('moment from your own life', text)
        self.assertTrue(any('no "voting Yes" line' in text for text in texts))
        for options in (agent_writing.POST_ENDINGS, agent_writing.REPLY_MOVES):
            self.assertAlmostEqual(sum(weight for weight, _ in options), 1.0)


class PersonalRepeatTests(unittest.TestCase):
    RECENT = ['I run a café near the wharf and I watched the queue grow. The ministry says the jab is free.']

    def test_a_repeated_first_person_detail_is_sent_back(self):
        body = ('People say the rule is rushed, but the health ministry plans a phased start once everyone has access. '
                'I run a café near the wharf and I watched the queue grow. Planning ahead is cheaper than a lockdown.')
        self.assertTrue(agent_pipeline.repeated_personal(body, self.RECENT))
        issues = agent_pipeline.local_issues(body, 'post', self.RECENT)
        self.assertIn(agent_pipeline.PERSONAL_ISSUE, agent_pipeline.generic_issues(issues))
        # The rewrite is told exactly which words to drop.
        self.assertIn(agent_pipeline.PERSONAL_ISSUE + ' Already in your recent posts, do not reuse: '
                      '"I run a café near the wharf and I watched the queue grow".', issues)

    def test_repeated_runs_are_merged_and_quoted_in_the_post_own_words(self):
        recent = ['The question I keep hearing is who pays. I am voting Yes on 2 November, gladly.']
        body = ('the question I keep hearing is simple. Our ICU has 30 beds. '
                'Honestly, I am voting Yes on 2 November because of it.')
        self.assertEqual(agent_pipeline.personal_repeats(body, recent),
                         ['the question I keep hearing is', 'I am voting Yes on 2 November'])
        # Only first-person sentences count, as in repeated_personal.
        self.assertEqual(agent_pipeline.personal_repeats('The question everyone keeps hearing is who pays.', recent), [])
        self.assertEqual(agent_pipeline.personal_repeats(body, []), [])
        self.assertEqual(agent_pipeline.personal_issue('Nothing repeated here.', recent), agent_pipeline.PERSONAL_ISSUE)

    def test_new_wording_and_shared_facts_without_first_person_pass(self):
        self.assertFalse(agent_pipeline.repeated_personal('Our crew learned to plan before storms.', self.RECENT))
        # The same fact wording outside a first-person sentence is left to the verbatim check.
        self.assertFalse(agent_pipeline.repeated_personal('The ministry says the jab is free and quick.', self.RECENT))
        self.assertFalse(agent_pipeline.repeated_personal('I run a café near the wharf.', []))


class SelfIntroTests(unittest.TestCase):
    """24 Oct: roles kept saying what they do ("I run a hall", "on my show") once D97 said "start from your work"."""
    HALL = ['A hall booking is a promise. I run a hall, so I think in bookings.',
            'Running a hall means planning the week ahead.',
            'The ferry was late again today.']

    def test_a_habit_of_saying_the_job_is_sent_back(self):
        body = 'My hall calendar fills fast in the wet season. Kingstonia Central has 40 unfilled nursing posts.'
        self.assertTrue(agent_pipeline.self_intro(body, self.HALL))
        self.assertIn(agent_pipeline.SELF_INTRO_ISSUE, agent_pipeline.local_issues(body, 'post', self.HALL))
        for phrase in ('On air I read fine print out loud for a living.', 'On my show I read hospital numbers.',
                       'As a teacher I see the empty desks.', 'I drive a cab past the clinic.'):
            self.assertTrue(agent_pipeline.self_intro(phrase, self.HALL), phrase)

    def test_once_in_a_while_or_without_a_job_passes(self):
        body = 'My hall calendar fills fast in the wet season.'
        # Only one of the last ten texts said it: not a habit yet.
        self.assertFalse(agent_pipeline.self_intro(body, self.HALL[1:]))
        # Saying the job ten texts ago no longer counts.
        self.assertFalse(agent_pipeline.self_intro(body, self.HALL[:2] + ['A plain post.'] * 10))
        for plain in ('I ran the numbers on the 30 ICU beds.', 'The queue at the pop-up clinic was short today.',
                      'Our street emptied in the last lockdown.'):
            self.assertFalse(agent_pipeline.self_intro(plain, self.HALL), plain)

    def test_the_writer_sees_its_recent_openings(self):
        recent = [f'Opening {i}. More text.' for i in range(14)]
        self.assertEqual(agent_pipeline.recent_openings(recent), [f'Opening {i}.' for i in range(4, 14)])
        self.assertEqual(agent_pipeline.recent_openings(['x' * 300]), ['x' * agent_pipeline.OPENING_CHARS])
        agent = {'id': 'maya', 'name': 'Maya', 'stance': 'Yes', 'voice': 'plain'}
        for action in ('post', 'reply'):
            decision = {'action': action, 'concern': 'access', 'target_post_id': None}
            system, user = agent_pipeline.write_messages(agent, decision, [], [], db_path=None, memory_view={},
                                                         recent_posts=self.HALL, tags=[], feedback=None, strategy=None,
                                                         rng=random.Random(1))
            self.assertEqual(json.loads(user['content'])['YOUR_RECENT_OPENINGS'],
                             ['A hall booking is a promise.', 'Running a hall means planning the week ahead.',
                              'The ferry was late again today.'])
            self.assertIn('Open differently from every line in YOUR_RECENT_OPENINGS', system['content'])
            self.assertIn('do not say what your job is', system['content'])


class RecommendedFactCapTests(unittest.TestCase):
    def test_radar_picks_the_team_used_this_hour_are_skipped(self):
        strategy = {'kind': 'concern_radar', 'angles': [{'fact_ids': ['busy', 'fresh']}]}
        used = ({}, {'busy': 5}, {'busy': 3})
        asked = []

        def by_ids(db_path, ids):
            asked.append(list(ids))
            return [{'id': i, 'concerns': []} for i in ids]
        with patch('ctn_data.fact_bank.usage', return_value=used), patch('ctn_data.fact_bank.by_ids', by_ids), \
                patch('ctn_data.fact_bank.facts_for', return_value=[]):
            facts = agent_pipeline.offered_facts('db', [], strategy, agent_id='maya')
        self.assertEqual(asked, [['fresh']])
        self.assertEqual([f['id'] for f in facts], ['fresh'])


if __name__ == '__main__':
    unittest.main()
