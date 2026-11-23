"""Long-term recall keeps real experiences beyond the bounded working memory."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from ctn_data import agent_memory as memory
from ctn_data import agent_memory_archive as archive
from ctn_data.db import SCHEMA, backup, connect, initialize
from ctn_data.migrate_postgres import _columns, _copy, _digest, schema_tables
from ctn_data.postgres import postgres_schema, translate_sql


class AgentMemoryArchiveTests(unittest.TestCase):
    def test_legacy_migration_does_not_replay_entire_activity_for_every_draft(self):
        own = self.data['memory'].setdefault('maya', {})
        own['activity'] = [{'draft_id': f'd{n}', 'action': 'post', 'state': 'published',
                            'at': memory._at(self.now + n), 'body': f'Past authored post {n}'}
                           for n in range(100)]
        self.data['drafts'] = {f'd{n}': self.draft(n, id=f'd{n}', state='published',
                               created_at=memory._at(self.now + n)) for n in range(120)}
        initial = self.conn.total_changes
        self.capture()
        self.assertEqual(self.query(kind='outcome')['total'], 120)
        self.assertLess(self.conn.total_changes - initial, 600)

    def setUp(self):
        self.conn = connect(':memory:')
        self.addCleanup(self.conn.close)
        with self.conn:
            archive.initialize(self.conn)
        self.now = 1792452000
        self.agent = {'id': 'maya', 'account_id': 77}
        self.other = {'id': 'ethan', 'account_id': 88}
        self.data = {'agents': {'maya': self.agent, 'ethan': self.other},
                     'memory': {}, 'drafts': {}, 'events': [], 'settings': {}}

    def source(self, number=1, **changes):
        return {'id': f'post:{number}', 'post_id': number, 'kind': 'post',
                'author_id': 18, 'title': '@library_reader',
                'text': 'The local library needs better ferry access.',
                'version': 'v1', 'origin': 'recent', 'source_role': 'recent',
                'observed_at': self.now, **changes}

    def draft(self, number=1, **changes):
        return {'id': f'draft:{number}', 'agent_id': 'maya', 'action': 'reply',
                'state': 'pending', 'body': 'Could we revisit the ferry timetable?',
                'target_post_id': number, 'post_id': None,
                'created_at': memory._at(self.now + number),
                'source_ids': [f'post:{number}'], 'sources': [self.source(number)],
                'reason': 'A question about ferry access.', **changes}

    def capture(self):
        with self.conn:
            archive.capture(self.conn, self.data)

    def query(self, agent_id='maya', **filters):
        return archive.query(self.conn, self.data, agent_id, **filters)

    def recall(self, sources=None, agent=None):
        return archive.recall(self.conn, self.data, agent or self.agent,
                              sources or [], self.now + 1000, limit=30)

    def note(self, number, text, *, category='viewpoints', at=None, **changes):
        draft = self.draft(number, **changes)
        update = {'text': text, 'source_ids': draft['source_ids']}
        if category == 'people':
            update['account_id'] = draft['sources'][0]['author_id']
        memory.record_subjective_updates(self.data, draft, {category: [update]},
                                         self.now + number if at is None else at)
        return draft

    def test_single_capture_keeps_reading_evicted_before_transaction_end(self):
        oldest = self.source(1, author_id=41, text='Ancient harbour ferry accessibility meeting.')
        memory.record_read(self.data, 'maya', [oldest], self.now)
        for number in range(2, memory.MAX_READINGS + 25):
            memory.record_read(self.data, 'maya', [self.source(number)], self.now + number)
        self.assertNotIn('post:1', {r['source_id'] for r in
                                   self.data['memory']['maya']['episodes']['readings']})
        self.capture()
        recalled = self.recall([self.source(900, author_id=41,
            text='Can we revisit the ancient harbour accessibility meeting?')])
        ancient = next(row for row in recalled['readings'] if row['source_id'] == 'post:1')
        self.assertEqual(ancient['excerpt'], oldest['text'])
        self.assertEqual(ancient['epistemic_status'], 'observed_quote')
        self.assertGreater(self.query(kind='reading')['total'], memory.MAX_READINGS)

    def test_subjective_notes_survive_working_cap_and_remain_sourced_impressions(self):
        self.note(1, 'The ancient harbour accessibility question still matters to me.',
                  sources=[self.source(1, author_id=41)])
        for number in range(2, memory.MAX_VIEWPOINTS + 8):
            self.note(number, f'My current reading impression number {number}.')
        self.assertEqual(len(self.data['memory']['maya']['subjective']['viewpoints']),
                         memory.MAX_VIEWPOINTS)
        self.capture()
        result = self.query(kind='viewpoints', q='ancient harbour')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['epistemic_status'], 'subjective_impression')
        self.assertEqual(result['items'][0]['source_ids'], ['post:1'])
        recalled = self.recall([self.source(900, author_id=41,
            text='The ancient harbour accessibility question returns.')])
        self.assertTrue(any('ancient harbour' in row['text'] for row in recalled['viewpoints']))

    def test_old_person_and_event_recalled_beyond_recent_candidate_window(self):
        memory.record_read(self.data, 'maya', [self.source(1, author_id=41,
            text='Ancient harbour accessibility meeting during the winter crossing.')], self.now)
        for number in range(2, 1252):
            memory.record_read(self.data, 'maya', [self.source(number)], self.now + number)
        self.capture()
        recalled = self.recall([self.source(9000, author_id=41,
            text='Revisit the ancient harbour accessibility meeting and winter crossing.')])
        self.assertIn('post:1', {row['source_id'] for row in recalled['readings']})
        # Even without the person's link, detailed event terms must retrieve
        # that old reading instead of only recent common-title matches.
        topical = self.recall([self.source(9001, author_id=999,
            text='Ancient harbour accessibility meeting during the winter crossing.')])
        self.assertIn('post:1', {row['source_id'] for row in topical['readings']})

    def test_every_person_impression_version_remains_available(self):
        for index, text in enumerate(('Initially I found their questions careful.',
                                      'Later I disagreed with their timetable claim.',
                                      'Today I appreciate their correction.'), start=1):
            self.note(index, text, category='people')
            self.capture()
        working = self.data['memory']['maya']['subjective']['people']
        self.assertEqual(len(working), 1)
        self.assertEqual(working[0]['text'], 'Today I appreciate their correction.')
        history = self.query(kind='people')
        self.assertEqual(history['total'], 3)
        self.assertEqual(len({row['id'] for row in history['items']}), 3)
        self.assertTrue(any(row['excerpt'].startswith('Initially')
                            for row in self.query(kind='people', q='Initially')['items']))
        self.assertTrue(all(row['epistemic_status'] == 'subjective_impression'
                            for row in history['items']))

    def test_capture_is_idempotent_and_reread_preserves_first_read_time(self):
        memory.record_read(self.data, 'maya', [self.source()], self.now)
        self.capture()
        first_id = self.query()['items'][0]['id']
        self.capture()
        memory.record_read(self.data, 'maya', [self.source()], self.now + 60)
        self.capture()
        self.assertEqual(self.query()['total'], 1)
        self.assertEqual(self.query()['items'][0]['id'], first_id)
        row = self.recall()['readings'][0]
        self.assertEqual(row['first_read_at'], memory._at(self.now))
        self.assertEqual(row['last_read_at'], memory._at(self.now + 60))

    def test_edited_source_keeps_the_old_wording_and_version(self):
        memory.record_read(self.data, 'maya', [self.source(text='Old timetable is missing.')], self.now)
        self.capture()
        memory.record_read(self.data, 'maya', [self.source(version='v2',
            text='The revised timetable is available.')], self.now + 60)
        self.capture()
        self.assertEqual(self.query(kind='reading')['total'], 2)
        readings = self.recall()['readings']
        self.assertEqual({row['version'] for row in readings}, {'v1', 'v2'})
        self.assertIn('Old timetable is missing.', {row['excerpt'] for row in readings})

    def test_invalid_evidence_and_raw_generated_delivery_claim_are_not_recalled(self):
        draft = self.draft()
        memory.record_decision(self.data, draft, {
            'memory': 'I have already published an imaginary reply.',
            'memory_updates': {'viewpoints': [
                {'text': 'An invented reading impression.', 'source_ids': ['post:missing']}],
                'people': [{'account_id': 999, 'text': 'An invented person.',
                            'source_ids': ['post:1']}]},
        }, self.now)
        self.capture()
        recalled = self.recall()
        self.assertFalse(recalled['viewpoints'])
        self.assertFalse(recalled['people'])
        serialized = json.dumps(recalled)
        self.assertNotIn('imaginary reply', serialized)
        self.assertNotIn('invented', serialized)
        history = self.query()
        self.assertEqual(history['total'], 1)
        self.assertEqual(history['items'][0]['kind'], 'outcome')
        self.assertEqual(history['items'][0]['state'], 'pending')
        self.assertNotIn('imaginary reply', json.dumps(history))

    def test_outcome_reconciliation_has_one_latest_state_and_preserves_manual_origin(self):
        draft = self.draft(state='uncertain', origin='manual')
        self.data['drafts'][draft['id']] = draft
        memory.record_outcome(self.data, draft, 'uncertain', 'Delivery requires checking.', self.now)
        self.capture()
        before = self.recall()['outcomes']
        self.assertEqual(before[0]['epistemic_status'], 'unconfirmed_delivery')
        draft.update(state='published', post_id=900, published_at=memory._at(self.now + 1))
        memory.record_outcome(self.data, draft, 'published', 'Verified remotely.', self.now + 1)
        self.capture()
        after = self.recall()['outcomes']
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]['state'], 'published')
        self.assertEqual(after[0]['origin'], 'manual')
        self.assertEqual(after[0]['epistemic_status'], 'confirmed_delivery')
        self.assertEqual(self.query(kind='outcome')['total'], 1)

    def test_failed_and_uncertain_records_do_not_become_completed_facts(self):
        for number, state in enumerate(('sending', 'uncertain', 'failed', 'discarded'), start=1):
            draft = self.draft(number, state=state)
            self.data['drafts'][draft['id']] = draft
            memory.record_outcome(self.data, draft, state, 'No confirmed delivery.', self.now + number)
        self.capture()
        recalled = self.recall()
        self.assertEqual({row['state'] for row in recalled['outcomes']},
                         {'sending', 'uncertain', 'failed', 'discarded'})
        view = memory.context_for(self.data, self.agent, [], self.now + 10, recalled=recalled)
        self.assertFalse(view['completed_actions'])
        self.assertEqual({row['state'] for row in view['uncertain_actions']}, {'sending', 'uncertain'})

    def test_late_older_delivery_event_does_not_regress_verified_history(self):
        draft = self.draft(state='published', post_id=900, origin='manual')
        memory.record_outcome(self.data, draft, 'published', 'Verified remotely.', self.now + 60)
        self.capture()
        stale = self.draft(state='pending', origin='manual')
        memory.record_outcome(self.data, stale, 'pending', 'An old delayed write.', self.now)
        self.capture()
        row = self.recall()['outcomes'][0]
        self.assertEqual((row['state'], row['post_id']), ('published', 900))
        self.assertEqual(self.query(kind='outcome')['items'][0]['state'], 'published')

    def test_reset_hides_pre_reset_archive_and_stale_outbox_without_affecting_other_role(self):
        memory.record_read(self.data, 'maya', [self.source(1, text='Maya old experience.')], self.now)
        memory.record_read(self.data, 'ethan', [self.source(2, text='Ethan retained experience.')], self.now)
        self.capture()
        # A late record queued before the reset must not enter the new memory epoch.
        memory.record_read(self.data, 'maya', [self.source(3, text='Maya stale queued experience.')], self.now + 1)
        memory.reset(self.data, 'maya', self.now + 2)
        self.capture()
        self.assertEqual(self.query()['total'], 0)
        self.assertFalse(self.recall()['readings'])
        self.assertEqual(self.query('ethan')['total'], 1)
        memory.record_read(self.data, 'maya', [self.source(1, text='Maya actual new experience.')], self.now + 3)
        self.capture()
        self.assertEqual(self.query()['total'], 1)
        self.assertEqual(self.query()['items'][0]['excerpt'], 'Maya actual new experience.')

    def test_forget_deletes_only_the_named_role_archive(self):
        for aid in ('maya', 'ethan'):
            memory.record_read(self.data, aid, [self.source()], self.now)
        self.capture()
        with self.conn:
            archive.forget(self.conn, 'maya')
        self.assertFalse(archive.export_for(self.conn, 'maya'))
        self.assertTrue(archive.export_for(self.conn, 'ethan'))

    def test_history_search_is_literal_case_insensitive_and_role_scoped(self):
        for aid, number, text in (('maya', 1, 'Read 50%_capacity at the harbour.'),
                                  ('maya', 2, 'Read 500capacity at the harbour.'),
                                  ('ethan', 3, 'Read 50%_capacity at the harbour.')):
            memory.record_read(self.data, aid, [self.source(number, text=text)], self.now + number)
        self.capture()
        result = self.query(q='50%_CAPACITY', kind='reading')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['excerpt'], 'Read 50%_capacity at the harbour.')

    def test_history_search_uses_bound_parameters(self):
        memory.record_read(self.data, 'maya', [self.source(1, text="quoted ' OR 1=1 --")], self.now)
        memory.record_read(self.data, 'ethan', [self.source(2, text='Different role.')], self.now)
        self.capture()
        self.assertEqual(self.query(q="' OR 1=1 --")['total'], 1)
        self.assertEqual(self.query("maya' OR 1=1 --")['total'], 0)

    def test_history_search_finds_person_and_source_identifiers(self):
        self.note(1, 'Their careful questions matter to me.', category='people',
                  sources=[self.source(1, author_id=2468)])
        self.capture()
        self.assertEqual(self.query(kind='people', q='2468')['total'], 1)
        self.assertEqual(self.query(kind='people', q='post:1')['total'], 1)

    def test_history_pagination_is_bounded_and_does_not_repeat_rows(self):
        for number in range(1, 9):
            memory.record_read(self.data, 'maya', [self.source(number)], self.now + number)
        self.capture()
        first = self.query(limit=3)
        second = self.query(limit=3, offset=3)
        third = self.query(limit=3, offset=6)
        self.assertEqual([page['total'] for page in (first, second, third)], [8, 8, 8])
        self.assertEqual([len(page['items']) for page in (first, second, third)], [3, 3, 2])
        self.assertTrue(first['has_more'])
        self.assertFalse(third['has_more'])
        self.assertEqual(len({row['id'] for page in (first, second, third) for row in page['items']}), 8)
        self.assertEqual(first['retention'], 'persistent_since_upgrade')

    def test_capture_participates_in_caller_transaction_rollback(self):
        memory.record_read(self.data, 'maya', [self.source()], self.now)
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.conn:
                archive.capture(self.conn, self.data)
                raise RuntimeError('rollback')
        self.assertEqual(self.query()['total'], 0)

    def test_recall_and_query_are_read_only_and_do_not_invent_unseen_sources(self):
        memory.record_read(self.data, 'maya', [self.source(1)], self.now)
        self.capture()
        before = copy.deepcopy(self.data)
        self.conn.execute('PRAGMA query_only=ON')
        recalled = self.recall([self.source(999, text='Unseen current candidate.')])
        self.query(q='library')
        self.assertEqual(self.data, before)
        self.assertEqual({row['source_id'] for row in recalled['readings']}, {'post:1'})

    def test_archive_survives_database_close_and_reopen_without_working_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'archive.sqlite3'
            conn = connect(path)
            try:
                with conn:
                    archive.initialize(conn)
                    memory.record_read(self.data, 'maya', [self.source(text='A durable old ferry discussion.')], self.now)
                    archive.capture(conn, self.data)
            finally:
                conn.close()
            conn = connect(path)
            try:
                fresh = {'agents': {'maya': self.agent}, 'memory': {}, 'drafts': {}}
                recalled = archive.recall(conn, fresh, self.agent, [], self.now + 3600)
                self.assertEqual(recalled['readings'][0]['excerpt'], 'A durable old ferry discussion.')
                self.assertEqual(archive.query(conn, fresh, 'maya')['total'], 1)
            finally:
                conn.close()


class AgentMemoryArchiveBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / 'source.sqlite3'
        initialize(self.path)
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        self.data = {'agents': {'maya': {'id': 'maya', 'account_id': 77}},
                     'memory': {}, 'drafts': {}}
        source = {'id': 'post:1', 'post_id': 1, 'author_id': 18, 'version': 'v1',
                  'text': 'A durable ferry access experience.', 'title': 'Ferry access'}
        memory.record_read(self.data, 'maya', [source], 1792452000)
        draft = {'id': 'd1', 'agent_id': 'maya', 'sources': [source,
                 {**source, 'id': 'post:2', 'post_id': 2, 'author_id': 19}],
                 'source_ids': ['post:1', 'post:2']}
        memory.record_subjective_updates(self.data, draft, {'viewpoints': [{
            'text': 'Both accounts raise access concerns.',
            'source_ids': ['post:2', 'post:1']}]}, 1792452000)
        with self.conn:
            archive.capture(self.conn, self.data)

    def test_core_and_independent_schema_columns_match_and_tables_are_portable(self):
        separate = connect(':memory:')
        self.addCleanup(separate.close)
        with separate:
            archive.initialize(separate)
        for table in ('agent_memory_records', 'agent_memory_links'):
            self.assertEqual(schema_tables().count(table), 1)
            self.assertEqual(_columns(self.conn, table), _columns(separate, table))
            self.assertIn('CREATE TABLE IF NOT EXISTS ' + table, postgres_schema(SCHEMA))

    def test_sqlite_online_backup_preserves_records_and_all_evidence_links(self):
        saved = backup(self.path, self.directory / 'backups')
        restored = connect(saved)
        self.addCleanup(restored.close)
        for table in ('agent_memory_records', 'agent_memory_links'):
            columns = _columns(self.conn, table)
            self.assertEqual(_digest(self.conn, table, columns), _digest(restored, table, columns))
        self.assertEqual(archive.query(restored, self.data, 'maya')['total'], 2)
        self.assertEqual(restored.execute('SELECT COUNT(*) FROM agent_memory_links').fetchone()[0], 3)
        self.assertIsNone(restored.execute('PRAGMA foreign_key_check').fetchone())

    def test_migration_copy_preserves_text_record_ids_and_composite_links(self):
        target = connect(':memory:')
        self.addCleanup(target.close)
        with target:
            archive.initialize(target)
            for table in ('agent_memory_records', 'agent_memory_links'):
                columns = _columns(self.conn, table)
                _copy(self.conn, target, table, columns)
                self.assertEqual(_digest(self.conn, table, columns), _digest(target, table, columns))
        self.assertEqual(archive.query(target, self.data, 'maya')['total'], 2)
        self.assertIsNone(target.execute('PRAGMA foreign_key_check').fetchone())

    def test_recall_queries_translate_bound_literals_for_postgres(self):
        seen = []
        underlying = self.conn
        class RecordingConnection:
            def execute(self, sql, parameters=()):
                seen.append((sql, parameters, translate_sql(sql)))
                return underlying.execute(sql, parameters)
        archive.recall(RecordingConnection(), self.data,
                       self.data['agents']['maya'], [{'id': 'post:1', 'post_id': 1,
                           'author_id': 18, 'text': '50%_capacity ferry accessibility'}], 1792453000)
        self.assertTrue(any('topic_matches' in sql for sql, _, _ in seen))
        for sql, parameters, translated in seen:
            self.assertEqual(translated.count('%s'), len(parameters))
            self.assertNotIn('50%', sql)


if __name__ == '__main__':
    unittest.main()
