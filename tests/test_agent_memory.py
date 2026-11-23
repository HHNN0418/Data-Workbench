"""Experience memory is local, bounded, and honest about action outcomes."""
import copy
import json
import unittest

from ctn_data import agent_memory as memory


class AgentMemoryTests(unittest.TestCase):
    def setUp(self):
        self.data = {'memory': {}, 'drafts': {}}
        self.agent = {'id': 'maya', 'account_id': 77}
        self.now = 1792452000

    def source(self, number=1, **changes):
        return {'id': f'post:{number}', 'post_id': number, 'author_id': 18,
                'title': '@library_reader', 'text': 'The local library needs better ferry access.',
                'version': 'v1', 'origin': 'notification', 'source_role': 'notification',
                'observed_at': self.now, 'notification_ids': [number], **changes}

    def draft(self, number=1, **changes):
        return {'id': f'draft:{number}', 'agent_id': 'maya', 'action': 'reply', 'state': 'pending',
                'body': 'I wonder whether the ferry timetable could help library visitors.',
                'target_post_id': number, 'post_id': None, 'created_at': self.now,
                'source_ids': [f'post:{number}'], 'sources': [self.source(number)],
                'reason': 'This relates to access to our local library.', **changes}

    def view(self, sources=None):
        return memory.context_for(self.data, self.agent, sources or [], self.now)

    def test_readings_keep_actual_quotes_and_versions_without_assuming_truth(self):
        source = self.source(text='I heard the ferry is closing. Is this true?')
        memory.record_read(self.data, 'maya', [source], self.now)
        memory.record_read(self.data, 'maya', [source], self.now + 10)
        memory.record_read(self.data, 'maya', [{**source, 'version': 'v2', 'text': 'The ferry remains open.'}], self.now + 20)
        rows = self.data['memory']['maya']['episodes']['readings']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['read_count'], 2)
        self.assertEqual(rows[0]['first_read_at'], memory._at(self.now))
        self.assertEqual(rows[0]['last_read_at'], memory._at(self.now + 10))
        self.assertEqual(rows[0]['epistemic_status'], 'observed_quote')
        self.assertIn('Is this true?', rows[0]['excerpt'])

    def test_old_sources_without_version_deduplicate_by_content(self):
        source = self.source()
        source.pop('version')
        memory.record_read(self.data, 'maya', [source], self.now)
        memory.record_read(self.data, 'maya', [source], self.now + 1)
        self.assertEqual(len(self.view()['observed_reading']), 1)
        self.assertEqual(self.view()['observed_reading'][0]['read_count'], 2)

    def test_newly_seen_current_quotes_are_not_repeated_in_model_context(self):
        source = self.source()
        memory.record_read(self.data, 'maya', [source], self.now)
        self.assertEqual(self.view([source])['observed_reading'], [])
        memory.record_read(self.data, 'maya', [source], self.now + 1)
        self.assertEqual(self.view([source])['observed_reading'], [])
        self.assertEqual(self.data['memory']['maya']['episodes']['readings'][0]['read_count'], 2)

    def test_current_edited_post_preserves_memory_of_its_old_wording(self):
        old = self.source(text='The ferry timetable has not been published.', version='old')
        new = self.source(text='The updated ferry timetable is now available.', version='new')
        memory.record_read(self.data, 'maya', [old], self.now - 60)
        memory.record_read(self.data, 'maya', [new], self.now)
        reading = self.view([new])['observed_reading']
        self.assertEqual(len(reading), 1)
        self.assertEqual(reading[0]['version'], 'old')
        self.assertEqual(reading[0]['excerpt'], old['text'])
        self.assertEqual(reading[0]['read_count'], 1)

    def test_first_new_reading_preserves_retained_legacy_batch(self):
        old = self.source(1)
        new = self.source(2, text='A new discussion about the library.')
        self.data['memory']['maya'] = {'last_sources': [old], 'last_read_at': memory._at(self.now - 60)}
        memory.record_read(self.data, 'maya', [new], self.now)
        # Simulate the observer replacing its temporary most-recent source list.
        self.data['memory']['maya'].update(last_sources=[new], last_read_at=memory._at(self.now))
        reading = self.view([new])['observed_reading']
        self.assertEqual(len(reading), 1)
        self.assertEqual(reading[0]['source_id'], old['id'])
        self.assertEqual(reading[0]['last_read_at'], memory._at(self.now - 60))
        episodes = self.data['memory']['maya']['episodes']['readings']
        self.assertEqual({row['source_id'] for row in episodes}, {old['id'], new['id']})

    def test_legacy_reading_is_not_reimported_on_each_new_batch(self):
        old = self.source(1)
        self.data['memory']['maya'] = {'last_sources': [old], 'last_read_at': memory._at(self.now - 60)}
        memory.record_read(self.data, 'maya', [self.source(2)], self.now)
        memory.record_read(self.data, 'maya', [self.source(3)], self.now + 1)
        readings = self.data['memory']['maya']['episodes']['readings']
        self.assertEqual(len(readings), 3)
        self.assertEqual(next(row for row in readings if row['source_id'] == old['id'])['read_count'], 1)

    def test_generated_delivery_claim_stays_in_audit_and_not_model_memory(self):
        draft = self.draft()
        self.data['drafts'][draft['id']] = draft
        memory.record_decision(self.data, draft, {'memory': 'I have already posted this.'}, self.now)
        view = self.view()
        self.assertEqual(view['completed_actions'], [])
        self.assertEqual(view['pending_intentions'][0]['draft_id'], draft['id'])
        self.assertEqual(view['pending_intentions'][0]['epistemic_status'], 'operational_intention')
        self.assertNotIn('I have already posted this.', json.dumps(view))
        self.assertEqual(self.data['memory']['maya']['episodes']['decisions'][0]['reflection'], 'I have already posted this.')
        self.assertIn('I have already posted this.', json.dumps(memory.public_view(self.data, self.agent)))
        self.assertEqual(view['subjective_notes']['epistemic_status'], 'self_reported_thoughts')

    def test_uncertain_and_failed_attempts_are_never_completed_actions(self):
        for number, state in enumerate(('sending', 'uncertain', 'rejected', 'failed', 'discarded'), start=1):
            draft = self.draft(number, state=state)
            self.data['drafts'][draft['id']] = draft
            memory.record_outcome(self.data, draft, state, 'No confirmed delivery.', self.now + number)
        view = self.view()
        self.assertEqual(view['completed_actions'], [])
        self.assertEqual({row['state'] for row in view['uncertain_actions']}, {'sending', 'uncertain'})
        self.assertEqual({row['state'] for row in view['other_outcomes']}, {'rejected', 'failed', 'discarded'})
        self.assertTrue(all(not row['confirmed_interactions'] for row in view['relationships']))

    def test_reconciliation_updates_one_outcome_and_one_contact_interaction(self):
        draft = self.draft()
        memory.record_read(self.data, 'maya', draft['sources'], self.now)
        memory.record_decision(self.data, draft, {'memory': 'I would like to ask about ferry times.'}, self.now)
        memory.record_outcome(self.data, draft, 'uncertain', 'Request timed out.', self.now + 1)
        draft.update(state='published', post_id=900, published_at=self.now + 2)
        self.data['drafts'][draft['id']] = draft
        memory.record_outcome(self.data, draft, 'published', 'Verified remotely.', self.now + 2)
        memory.record_outcome(self.data, draft, 'published', 'Verified again.', self.now + 3)
        view = self.view()
        self.assertEqual(len(view['completed_actions']), 1)
        self.assertEqual(view['completed_actions'][0]['post_id'], 900)
        self.assertEqual(view['completed_actions'][0]['epistemic_status'], 'confirmed_delivery')
        self.assertEqual(view['completed_actions'][0]['state_history'][0]['state'], 'uncertain')
        self.assertEqual(view['pending_intentions'], [])
        self.assertEqual(len(view['relationships'][0]['confirmed_interactions']), 1)
        self.assertNotIn('stance', view['relationships'][0])
        self.assertNotIn('friendship', view['relationships'][0])

    def test_successful_like_remembers_no_authored_body_or_new_post(self):
        draft = self.draft(action='like', body='', state='published')
        memory.record_outcome(self.data, draft, 'published', 'Liked.', self.now)
        row = self.view()['completed_actions'][0]
        self.assertEqual(row['body'], '')
        self.assertIsNone(row['post_id'])
        self.assertEqual(row['target_post_id'], 1)
        self.assertEqual(row['action'], 'like')

    def test_live_durable_state_overrides_old_episode(self):
        draft = self.draft(state='published', post_id=4)
        memory.record_outcome(self.data, draft, 'published', 'Done.', self.now)
        self.data['drafts'][draft['id']] = {**draft, 'state': 'uncertain'}
        view = self.view()
        self.assertEqual(view['completed_actions'], [])
        self.assertEqual(view['uncertain_actions'][0]['epistemic_status'], 'unconfirmed_delivery')

    def test_discarded_intention_is_not_kept_as_an_unfinished_plan(self):
        draft = self.draft()
        memory.record_decision(self.data, draft, {}, self.now)
        self.data['drafts'][draft['id']] = {**draft, 'state': 'discarded'}
        self.assertEqual(self.view()['pending_intentions'], [])

    def test_retrieval_prefers_people_and_topics_in_current_reading(self):
        memory.record_read(self.data, 'maya', [self.source(number, author_id=number + 100,
                           text='A completely unrelated discussion of football.') for number in range(1, 19)], self.now)
        memory.record_read(self.data, 'maya', [self.source(25, author_id=18)], self.now - 86400)
        current = self.source(100, author_id=18)
        view = self.view([current])
        self.assertEqual(view['observed_reading'][0]['post_id'], 25)
        self.assertEqual(view['relationships'][0]['author_id'], 18)

    def test_fictional_future_publication_time_does_not_replace_reading_time(self):
        source = self.source(id='news:future', post_id=None, author_id=None, kind='news',
                             published_at='2099-01-01T00:00:00Z', created_at='')
        memory.record_read(self.data, 'maya', [source], self.now)
        row = self.view()['observed_reading'][0]
        self.assertEqual(row['source_created_at'], '2099-01-01T00:00:00Z')
        self.assertEqual(row['last_read_at'], memory._at(self.now))

    def test_relevant_older_relationship_survives_many_recent_contacts(self):
        memory.record_read(self.data, 'maya', [self.source(author_id=18)], self.now - 86400)
        for number in range(2, 90):
            memory.record_read(self.data, 'maya', [self.source(number, author_id=number + 100)], self.now)
        self.assertEqual(self.view([self.source(100, author_id=18)])['relationships'][0]['author_id'], 18)

    def test_existing_activity_and_retained_source_text_are_available_without_migration(self):
        self.data['memory']['maya'] = {'summary': 'I worry about transport access.', 'summary_source': 'operator',
            'last_read_at': memory._at(self.now), 'last_sources': [self.source()],
            'read_ids': ['post:1', 'post:999999'],
            'activity': [{'draft_id': 'legacy', 'action': 'post', 'state': 'published',
                          'body': 'My earlier actual post.', 'at': memory._at(self.now)}]}
        before = copy.deepcopy(self.data)
        view = self.view()
        self.assertEqual(view['observed_reading'][0]['source_id'], 'post:1')
        self.assertNotIn('999999', json.dumps(view))
        self.assertEqual(view['completed_actions'][0]['body'], 'My earlier actual post.')
        self.assertEqual(view['subjective_notes']['operator_notes'], 'I worry about transport access.')
        self.assertEqual(self.data, before)

    def test_empty_and_old_string_memory_are_compatible(self):
        before = copy.deepcopy(self.data)
        self.assertEqual(self.view()['completed_actions'], [])
        self.assertEqual(self.data, before)
        self.data['memory']['maya'] = 'Older personal notes.'
        notes = self.view()['subjective_notes']
        self.assertEqual(notes['summary'], 'Older personal notes.')
        self.assertEqual(notes['summary_source'], 'legacy_unknown')
        self.assertEqual(notes['operator_notes'], '')
        self.assertIn('Older personal notes.', json.dumps(memory.public_view(self.data, self.agent)))
        memory.record_read(self.data, 'maya', [self.source()], self.now)
        self.assertEqual(self.data['memory']['maya']['summary'], 'Older personal notes.')

    def test_ledger_and_model_context_are_bounded_without_model_or_network(self):
        for index in range(260):
            source = self.source(index + 1, text='图书馆 公交 接驳 值得关心 ' * 200)
            memory.record_read(self.data, 'maya', [source], self.now + index)
            draft = self.draft(index + 1, body='The library needs better transport. ' * 80,
                               state='published', reason='A relevant issue. ' * 100,
                               sources=[source])
            memory.record_decision(self.data, draft, {'memory': 'My thoughts. ' * 500}, self.now + index)
            memory.record_outcome(self.data, draft, 'published', 'Completed. ' * 100, self.now + index)
        episodes = self.data['memory']['maya']['episodes']
        self.assertEqual(len(episodes['readings']), memory.MAX_READINGS)
        self.assertEqual(len(episodes['decisions']), memory.MAX_DECISIONS)
        self.assertEqual(len(episodes['outcomes']), memory.MAX_OUTCOMES)
        self.assertLessEqual(len(json.dumps(self.view([self.source(999)]), ensure_ascii=False)), memory.MAX_CONTEXT_CHARACTERS)
        self.assertLessEqual(len(json.dumps(episodes, ensure_ascii=False)), 1000000)

    def test_public_view_exposes_inspectable_lists_and_distinguishes_notes(self):
        source = self.source()
        memory.record_read(self.data, 'maya', [source], self.now)
        draft = self.draft()
        memory.record_decision(self.data, draft, {'memory': 'I plan to ask politely.'}, self.now)
        self.data['memory']['maya']['summary'] = 'I care about this community.'
        public = memory.public_view(self.data, self.agent)
        self.assertEqual(public['observed_reading'][0]['at'], memory._at(self.now))
        self.assertEqual(public['observed_reading'][0]['excerpt'], source['text'])
        self.assertEqual(public['pending_intentions'][0]['id'], draft['id'])
        self.assertIn('与账号 18', public['relationships'][0]['title'])
        self.assertEqual(public['subjective_notes'][0]['excerpt'], 'I care about this community.')
        self.assertTrue(all(isinstance(public[key], list) for key in ('observed_reading', 'completed_actions',
                             'pending_intentions', 'uncertain_actions', 'relationships', 'subjective_notes')))
        self.assertEqual(public['completed_actions'], [])

    def test_read_source_and_output_inputs_are_not_mutated(self):
        source, draft = self.source(), self.draft()
        output = {'memory': 'A thought.'}
        copies = copy.deepcopy((source, draft, output))
        memory.record_read(self.data, 'maya', [source], self.now)
        memory.record_decision(self.data, draft, output, self.now)
        memory.record_outcome(self.data, draft, 'published', 'Done.', self.now)
        memory.public_view(self.data, self.agent)
        self.assertEqual((source, draft, output), copies)

    def test_reset_forgets_only_selected_role_experiences_and_keeps_operational_state(self):
        self.data.update(agents={'maya': {**self.agent, 'background': 'Local librarian.',
                                        'provider_id': 'configured', 'enabled': False,
                                        'last_read_at': memory._at(self.now), 'last_read_count': 1},
                                 'ethan': {'id': 'ethan', 'background': 'Another role.'}},
                         events=[{'agent_id': 'maya', 'kind': 'read'}],
                         rate={'accounts': {'maya': {'attempts': 2}}},
                         leases={'agent:maya': {'token': 'protected', 'until': self.now + 100}})
        draft = self.draft(state='uncertain')
        self.data['drafts'][draft['id']] = draft
        memory.record_read(self.data, 'maya', [self.source()], self.now)
        memory.record_decision(self.data, draft, {'memory': 'Old thoughts.'}, self.now)
        memory.record_outcome(self.data, draft, 'uncertain', 'Needs verification.', self.now)
        self.data['memory']['ethan'] = {'summary': 'Preserve another role.', 'read_ids': ['post:2']}
        old_memory = self.data['memory']['maya']
        old_memory.update(summary='Old summary.', read_ids=['post:1'], read_versions={'post:1': 'v1'},
                          read_notification_ids=[1], decision_versions={'post:1': 'v1'},
                          decision_notification_ids=[1], news_versions={'news:1': 'v1'},
                          last_sources=[self.source()], home_sources=[self.source()],
                          last_read_at=memory._at(self.now), last_decision_at=memory._at(self.now),
                          activity=[{'draft_id': draft['id'], 'state': 'uncertain'}],
                          inbox=[{'id': 1, 'status': 'unresolved'}, {'id': 2, 'status': 'resolved'}],
                          notification_outbox={'1': {'notification_id': 1, 'state': 'pending'}},
                          observation={'observed_at': memory._at(self.now), 'source_count': 1,
                                       'new_source_count': 1, 'latest_post_at': memory._at(self.now),
                                       'latest_news_at': memory._at(self.now), 'home_next_at': self.now + 60})
        before = copy.deepcopy(self.data)
        result = memory.reset(self.data, 'maya', self.now + 1)
        self.assertEqual(result['reset_at'], memory._at(self.now + 1))
        for key in ('summary', 'read_ids', 'read_versions', 'read_notification_ids',
                    'decision_versions', 'decision_notification_ids', 'news_versions',
                    'last_sources', 'home_sources', 'activity'):
            self.assertFalse(result[key], key)
        self.assertEqual(result['episodes'], {'version': 1, 'readings': [], 'decisions': [], 'outcomes': []})
        self.assertNotIn('last_read_at', result)
        self.assertNotIn('last_decision_at', result)
        self.assertNotIn('last_read_at', self.data['agents']['maya'])
        self.assertEqual(self.data['agents']['maya']['last_read_count'], 0)
        self.assertEqual(result['observation']['source_count'], 0)
        self.assertEqual(result['observation']['home_next_at'], self.now + 60)
        for key in ('inbox', 'notification_outbox'):
            self.assertEqual(result[key], before['memory']['maya'][key])
        for key in ('drafts', 'events', 'rate', 'leases'):
            self.assertEqual(self.data[key], before[key])
        self.assertEqual(self.data['memory']['ethan'], before['memory']['ethan'])
        self.assertEqual(self.data['agents']['ethan'], before['agents']['ethan'])
        expected_role = before['agents']['maya']
        expected_role.pop('last_read_at')
        expected_role['last_read_count'] = 0
        self.assertEqual(self.data['agents']['maya'], expected_role)

    def test_reset_cutoff_prevents_retained_drafts_and_legacy_history_reappearing(self):
        self.data['agents'] = {'maya': self.agent.copy()}
        for number, state in enumerate(('published', 'discarded', 'pending', 'skipped'), start=1):
            draft = self.draft(number, state=state)
            self.data['drafts'][draft['id']] = draft
            memory.record_outcome(self.data, draft, state, 'Old history.', self.now)
        memory.reset(self.data, 'maya', self.now + 1)
        # Even an older-format cache accidentally restored after reset must not
        # import retained text or delivery audit back across the cutoff.
        self.data['memory']['maya'].update(last_sources=[self.source()], last_read_at=memory._at(self.now),
            activity=[{'draft_id': 'legacy', 'state': 'published', 'at': memory._at(self.now)}])
        before = copy.deepcopy(self.data)
        for view in (self.view(), memory.public_view(self.data, self.agent)):
            for category in ('observed_reading', 'completed_actions',
                             'other_outcomes', 'relationships'):
                self.assertEqual(view[category], [], category)
            self.assertEqual(len(view['pending_intentions']), 1)
            self.assertTrue(view['pending_intentions'][0]['predates_memory_reset'])
            self.assertTrue(view['pending_intentions'][0]['operational_only'])
        self.assertEqual(self.data, before)
        memory.record_read(self.data, 'maya', [self.source(20)], self.now + 2)
        readings = self.data['memory']['maya']['episodes']['readings']
        self.assertEqual([row['source_id'] for row in readings], ['post:20'])
        self.assertEqual(readings[0]['read_count'], 1)

    def test_reset_keeps_ambiguous_delivery_visible_and_allows_actual_new_experiences(self):
        self.data['agents'] = {'maya': self.agent.copy()}
        uncertain = self.draft(1, state='uncertain')
        sending = self.draft(2, state='sending')
        published = self.draft(3, state='published', published_at=self.now)
        for draft in (uncertain, sending, published):
            self.data['drafts'][draft['id']] = draft
        memory.reset(self.data, 'maya', self.now + 1)
        view = self.view()
        self.assertEqual({row['state'] for row in view['uncertain_actions']}, {'uncertain', 'sending'})
        self.assertEqual(view['completed_actions'], [])
        uncertain.update(state='published', post_id=901, published_at=self.now + 2)
        memory.record_outcome(self.data, uncertain, 'published', 'Verified after reset.', self.now + 2)
        fresh = self.draft(4, created_at=self.now + 2)
        memory.record_decision(self.data, fresh, {'memory': 'A new thought.'}, self.now + 2)
        self.assertEqual(self.view()['completed_actions'][0]['post_id'], 901)
        self.assertNotIn('A new thought.', json.dumps(self.view()))
        self.assertIn('A new thought.', json.dumps(memory.public_view(self.data, self.agent)))
        self.assertEqual(len(self.data['drafts']), 3)

    def test_reset_rejects_missing_role_or_invalid_cutoff_without_mutation(self):
        self.data['agents'] = {'maya': self.agent.copy()}
        before = copy.deepcopy(self.data)
        with self.assertRaises(KeyError):
            memory.reset(self.data, 'missing', self.now)
        with self.assertRaises(ValueError):
            memory.reset(self.data, 'maya', 'invalid')
        self.assertEqual(self.data, before)

    def test_discarded_draft_marks_its_notes_inactive_without_erasing_actual_reads(self):
        draft = self.draft()
        summary = 'I would like to ask about ferry access.'
        memory.record_read(self.data, 'maya', [self.source()], self.now)
        self.data['memory']['maya']['summary'] = summary
        memory.record_decision(self.data, draft, {'memory': summary}, self.now)
        self.data['drafts'][draft['id']] = draft
        notes = self.view()['subjective_notes']
        self.assertEqual(notes['summary'], summary)
        self.assertTrue(notes['summary_is_active_intention'])
        draft['state'] = 'discarded'
        memory.record_outcome(self.data, draft, 'discarded', 'Abandoned without delivery.', self.now + 1)
        view = self.view()
        self.assertEqual(view['pending_intentions'], [])
        self.assertEqual(view['completed_actions'], [])
        notes = view['subjective_notes']
        self.assertEqual(notes['summary'], summary)
        self.assertEqual(notes['summary_state'], 'discarded')
        self.assertEqual(notes['summary_intention_status'], 'abandoned')
        self.assertFalse(notes['summary_is_active_intention'])
        self.assertEqual(view['observed_reading'][0]['source_id'], 'post:1')
        public = memory.public_view(self.data, self.agent)
        self.assertEqual(len(public['subjective_notes']), 2)
        self.assertTrue(all('已放弃' in row['title'] for row in public['subjective_notes']))
        self.assertTrue(all(row['is_active_intention'] is False for row in public['subjective_notes']))

    def test_manually_edited_summary_is_not_bound_to_an_older_discarded_draft(self):
        draft = self.draft(state='discarded')
        self.data['memory']['maya'] = {'summary': 'I prepared a ferry question.'}
        memory.record_decision(self.data, draft, {'memory': 'I prepared a ferry question.'}, self.now)
        self.data['drafts'][draft['id']] = draft
        self.data['memory']['maya']['summary'] = 'The local library is important to me.'
        self.data['memory']['maya']['summary_source'] = 'operator'
        notes = self.view()['subjective_notes']
        self.assertEqual(notes['operator_notes'], 'The local library is important to me.')
        self.assertEqual(notes['operator_notes_source'], 'operator')

    def test_excerpt_metadata_preserves_reading_and_thread_boundaries(self):
        source = self.source(text='a' * 600, text_length=900, text_truncated=True,
            reply_to_post_id=8, root_post_id=7, post_kind='reply', url='https://example.test/posts/1')
        memory.record_read(self.data, 'maya', [source], self.now)
        row = self.view()['observed_reading'][0]
        self.assertEqual((len(row['excerpt']), row['excerpt_length']), (360, 360))
        self.assertEqual((row['supplied_text_length'], row['text_length']), (600, 900))
        self.assertEqual(row['coverage'], 'excerpt')
        self.assertTrue(row['text_truncated'])
        self.assertEqual((row['reply_to_post_id'], row['root_post_id'], row['post_kind']), (8, 7, 'reply'))
        self.assertEqual(row['url'], source['url'])
        memory.record_read(self.data, 'maya', [self.source(2, text='', coverage='title_only')], self.now)
        self.assertEqual(next(row for row in self.view()['observed_reading'] if row['post_id'] == 2)['coverage'], 'title_only')

    def test_structured_impressions_have_exact_evidence_without_creating_action_facts(self):
        draft = self.draft()
        update = {'viewpoints': [{'text': 'The ferry gap matters to me.', 'source_ids': ['post:1']}],
                  'questions': [{'text': 'Is there a printed timetable?', 'source_ids': ['post:1']}],
                  'people': [{'account_id': 18, 'text': 'Their careful questions make me want to read more.',
                              'source_ids': ['post:1'], 'evidence': [{'author_id': 999}]}]}
        memory.record_decision(self.data, draft, {'memory': 'I posted this already.', 'memory_updates': update}, self.now)
        draft['state'] = 'discarded'
        self.data['drafts'][draft['id']] = draft
        view = self.view()
        notes = view['subjective_notes']
        self.assertEqual(notes['viewpoints'][0]['text'], update['viewpoints'][0]['text'])
        self.assertEqual(notes['questions'][0]['text'], update['questions'][0]['text'])
        person = notes['people'][0]
        self.assertEqual(person['account_id'], 18)
        self.assertEqual(person['epistemic_status'], 'subjective_impression')
        self.assertFalse(person['is_action_intention'])
        self.assertEqual(person['evidence'][0]['author_id'], 18)
        evidence = view['evidence_sources'][person['evidence'][0]['ref']]
        self.assertEqual(evidence['excerpt'], draft['sources'][0]['text'])
        self.assertEqual(person['evidence'][0]['version'], 'v1')
        self.assertEqual(view['completed_actions'], [])
        self.assertEqual(view['pending_intentions'], [])
        self.assertNotIn('I posted this already.', json.dumps(view))

    def test_invented_missing_mismatched_or_self_impression_evidence_is_ignored(self):
        draft = self.draft(account_id=77)
        updates = {'people': [
            {'account_id': 99, 'text': 'Not the author read.', 'source_ids': ['post:1']},
            {'account_id': 18, 'text': 'An invented reference.', 'source_ids': ['post:404']},
            {'account_id': 77, 'text': 'My own profile.', 'source_ids': ['post:1']},
        ], 'questions': [{'text': 'No evidence.', 'source_ids': []}],
           'viewpoints': [{'text': 'A fake reference.', 'source_ids': [True]}]}
        memory.record_decision(self.data, draft, {'memory_updates': updates}, self.now)
        notes = self.view()['subjective_notes']
        self.assertTrue(all(not notes[key] for key in ('viewpoints', 'questions', 'people')))
        self.assertEqual(self.data['memory']['maya']['episodes']['decisions'][0]['draft_id'], draft['id'])

    def test_person_impression_survives_reading_eviction_and_updates_with_new_evidence(self):
        source = self.source()
        draft = self.draft(sources=[source])
        memory.record_read(self.data, 'maya', [source], self.now)
        memory.record_decision(self.data, draft, {'memory_updates': {'people': [
            {'account_id': 18, 'text': 'Careful about evidence.', 'source_ids': ['post:1']}]}}, self.now)
        for number in range(2, 190):
            memory.record_read(self.data, 'maya', [self.source(number, author_id=100 + number)], self.now + number)
        self.assertFalse(any(row['source_id'] == 'post:1' for row in self.data['memory']['maya']['episodes']['readings']))
        self.assertEqual(self.view()['subjective_notes']['people'][0]['evidence'][0]['source_id'], 'post:1')
        changed = self.draft(200, sources=[self.source(200)])
        memory.record_decision(self.data, changed, {'memory_updates': {'people': [
            {'account_id': 18, 'text': 'Their new claim leaves me doubtful.', 'source_ids': ['post:200']}]}}, self.now + 200)
        people = self.view()['subjective_notes']['people']
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]['text'], 'Their new claim leaves me doubtful.')
        self.assertEqual(people[0]['previous_text'], 'Careful about evidence.')
        self.assertEqual(people[0]['source_ids'], ['post:200'])

    def test_model_delivery_claim_remains_subjective_and_never_confirms_an_action(self):
        draft = self.draft()
        claim = 'I posted my short list instead.'
        self.data['memory']['maya'] = {'summary': claim}
        memory.record_decision(self.data, draft, {'memory': claim}, self.now)
        self.data['drafts'][draft['id']] = draft
        before = copy.deepcopy(self.data)
        view = self.view()
        self.assertEqual(view['subjective_notes']['summary'], claim)
        self.assertEqual(view['subjective_notes']['summary_source'], 'model')
        self.assertEqual(view['subjective_notes']['epistemic_status'], 'self_reported_thoughts')
        self.assertEqual(view['completed_actions'], [])
        self.assertEqual(view['pending_intentions'][0]['draft_id'], draft['id'])
        self.assertIn(claim, json.dumps(memory.public_view(self.data, self.agent)))
        self.assertEqual(self.data, before)

    def test_legacy_and_model_summaries_reach_context_in_full_without_migration(self):
        summary = ('我仍在思考图书馆与交通问题。' * 300)[:3980] + '最后的个人想法也要保留。'
        self.assertGreater(len(summary), 1400)
        for source in ('legacy_unknown', 'model'):
            with self.subTest(source=source):
                self.data = {'memory': {'maya': {'summary': summary}}, 'drafts': {}}
                if source == 'model':
                    memory.record_decision(self.data, self.draft(), {'memory': summary}, self.now)
                before = copy.deepcopy(self.data)
                view = self.view()
                self.assertEqual(view['subjective_notes']['summary'], summary)
                self.assertEqual(view['subjective_notes']['summary_source'], source)
                self.assertEqual(view['subjective_notes']['operator_notes'], '')
                self.assertEqual(view['completed_actions'], [])
                self.assertLessEqual(len(json.dumps(view, ensure_ascii=False)), memory.MAX_CONTEXT_CHARACTERS)
                self.assertEqual(self.data, before)

    def test_operator_summary_keeps_full_text_and_compatible_short_note(self):
        summary = ('I care about the library and transport access. ' * 100)[:4000]
        self.data['memory']['maya'] = {'summary': summary, 'summary_source': 'operator'}
        memory.record_decision(self.data, self.draft(), {'memory': 'A different model reflection.'}, self.now)
        before = copy.deepcopy(self.data)
        notes = self.view()['subjective_notes']
        self.assertEqual(notes['summary'], summary)
        self.assertEqual(notes['summary_source'], 'operator')
        self.assertEqual(notes['operator_notes'], summary[:1400])
        self.assertEqual(notes['operator_notes_source'], 'operator')
        self.assertEqual(self.data, before)

    def test_restored_summary_obeys_context_budget_with_readings_and_json_escapes(self):
        summaries = [
            ('我对这些讨论的想法需要连续保存。' * 300)[:4000],
            '"' * 4000,
            '\x01' * 4000,
        ]
        for summary in summaries:
            with self.subTest(character=repr(summary[0])):
                self.data = {'memory': {}, 'drafts': {}}
                for number in range(1, 12):
                    memory.record_read(self.data, 'maya',
                        [self.source(number, text='值得继续思考的公开讨论。' * 100)], self.now + number)
                self.data['memory']['maya'].update(summary=summary, summary_source='operator')
                before = copy.deepcopy(self.data)
                view = self.view()
                notes = view['subjective_notes']
                self.assertLessEqual(len(json.dumps(view, ensure_ascii=False)), memory.MAX_CONTEXT_CHARACTERS)
                self.assertTrue(notes['summary'])
                self.assertTrue(summary.startswith(notes['summary']))
                if summary[0] == '我':
                    self.assertEqual(notes['summary'], summary)
                elif len(notes['summary']) < len(summary):
                    self.assertTrue(notes['summary_truncated'])
                self.assertEqual(self.data, before)

    def test_reset_does_not_recover_cleared_summaries_from_retained_drafts(self):
        old_summary = 'Forgotten personal thoughts about a ferry journey.'
        for source in ('legacy_unknown', 'model', 'operator'):
            with self.subTest(source=source):
                self.data = {'agents': {'maya': self.agent.copy()}, 'memory': {}, 'drafts': {}}
                draft = self.draft(reason=old_summary, body='A retained unpublished draft.')
                self.data['drafts'][draft['id']] = draft
                self.data['memory']['maya'] = {'summary': old_summary}
                if source == 'operator':
                    self.data['memory']['maya']['summary_source'] = 'operator'
                elif source == 'model':
                    memory.record_decision(self.data, draft, {'memory': old_summary}, self.now)
                memory.reset(self.data, 'maya', self.now + 1)
                memory.record_read(self.data, 'maya', [self.source(20)], self.now + 2)
                before = copy.deepcopy(self.data)
                view = self.view()
                self.assertEqual(view['subjective_notes']['summary'], '')
                self.assertEqual(view['subjective_notes']['operator_notes'], '')
                self.assertNotIn(old_summary, json.dumps(view, ensure_ascii=False))
                self.assertNotIn(old_summary, json.dumps(memory.public_view(self.data, self.agent), ensure_ascii=False))
                self.assertEqual(view['completed_actions'], [])
                self.assertTrue(view['pending_intentions'][0]['predates_memory_reset'])
                self.assertEqual(self.data, before)

    def test_reset_clears_subjective_people_and_operator_notes_but_preserves_live_outbox(self):
        self.data['agents'] = {'maya': self.agent.copy()}
        draft = self.draft()
        self.data['drafts'][draft['id']] = draft
        memory.record_read(self.data, 'maya', draft['sources'], self.now)
        memory.record_decision(self.data, draft, {'memory_updates': {'people': [
            {'account_id': 18, 'text': 'Worth following up.', 'source_ids': ['post:1']}]}}, self.now)
        self.data['memory']['maya'].update(summary='Operator note.', summary_source='operator')
        memory.reset(self.data, 'maya', self.now + 1)
        view = self.view()
        self.assertEqual(view['subjective_notes']['people'], [])
        self.assertEqual(view['subjective_notes']['summary'], '')
        self.assertEqual(view['subjective_notes']['operator_notes'], '')
        self.assertEqual(view['observed_reading'], [])
        self.assertEqual(view['relationships'], [])
        self.assertEqual(view['completed_actions'], [])
        self.assertEqual(view['pending_intentions'][0]['draft_id'], draft['id'])
        self.assertTrue(view['pending_intentions'][0]['predates_memory_reset'])
        self.assertNotIn('reason', view['pending_intentions'][0])
        self.assertNotIn('sources', view['pending_intentions'][0])

    def test_subjective_updates_remain_bounded_and_old_model_outputs_work(self):
        for number in range(50):
            draft = self.draft(number + 1, sources=[self.source(number + 1, author_id=100 + number)])
            source_id = draft['sources'][0]['id']
            memory.record_decision(self.data, draft, {'memory': 'Legacy note.', 'memory_updates': {
                'people': [{'account_id': 100 + number, 'text': 'A person I read.', 'source_ids': [source_id]}],
                'questions': [{'text': 'Question ' + str(number), 'source_ids': [source_id]}],
                'viewpoints': [{'text': 'View ' + str(number), 'source_ids': [source_id]}]}}, self.now + number)
        subjective = self.data['memory']['maya']['subjective']
        self.assertEqual(len(subjective['people']), memory.MAX_PEOPLE_NOTES)
        self.assertEqual(len(subjective['questions']), memory.MAX_QUESTIONS)
        self.assertEqual(len(subjective['viewpoints']), memory.MAX_VIEWPOINTS)
        self.assertLessEqual(len(json.dumps(self.view(), ensure_ascii=False)), memory.MAX_CONTEXT_CHARACTERS)
        before = copy.deepcopy(subjective)
        memory.record_decision(self.data, self.draft(200), {'memory': 'An older compatible response.'}, self.now + 200)
        self.assertEqual(before, self.data['memory']['maya']['subjective'])
        self.assertEqual(self.data['memory']['maya']['episodes']['decisions'][-1]['reflection'], 'An older compatible response.')

    def test_strict_schema_only_offers_actual_source_and_other_author_evidence(self):
        from ctn_data.agent_decision import response_schema
        schema = response_schema({**self.agent, 'allowed_actions': ['post', 'reply', 'skip']},
                                 [self.source(), self.source(2, author_id=77)])
        update = schema['properties']['memory_updates']
        self.assertIn('memory_updates', schema['required'])
        self.assertEqual(set(update['required']), {'viewpoints', 'questions', 'people'})
        person = update['properties']['people']['items']
        self.assertEqual(person['properties']['account_id']['enum'], [18])
        self.assertEqual(person['properties']['source_ids']['items']['enum'], ['post:1', 'post:2'])
        self.assertFalse(person['additionalProperties'])
        no_author = response_schema({**self.agent, 'allowed_actions': ['post', 'skip']},
                                    [self.source(author_id=None)])
        self.assertEqual(no_author['properties']['memory_updates']['properties']['people']['maxItems'], 0)


if __name__ == '__main__':
    unittest.main()
