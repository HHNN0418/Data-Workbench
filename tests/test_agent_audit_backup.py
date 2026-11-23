"""Portable snapshots retain the full audit beyond the bounded live summary."""
import json
from pathlib import Path
import tempfile
import unittest

from ctn_data import agent_audit
from ctn_data.agent_control import ACTION_TYPES, AgentStore
from ctn_data.db import SCHEMA, SCHEMA_VERSION, backup, connect, initialize
from ctn_data.migrate_postgres import _columns, _copy, _digest, schema_tables
from ctn_data.postgres import postgres_schema


AT = '2026-10-20T02:00:00Z'


class AgentAuditBackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / 'source.sqlite3'

    def populated_store(self, count=275):
        store = AgentStore(self.path)
        def seed(data, conn):
            data['agents']['maya'] = {'id': 'maya', 'version': 1, 'allowed_actions': list(ACTION_TYPES)}
            data['events'].extend({'id': f'maya-{number:05d}', 'agent_id': 'maya', 'at': AT,
                'kind': 'reading', 'message': f'Read {number}', 'draft_id': None,
                'details': {'source_count': 1}} for number in range(count))
            data['events'].append({'id': 'ethan-event', 'agent_id': 'ethan', 'at': AT,
                'kind': 'reading', 'message': 'Separate role', 'draft_id': None})
        store.change(seed)
        return store

    def test_core_schema_and_portable_table_enumeration_include_audit_once(self):
        self.assertEqual(schema_tables().count('agent_audit_events'), 1)
        initialize(self.path)
        conn = connect(self.path)
        try:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_audit_events').fetchone()[0], 0)
            indexes = {row[1] for row in conn.execute('PRAGMA index_list(agent_audit_events)')}
            self.assertIn('idx_agent_audit_agent_time', indexes)
            self.assertIn('idx_agent_audit_agent_kind_time', indexes)
        finally:
            conn.close()

    def test_independent_and_global_initializers_have_identical_columns(self):
        independent, core = connect(':memory:'), connect(':memory:')
        try:
            with independent:
                agent_audit.initialize(independent)
            core.executescript(SCHEMA)
            expected = [tuple(row) for row in independent.execute('PRAGMA table_info(agent_audit_events)')]
            self.assertEqual([tuple(row) for row in core.execute('PRAGMA table_info(agent_audit_events)')], expected)
            with core:
                agent_audit.initialize(core)  # Both orders are safe and idempotent.
            self.assertEqual(_columns(independent, 'agent_audit_events'), _columns(core, 'agent_audit_events'))
        finally:
            independent.close()
            core.close()

    def test_sqlite_backup_contains_full_history_while_live_summary_is_200(self):
        store = self.populated_store()
        self.assertEqual(len(store.read()['events']), 200)
        saved = backup(self.path, self.directory / 'backups')
        source, snapshot = connect(self.path), connect(saved)
        try:
            columns = _columns(snapshot, 'agent_audit_events')
            self.assertEqual(_digest(source, 'agent_audit_events', columns),
                             _digest(snapshot, 'agent_audit_events', columns))
            self.assertEqual(agent_audit.query(snapshot, 'maya')['total'], 275)
            self.assertEqual(agent_audit.query(snapshot, 'ethan')['total'], 1)
            self.assertEqual(snapshot.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertIsNone(snapshot.execute('PRAGMA foreign_key_check').fetchone())
        finally:
            source.close()
            snapshot.close()

    def test_reopening_backup_seeds_recent_history_without_deleting_or_duplicating(self):
        self.populated_store(340)
        saved = backup(self.path, self.directory / 'backups')
        first = AgentStore(saved)
        second = AgentStore(saved)
        self.assertEqual(len(first.read()['events']), 200)
        self.assertEqual(len(second.read()['events']), 200)
        conn = connect(saved)
        try:
            self.assertEqual(agent_audit.query(conn, 'maya')['total'], 340)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_audit_events').fetchone()[0], 341)
            oldest = conn.execute('SELECT message FROM agent_audit_events WHERE id=?', ('maya-00000',)).fetchone()
            self.assertEqual(oldest[0], 'Read 0')
        finally:
            conn.close()

    def test_existing_migration_copy_and_digest_path_accepts_audit_text_ids(self):
        self.populated_store()
        initialize(self.directory / 'target.sqlite3')
        source, target = connect(self.path), connect(self.directory / 'target.sqlite3')
        try:
            columns = _columns(source, 'agent_audit_events')
            with target:
                _copy(source, target, 'agent_audit_events', columns)
            self.assertEqual(_digest(source, 'agent_audit_events', columns),
                             _digest(target, 'agent_audit_events', columns))
            self.assertEqual(agent_audit.query(target, 'maya')['total'], 275)
            details = target.execute('SELECT details_json FROM agent_audit_events WHERE id=?', ('maya-00001',)).fetchone()[0]
            self.assertEqual(json.loads(details), {'source_count': 1})
        finally:
            source.close()
            target.close()

    def test_postgres_ddl_retains_text_event_ids_and_portable_indexes(self):
        schema = postgres_schema(SCHEMA)
        start = schema.index('CREATE TABLE IF NOT EXISTS agent_audit_events(')
        audit_ddl = schema[start:schema.index(';', start)]
        self.assertIn('id TEXT PRIMARY KEY', audit_ddl)
        self.assertNotIn('BIGSERIAL', audit_ddl)
        self.assertIn('idx_agent_audit_agent_time', schema)
        self.assertIn('idx_agent_audit_agent_kind_time', schema)


if __name__ == '__main__':
    unittest.main()
