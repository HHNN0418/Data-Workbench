"""Durable per-role logs, safe diagnostics and unbounded streaming exports."""
import base64
from contextlib import contextmanager
import csv
import io
import json
import unittest
from unittest.mock import patch

from ctn_data import agent_audit
from ctn_data.db import connect
from ctn_data.postgres import translate_sql


AT = '2026-10-20T02:00:00Z'


class AgentAuditTests(unittest.TestCase):
    def setUp(self):
        self.conn = connect(':memory:')
        with self.conn:
            agent_audit.initialize(self.conn)

    def tearDown(self):
        self.conn.close()

    def event(self, number=1, **values):
        return {'id': f'event-{number:04d}', 'agent_id': 'maya', 'at': AT,
                'kind': 'reading', 'message': '已读取新内容', 'draft_id': None, **values}

    def save(self, *events):
        with self.conn:
            return agent_audit.record(self.conn, events)

    def test_truncation_diagnostics_keep_actual_provider_and_numeric_reasoning_usage(self):
        self.save(self.event(details={'provider': 'Example Provider', 'model': 'model (thinking)',
            'error_code': 'output_truncated', 'finish_reason': 'length',
            'native_finish_reason': 'MAX_TOKENS', 'reasoning_tokens': 4200,
            'answer_chars': 15, 'has_reasoning': True, 'response_body': 'private'}))
        details = json.loads(self.conn.execute('SELECT details_json FROM agent_audit_events').fetchone()[0])
        self.assertEqual(details['provider'], 'Example Provider')
        self.assertEqual(details['model'], 'model (thinking)')
        self.assertEqual(details['reasoning_tokens'], 4200)
        self.assertNotIn('response_body', details)

    def test_a_rejected_draft_keeps_its_text_and_issues_cleaned_and_cut(self):
        body = 'api_key: PRIVATE-TEST-TOKEN\n' + 'I am voting Yes because the jab is free. ' * 20
        issues = ['It concedes a No argument.', '', 7, 'x' * 300, 'Fourth.', 'Fifth.']
        self.save(self.event(kind='generation_failed', details={'action': 'post', 'rejected_body': body,
                                                                'rejected_issues': issues}))
        details = json.loads(self.conn.execute('SELECT details_json FROM agent_audit_events').fetchone()[0])
        self.assertEqual(details['action'], 'post')
        self.assertNotIn('PRIVATE-', json.dumps(details))
        self.assertTrue(details['rejected_body'].startswith('[已隐藏敏感字段]\nI am voting Yes'))
        self.assertEqual(len(details['rejected_body']), agent_audit.REJECTED_BODY_CHARS)
        self.assertEqual(details['rejected_issues'], ['It concedes a No argument.', 'x' * 200, 'Fourth.', 'Fifth.'])
        # Anything that is not text is dropped, and events without the fields get none.
        self.assertEqual(agent_audit.rejected_fields({'body': 'x'}, 'not a list'), {})

    def test_initialize_idempotent_without_global_schema_mutation(self):
        self.save(self.event())
        with self.conn:
            agent_audit.initialize(self.conn)
        self.assertEqual(agent_audit.query(self.conn, 'maya')['total'], 1)
        self.assertEqual(self.conn.execute('PRAGMA user_version').fetchone()[0], 0)

    def test_append_retains_first_event_identity_without_updates(self):
        original = self.event()
        self.assertEqual(self.save(original), 1)
        self.assertEqual(self.save({**original, 'message': 'changed'}), 0)
        self.assertEqual(agent_audit.query(self.conn, 'maya')['items'][0]['message'], original['message'])

    def test_caller_rollback_keeps_state_and_log_atomic(self):
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.conn:
                agent_audit.record(self.conn, [self.event()])
                raise RuntimeError('rollback')
        self.assertEqual(agent_audit.query(self.conn, 'maya')['total'], 0)

    def test_no_200_event_limit_or_cross_role_system_events(self):
        self.save(*(self.event(i) for i in range(275)), self.event(1000, agent_id='ethan'),
                  self.event(1001, agent_id=None))
        found, cursor = [], None
        while True:
            page = agent_audit.query(self.conn, 'maya', limit=47, cursor=cursor)
            self.assertEqual(page['total'], 275)
            self.assertEqual(page['scope'], 'agent')
            self.assertEqual(page['retention'], 'since_audit_enabled')
            self.assertIn('此前', page['history_note'])
            found += [row['id'] for row in page['items']]
            cursor = page['next_cursor']
            if not cursor:
                self.assertFalse(page['has_more'])
                break
        self.assertEqual(len(found), len(set(found)))
        self.assertEqual(len(found), 275)
        self.assertEqual(found, sorted(found, reverse=True))

    def test_cursor_remains_stable_when_newer_event_arrives(self):
        self.save(*(self.event(i) for i in range(8)))
        first = agent_audit.query(self.conn, 'maya', limit=3)
        self.save(self.event(100, at='2026-10-20T02:01:00Z'))
        second = agent_audit.query(self.conn, 'maya', limit=3, cursor=first['next_cursor'])
        self.assertEqual([r['id'] for r in second['items']], ['event-0004', 'event-0003', 'event-0002'])
        self.assertEqual(second['total'], 9)

    def test_timestamps_normalized_before_ordering(self):
        self.save(self.event(1, at='2026-10-20T13:00:00+11:00'),
                  self.event(2, at='2026-10-20T02:00:00.500Z'))
        items = agent_audit.query(self.conn, 'maya')['items']
        self.assertEqual(items[0]['id'], 'event-0002')
        self.assertEqual(items[1]['at'], '2026-10-20T02:00:00.000000Z')

    def test_exact_kind_and_literal_search_with_case_matching(self):
        self.save(self.event(1, message='Read 50%_capacity'), self.event(2, message='Read 500capacity'),
                  self.event(3, kind='draft', message='Read 50%_capacity'),
                  self.event(4, agent_id='maya-other', message='Read 50%_capacity'))
        page = agent_audit.query(self.conn, 'maya', kind='reading', q='50%_CAPACITY')
        self.assertEqual([r['id'] for r in page['items']], ['event-0001'])
        self.assertIsNone(page['next_cursor'])

    def test_search_uses_bound_parameters_not_sql_interpolation(self):
        self.save(self.event(1, message="quoted ' OR 1=1 --"), self.event(2, agent_id='ethan'))
        page = agent_audit.query(self.conn, 'maya', q="' OR 1=1 --")
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['items'][0]['id'], 'event-0001')
        self.assertEqual(agent_audit.query(self.conn, "maya' OR 1=1 --")['total'], 0)

    def test_search_matches_draft_and_safe_diagnostics(self):
        self.save(self.event(draft_id='draft-42', details={'job_id': 'job-9'}))
        self.assertEqual(agent_audit.query(self.conn, 'maya', q='draft-42')['total'], 1)
        self.assertEqual(agent_audit.query(self.conn, 'maya', q='job-9')['total'], 1)

    def test_cursor_bound_to_role_and_all_filters(self):
        self.save(*(self.event(i) for i in range(4)))
        cursor = agent_audit.query(self.conn, 'maya', limit=1)['next_cursor']
        for values in ({'agent_id': 'ethan'}, {'agent_id': 'maya', 'kind': 'reading'},
                       {'agent_id': 'maya', 'q': 'new'}):
            with self.subTest(values=values), self.assertRaises(agent_audit.AuditValidationError):
                agent_audit.query(self.conn, cursor=cursor, **values)

    def test_invalid_or_forged_cursor_rejected(self):
        for cursor in ('', '*bad', 'e30', 'a' * 1501, 42,
                       base64.urlsafe_b64encode(json.dumps({'version': 1, 'scope': '', 'at': AT}).encode()).decode()):
            with self.subTest(cursor=cursor), self.assertRaises(agent_audit.AuditValidationError):
                agent_audit.query(self.conn, 'maya', cursor=cursor)

    def test_invalid_filters_raise_before_streaming(self):
        for values in ({'limit': True}, {'limit': 0}, {'limit': 201}, {'q': 'x' * 201},
                       {'kind': []}, {'q': {}}, {'q': '\x00bad'}):
            with self.subTest(values=values), self.assertRaises(agent_audit.AuditValidationError):
                agent_audit.query(self.conn, 'maya', **values)
        with self.assertRaises(agent_audit.AuditValidationError):
            agent_audit.export_csv(self.conn, 'maya', q='x' * 201)
        with self.assertRaises(agent_audit.AuditValidationError):
            agent_audit.export_csv(self.conn, 'maya', batch_size=0)

    def test_operational_details_are_allowlisted_not_arbitrary_payloads(self):
        self.save(self.event(details={'job_id': 'run-1', 'model': 'grok-4.7',
            'elapsed_seconds': 87.2, 'phase': 'receiving', 'automatic': True, 'manual': True, 'origin': 'manual',
            'prompt': 'private reading history', 'api_key': 'secret', 'cookies': {'sid': 'secret'},
            'response_body': 'raw answer', 'unexpected': 'untrusted',
            'usage': {'prompt_tokens': 500, 'completion_tokens': 49, 'total_tokens': 549,
                      'api_key': 'secret', 'reasoning': 'private text'}},
            diagnostics={'stage_elapsed_seconds': 3, 'timeout_seconds': None}))
        row = agent_audit.query(self.conn, 'maya')['items'][0]
        self.assertEqual(row['details'], {'job_id': 'run-1', 'model': 'grok-4.7',
            'elapsed_seconds': 87.2, 'phase': 'receiving', 'automatic': True, 'manual': True, 'origin': 'manual',
            'stage_elapsed_seconds': 3, 'timeout_seconds': None,
            'usage': {'prompt_tokens': 500, 'completion_tokens': 49, 'total_tokens': 549}})
        raw = self.conn.execute('SELECT details_json FROM agent_audit_events').fetchone()[0]
        self.assertNotIn('secret', raw)
        self.assertNotIn('private', raw)

    def test_safe_details_drop_nonfinite_negative_or_secret_shaped_values(self):
        self.save(self.event(details={'elapsed_seconds': float('nan'), 'duration_ms': -1,
            'request_id': 'Bearer secret', 'model': 'sk-abcdefgh123456',
            'received_bytes': True, 'usage': {'total_tokens': True, 'input_tokens': -1},
            'post_id': 42, 'cancelled': False}))
        self.assertEqual(agent_audit.query(self.conn, 'maya')['items'][0]['details'],
                         {'post_id': 42, 'cancelled': False})

    def test_selection_failure_diagnostics_are_retained_without_response_text(self):
        self.save(self.event(kind='reading_selection', details={
            'selection_status': 'fallback', 'candidate_count': 48, 'selected_count': 12,
            'error_code': 'invalid_selection', 'error_detail': 'unknown_source_id',
            'phase': 'validation', 'http_status': 502, 'response_body': 'SECRET RESPONSE'}))
        details = agent_audit.query(self.conn, 'maya')['items'][0]['details']
        self.assertEqual(details, {'selection_status': 'fallback', 'candidate_count': 48,
            'selected_count': 12, 'error_code': 'invalid_selection', 'error_detail': 'unknown_source_id',
            'phase': 'validation', 'http_status': 502})

    def test_credential_labels_and_tokens_redacted_from_messages(self):
        for i, message in enumerate(('Failed Bearer abc.123', 'key sk-abcdefgh123456',
                                     'api_key=the-secret', 'Cookie: session=private',
                                     'prompt: private reading context', 'response_body=private',
                                     '{"api_key":"private"}', "'Cookie': 'private'",
                                     'prompt:\nprivate reading context')):
            self.save(self.event(i, message=message))
        for row in agent_audit.query(self.conn, 'maya')['items']:
            self.assertIn('[已隐藏敏感字段]', row['message'])
            self.assertNotIn('private', row['message'])
            self.assertNotIn('the-secret', row['message'])
            self.assertNotIn('abcdefgh', row['message'])

    def test_long_message_truncation_is_explicit_and_event_not_lost(self):
        self.save(self.event(message='x' * 9000))
        row = agent_audit.query(self.conn, 'maya')['items'][0]
        self.assertTrue(row['details']['message_truncated'])
        self.assertEqual(row['details']['original_message_chars'], 9000)
        self.assertTrue(row['message'].endswith('[过长说明已截断]'))

    def test_malformed_events_fail_instead_of_silently_disappearing(self):
        for event in (None, self.event(id=''), self.event(at='not-a-date'),
                      self.event(message=None), self.event(agent_id=12)):
            with self.subTest(event=event), self.assertRaises(agent_audit.AuditValidationError):
                self.save(event)
        self.assertEqual(agent_audit.query(self.conn, 'maya')['total'], 0)

    def test_export_has_no_query_page_limit_and_same_filters(self):
        self.save(*(self.event(i) for i in range(260)), self.event(1000, agent_id='ethan'),
                  self.event(1001, kind='draft'))
        chunks = list(agent_audit.export_csv(self.conn, 'maya', kind='reading', batch_size=40))
        self.assertEqual(len(chunks), 8)
        rows = list(csv.DictReader(io.StringIO(''.join(chunks))))
        self.assertEqual(len(rows), 260)
        self.assertTrue(all(row['agent_id'] == 'maya' and row['kind'] == 'reading' for row in rows))
        self.assertEqual(rows[0]['id'], 'event-0259')

    def test_csv_neutralizes_formulas_and_preserves_quoted_multiline_values(self):
        messages = ('=WEBSERVICE("https://example.invalid")', '+cmd', '-cmd', '@SUM(1,1)',
                    '  =SUM(1,1)', '\tcmd', 'ordinary, "quoted"\nnext line')
        self.save(*(self.event(i, message=message) for i, message in enumerate(messages)))
        rows = list(csv.DictReader(io.StringIO(''.join(agent_audit.export_csv(self.conn, 'maya')))))
        by_id = {row['id']: row['message'] for row in rows}
        for i, message in enumerate(messages[:-1]):
            self.assertEqual(by_id[f'event-{i:04d}'], "'" + message)
        self.assertEqual(by_id['event-0006'], messages[-1])

    def test_closing_export_early_releases_stream_cursor(self):
        self.save(*(self.event(i) for i in range(4)))
        closed = []

        @contextmanager
        def fake_stream(conn, sql, params, *, batch_size):
            cursor = conn.execute(sql, params)
            try:
                yield cursor
            finally:
                cursor.close()
                closed.append(True)

        with patch('ctn_data.agent_audit.iter_rows', fake_stream):
            chunks = agent_audit.export_csv(self.conn, 'maya', batch_size=1)
            next(chunks)  # Header does not open the database cursor yet.
            next(chunks)
            chunks.close()
        self.assertEqual(closed, [True])

    def test_sql_uses_portable_text_ids_and_bound_parameters(self):
        seen = []

        class RecordingConnection:
            def execute(inner, sql, params=None):
                seen.append(translate_sql(sql, params is not None))
                return self.conn.execute(sql, params) if params is not None else self.conn.execute(sql)

        connection = RecordingConnection()
        with self.conn:
            agent_audit.initialize(connection)
            agent_audit.record(connection, [self.event()])
        agent_audit.query(connection, 'maya', q='%literal_')
        list(agent_audit.export_csv(connection, 'maya', q='%literal_'))
        self.assertTrue(any('ON CONFLICT(id) DO NOTHING' in sql for sql in seen))
        self.assertTrue(any('agent_id=%s' in sql for sql in seen))
        self.assertFalse(any('AUTOINCREMENT' in sql or 'PRAGMA' in sql for sql in seen))


if __name__ == '__main__':
    unittest.main()
