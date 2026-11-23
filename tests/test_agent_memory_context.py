"""Recall and prompt-budget regressions for long-running role memory."""
import copy
import json
import unittest

from ctn_data import agent_memory as memory
from ctn_data import agent_memory_archive as archive
from ctn_data.db import connect


class AgentMemoryContextTests(unittest.TestCase):
    def setUp(self):
        self.now = 1792452000
        self.agent = {'id': 'role', 'account_id': 77}
        self.data = {'agents': {'role': self.agent.copy()}, 'drafts': {}, 'memory': {
            'role': {'episodes': {'readings': [], 'decisions': [], 'outcomes': []},
                     'subjective': {'viewpoints': [], 'questions': [], 'people': []}}}}

    def reading(self, number=1, author=18, text='A ferry timetable discussion.', at=None):
        return {'source_id': f'post:{number}', 'version': 'v1', 'post_id': number,
                'author_id': author, 'excerpt': text, 'coverage': 'excerpt',
                'last_read_at': memory._at(self.now if at is None else at),
                'read_count': 1, 'epistemic_status': 'observed_quote'}

    def note(self, category, number=1, *, text=None, at=None, sources=None, author=18):
        evidence = sources or [self.reading(number, author)]
        return {'id': f'{category}:{number}', 'at': memory._at(self.now if at is None else at),
                'text': text or f'My {category} thought {number}.',
                'author_id': author if category == 'people' else None,
                'account_id': author if category == 'people' else None,
                'source_ids': [row['source_id'] for row in evidence],
                'evidence': copy.deepcopy(evidence), 'is_action_intention': False,
                'epistemic_status': 'subjective_impression'}

    def view(self, *, sources=None, recalled=None):
        return memory.context_for(self.data, self.agent, sources or [], self.now, recalled=recalled)

    def populate_large_memory(self):
        own = self.data['memory']['role']
        own['episodes']['readings'] = [self.reading(number, 100 + number, 'Public excerpt ' * 40)
                                      for number in range(1, 31)]
        for category in ('viewpoints', 'questions', 'people'):
            own['subjective'][category] = [self.note(category, number,
                text=(f'{category} thought {number}. ' * 30)[:320],
                sources=[self.reading(number * 10 + offset, 100 + number,
                    ('Read quotation ' + str(offset) + '. ') * 30) for offset in range(4)],
                author=100 + number) for number in range(1, 13)]
        own['episodes']['outcomes'] = [
            {'draft_id': f'completed:{number}', 'action': 'reply', 'state': 'published',
             'body': 'An actual published answer. ' * 20, 'at': memory._at(self.now),
             'epistemic_status': 'confirmed_delivery'} for number in range(12)]

    def test_large_evidence_keeps_views_questions_and_people_within_budget(self):
        self.populate_large_memory()
        before = copy.deepcopy(self.data)
        view = self.view()
        self.assertLessEqual(len(json.dumps(view, ensure_ascii=False)), memory.MAX_CONTEXT_CHARACTERS)
        for category in ('viewpoints', 'questions', 'people'):
            self.assertTrue(view['subjective_notes'][category], category)
        self.assertTrue(view['observed_reading'])
        self.assertTrue(view['completed_actions'])
        self.assertEqual(self.data, before)

    def test_relevant_old_opinion_wins_over_recent_unrelated_notes(self):
        self.populate_large_memory()
        old = self.note('viewpoints', 999, text='The ferry timetable matters to me.',
            at=self.now - 86400 * 90, sources=[self.reading(999, 18)], author=18)
        view = self.view(sources=[{'id': 'post:new', 'author_id': 18, 'text': 'ferry timetable'}],
                         recalled={'viewpoints': [old]})
        self.assertEqual(view['subjective_notes']['viewpoints'][0]['text'], old['text'])
        self.assertEqual(view['subjective_notes']['viewpoints'][0]['memory_origin'], 'archive')

    def test_shared_source_quote_is_serialized_once_across_impressions(self):
        quote = 'UNIQUE-HISTORICAL-QUOTE about printed ferry access.'
        evidence = [self.reading(1, text=quote)]
        own = self.data['memory']['role']
        for category in ('viewpoints', 'questions', 'people'):
            own['subjective'][category] = [self.note(category, sources=evidence)]
        view = self.view()
        self.assertEqual(len(view['evidence_sources']), 1)
        self.assertEqual(json.dumps(view).count('UNIQUE-HISTORICAL-QUOTE'), 1)
        references = {view['subjective_notes'][key][0]['evidence'][0]['ref']
                      for key in ('viewpoints', 'questions', 'people')}
        self.assertEqual(references, set(view['evidence_sources']))

    def test_reading_quote_is_not_repeated_in_note_evidence_catalog(self):
        reading = self.reading(text='UNIQUE-READING-QUOTE about the ferry.')
        self.data['memory']['role']['episodes']['readings'] = [reading]
        self.data['memory']['role']['subjective']['viewpoints'] = [
            self.note('viewpoints', sources=[reading])]
        view = self.view()
        self.assertEqual(json.dumps(view).count('UNIQUE-READING-QUOTE'), 1)
        self.assertTrue(next(iter(view['evidence_sources'].values()))['reading_in_context'])

    def test_current_source_is_not_requoted_as_old_note_evidence(self):
        source = {'id': 'post:1', 'post_id': 1, 'author_id': 18, 'version': 'v1',
                  'text': 'CURRENT-SOURCE-QUOTE about the ferry.'}
        memory.record_read(self.data, 'role', [source], self.now)
        evidence = self.data['memory']['role']['episodes']['readings'][0]
        self.data['memory']['role']['subjective']['viewpoints'] = [
            self.note('viewpoints', sources=[evidence])]
        view = self.view(sources=[source])
        self.assertEqual(view['observed_reading'], [])
        self.assertNotIn('CURRENT-SOURCE-QUOTE', json.dumps(view))
        self.assertTrue(next(iter(view['evidence_sources'].values()))['current_source_in_context'])

    def test_archive_merge_deduplicates_and_respects_live_delivery_state(self):
        reading = self.reading()
        self.data['memory']['role']['episodes']['readings'] = [reading]
        self.data['drafts']['old-action'] = {'id': 'old-action', 'agent_id': 'role',
            'action': 'reply', 'state': 'failed', 'created_at': memory._at(self.now)}
        recalled = {'readings': [reading, self.reading(2)], 'outcomes': [
            {'draft_id': 'old-action', 'state': 'published', 'action': 'reply',
             'at': memory._at(self.now - 100), 'body': 'An old stored outcome.'}]}
        view = self.view(recalled=recalled)
        self.assertEqual({row['source_id'] for row in view['observed_reading']}, {'post:1', 'post:2'})
        self.assertEqual(view['completed_actions'], [])
        self.assertEqual(view['other_outcomes'][0]['state'], 'failed')

    def test_person_history_is_dated_and_does_not_replace_current_impression(self):
        current = self.note('people', text='Their current explanation is clearer.')
        old = self.note('people', text='Their old claim left me doubtful.', at=self.now - 86400)
        self.data['memory']['role']['subjective']['people'] = [current]
        recalled = {'people': [old, {**current, 'archive_id': 'duplicate'}]}
        view = self.view(recalled=recalled)
        people = view['subjective_notes']['people']
        self.assertEqual(len(people), 2)
        self.assertEqual(people[0]['text'], current['text'])
        self.assertFalse(people[0]['historical'])
        self.assertTrue(people[1]['historical'])
        self.assertEqual(people[1]['at'], old['at'])

    def test_reset_blocks_old_archive_except_unresolved_delivery(self):
        self.data['memory']['role']['reset_at'] = memory._at(self.now)
        old_at = self.now - 86400
        recalled = {'readings': [self.reading(at=old_at)],
            'people': [self.note('people', at=old_at)],
            'viewpoints': [self.note('viewpoints', at=old_at)],
            'outcomes': [{'draft_id': 'published', 'state': 'published', 'at': memory._at(old_at)},
                         {'draft_id': 'uncertain', 'state': 'uncertain', 'at': memory._at(old_at)}]}
        view = self.view(recalled=recalled)
        self.assertEqual(view['observed_reading'], [])
        self.assertEqual(view['subjective_notes']['people'], [])
        self.assertEqual(view['subjective_notes']['viewpoints'], [])
        self.assertEqual(view['completed_actions'], [])
        self.assertEqual(view['uncertain_actions'][0]['draft_id'], 'uncertain')

    def test_wrong_role_archive_and_raw_reflections_are_not_model_memory(self):
        foreign = self.reading(text='A different role secret.', at=self.now)
        foreign['agent_id'] = 'other-role'
        view = self.view(recalled={'readings': [foreign],
            'decisions': [{'reflection': 'I have already posted this!', 'at': memory._at(self.now)}]})
        serialized = json.dumps(view)
        self.assertNotIn('A different role secret.', serialized)
        self.assertNotIn('I have already posted this!', serialized)

    def test_escaped_summary_cannot_remove_pending_states_or_entire_note_category(self):
        self.populate_large_memory()
        own = self.data['memory']['role']
        own.update(summary='\x01' * 4000, summary_source='operator')
        for number in range(4):
            self.data['drafts'][f'pending:{number}'] = {
                'id': f'pending:{number}', 'agent_id': 'role', 'action': 'reply',
                'state': 'pending', 'body': 'Planned response ' * 40,
                'created_at': memory._at(self.now)}
        own['episodes']['outcomes'].extend({'draft_id': f'uncertain:{number}',
            'action': 'reply', 'state': 'uncertain', 'at': memory._at(self.now),
            'body': 'Unconfirmed response ' * 40, 'epistemic_status': 'unconfirmed_delivery'}
            for number in range(4))
        before = copy.deepcopy(self.data)
        view = self.view()
        self.assertLessEqual(len(json.dumps(view, ensure_ascii=False)), memory.MAX_CONTEXT_CHARACTERS)
        self.assertEqual({row['draft_id'] for row in view['pending_intentions']},
                         {f'pending:{number}' for number in range(4)})
        self.assertEqual({row['draft_id'] for row in view['uncertain_actions']},
                         {f'uncertain:{number}' for number in range(4)})
        for category in ('viewpoints', 'questions', 'people'):
            self.assertTrue(view['subjective_notes'][category])
        self.assertTrue(view['subjective_notes']['summary_truncated'])
        self.assertEqual(self.data, before)

    def test_public_notes_keep_full_evidence_and_explicit_category(self):
        note = self.note('questions')
        self.data['memory']['role']['subjective']['questions'] = [note]
        public = memory.public_view(self.data, self.agent)
        row = public['subjective_notes'][0]
        self.assertEqual(row['kind'], 'questions')
        self.assertEqual(row['evidence'], note['evidence'])

    def test_unbounded_legacy_numeric_ids_cannot_break_prompt_budget(self):
        self.populate_large_memory()
        huge = int('9' * 2000)
        own = self.data['memory']['role']
        for row in own['episodes']['readings']:
            row.update(author_id=huge, post_id=huge)
        for category in ('viewpoints', 'questions', 'people'):
            for row in own['subjective'][category]:
                row.update(author_id=huge, account_id=huge)
                for evidence in row['evidence']:
                    evidence.update(author_id=huge, post_id=huge)
        before = copy.deepcopy(self.data)
        view = self.view()
        serialized = json.dumps(view, ensure_ascii=False)
        self.assertLessEqual(len(serialized), memory.MAX_CONTEXT_CHARACTERS)
        self.assertNotIn('9' * 2000, serialized)
        for category in ('viewpoints', 'questions', 'people'):
            self.assertTrue(view['subjective_notes'][category])
        self.assertEqual(self.data, before)

    def test_persistent_archive_recalls_person_after_working_memory_eviction(self):
        conn = connect(':memory:')
        self.addCleanup(conn.close)
        with conn:
            archive.initialize(conn)
        for number in range(1, 211):
            source = {'id': f'post:{number}', 'post_id': number, 'version': 'v1',
                'author_id': 18 if number == 1 else 100 + number,
                'text': ('The old harbour ramp needs a printed ferry timetable.' if number == 1
                         else 'An unrelated football match and season result.'), 'kind': 'post'}
            memory.record_read(self.data, 'role', [source], self.now + number)
            draft = {'id': f'draft:{number}', 'agent_id': 'role', 'sources': [source],
                     'created_at': memory._at(self.now + number)}
            updates = {}
            for category in ('viewpoints', 'questions', 'people'):
                note = {'text': (f'My old harbour {category} memory.' if number == 1
                                 else f'Football {category} impression number {number}.'),
                        'source_ids': [source['id']]}
                if category == 'people':
                    note['account_id'] = source['author_id']
                updates[category] = [note]
            memory.record_subjective_updates(self.data, draft, updates, self.now + number)
        own = self.data['memory']['role']
        self.assertFalse(any(row['source_id'] == 'post:1' for row in own['episodes']['readings']))
        self.assertFalse(any(row.get('account_id') == 18 for row in own['subjective']['people']))
        with conn:
            archive.capture(conn, self.data)
        current = [{'id': 'post:9999', 'post_id': 9999, 'author_id': 18,
                    'text': 'Can we revisit that harbour ramp and printed ferry timetable?'}]
        recalled = archive.recall(conn, self.data, self.agent, current, self.now + 220)
        view = memory.context_for(self.data, self.agent, current, self.now + 220, recalled=recalled)
        self.assertGreater(archive.stats(conn, self.data, 'role')['counts']['reading'], memory.MAX_READINGS)
        self.assertTrue(any(row['source_id'] == 'post:1' for row in view['observed_reading']))
        person = next(row for row in view['subjective_notes']['people'] if row.get('account_id') == 18)
        self.assertIn('old harbour', person['text'])
        self.assertTrue(person['historical'])
        for category in ('viewpoints', 'questions', 'people'):
            self.assertTrue(view['subjective_notes'][category], category)
        self.assertLessEqual(len(json.dumps(view, ensure_ascii=False)), memory.MAX_CONTEXT_CHARACTERS)


if __name__ == '__main__':
    unittest.main()
