"""Review decisions and mandatory language policy survive portable recovery."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from ctn_data import agent_audit, agent_review
from ctn_data.agent_control import AgentStore, PREFIX
from ctn_data.cli import main
from ctn_data.db import SCHEMA, SCHEMA_VERSION, backup, connect, initialize
from ctn_data.migrate_postgres import _columns, _copy, _digest, schema_tables
from ctn_data.postgres import postgres_schema
from ctn_data.server import create_app
from tests.auth_helpers import login


AT = '2026-10-20T02:00:00Z'


class AgentReviewBackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / 'source.sqlite3'
        initialize(self.path)

    def populate(self):
        connection = connect(self.path)
        try:
            with connection:
                connection.execute('INSERT INTO meta(key,value) VALUES(?,?)',
                                   (PREFIX + 'settings', json.dumps({'ai_review': {'enabled': True,
                                       'provider_id': 'review-provider', 'model': 'review-model'}})))
                connection.executemany('''INSERT INTO agent_reviews
                    (id,draft_id,agent_id,created_at,input_hash,status,record_json)
                    VALUES(?,?,?,?,?,?,?)''', [
                    (f'review-{i:04d}', f'draft-{i % 7}', 'role-one', AT, f'hash-{i}',
                     'blocked' if i % 2 else 'passed', json.dumps({
                         'language': {'status': 'passed', 'english_required': True,
                                      'emoji_allowed': True},
                         'findings': [{'code': 'NPC_INJECTION', 'evidence': 'Quoted instructions'}]
                                     if i % 2 else []})) for i in range(275)])
        finally:
            connection.close()

    def test_review_table_is_in_core_schema_and_both_initializers_agree(self):
        self.assertEqual(schema_tables().count('agent_reviews'), 1)
        connection, independent = connect(self.path), connect(':memory:')
        try:
            with independent:
                agent_review.initialize(independent)
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            self.assertEqual([tuple(row) for row in connection.execute('PRAGMA table_info(agent_reviews)')],
                             [tuple(row) for row in independent.execute('PRAGMA table_info(agent_reviews)')])
            with connection:
                agent_review.initialize(connection)
            indexes = {row[1] for row in connection.execute('PRAGMA index_list(agent_reviews)')}
            self.assertTrue({'idx_agent_reviews_time', 'idx_agent_reviews_draft'} <= indexes)
        finally:
            connection.close()
            independent.close()

    def test_actual_backup_and_cli_restore_retain_complete_review_history_and_settings(self):
        self.populate()
        saved = backup(self.path, self.directory / 'backups')
        restored = self.directory / 'restored.sqlite3'
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--db', str(restored), 'restore', str(saved)]), 0)
        initialize(restored)
        self.assertEqual(AgentStore.open_existing(restored).read()['settings']['ai_review'],
                         {'enabled': True, 'provider_id': 'review-provider', 'model': 'review-model'})
        source, recovered = connect(self.path), connect(restored)
        try:
            for table in ('agent_reviews', 'meta'):
                columns = _columns(source, table)
                self.assertEqual(_digest(source, table, columns), _digest(recovered, table, columns))
            self.assertEqual(recovered.execute('SELECT COUNT(*) FROM agent_reviews').fetchone()[0], 275)
            oldest = recovered.execute('SELECT record_json FROM agent_reviews WHERE id=?', ('review-0000',)).fetchone()[0]
            self.assertTrue(json.loads(oldest)['language']['english_required'])
            self.assertEqual(recovered.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertIsNone(recovered.execute('PRAGMA foreign_key_check').fetchone())
        finally:
            source.close()
            recovered.close()

    def test_portable_postgres_copy_and_digest_path_include_review_text_ids(self):
        self.populate()
        target_path = self.directory / 'target.sqlite3'
        initialize(target_path)
        source, target = connect(self.path), connect(target_path)
        try:
            # PostgreSQL portable backups and imports use these same copying
            # and digest helpers for every table enumerated from core SCHEMA.
            columns = _columns(source, 'agent_reviews')
            with target:
                _copy(source, target, 'agent_reviews', columns)
            self.assertEqual(_digest(source, 'agent_reviews', columns),
                             _digest(target, 'agent_reviews', columns))
            self.assertEqual(target.execute('SELECT COUNT(*) FROM agent_reviews').fetchone()[0], 275)
        finally:
            source.close()
            target.close()
        ddl = postgres_schema(SCHEMA)
        start = ddl.index('CREATE TABLE IF NOT EXISTS agent_reviews(')
        review_ddl = ddl[start:ddl.index(';', start)]
        self.assertIn('id TEXT PRIMARY KEY', review_ddl)
        self.assertNotIn('IDENTITY', review_ddl)

    def test_review_audit_details_retain_references_without_model_or_secret_payloads(self):
        connection = connect(self.path)
        try:
            with connection:
                agent_audit.record(connection, [{'id': 'event-review', 'agent_id': 'role-one',
                    'draft_id': 'draft-one', 'at': AT, 'kind': 'ai_review', 'message': 'Review finished',
                    'details': {'review_id': 'review-one', 'review_status': 'blocked',
                                'review_kind': 'rules', 'rule_version': '2026-10-21', 'revision': 3,
                                'prompt': 'private prompt', 'response_body': 'private provider body',
                                'api_key': 'private key'}}])
            details = json.loads(connection.execute('SELECT details_json FROM agent_audit_events').fetchone()[0])
            self.assertEqual(details, {'review_id': 'review-one', 'review_status': 'blocked',
                                      'review_kind': 'rules', 'rule_version': '2026-10-21', 'revision': 3})
        finally:
            connection.close()


class AgentReviewPageTests(unittest.TestCase):
    def test_direct_review_routes_and_health_expose_loaded_feature(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            web = directory / 'dist'
            web.mkdir()
            (web / 'index.html').write_text('<html>Review shell</html>', encoding='utf-8')
            app = create_app(directory / 'test.sqlite3', web_root=web)
            client = TestClient(app, base_url='http://127.0.0.1:8765')
            try:
                login(client)
                for path in ('/ai/review', '/ai-review'):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200, path)
                    self.assertIn('Review shell', response.text)
                self.assertEqual(client.get('/api/health').json()['agent_review_policy'], 2)
                self.assertEqual(client.get('/api/review-missing').status_code, 404)
            finally:
                client.close()


if __name__ == '__main__':
    unittest.main()
