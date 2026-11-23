from contextlib import closing, redirect_stdout
from copy import deepcopy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ctn_data import agent_competition, queries
from ctn_data.db import begin_write, connect, initialize, raw_record, SCHEMA_VERSION
from ctn_data.imports import add_score_snapshot, import_activity_csv
from ctn_data.retention import prune_history
from ctn_data.scoring import score_overview
from tests.test_imports import export_csv

OLD = '2026-08-01T00:00:00.000000Z'
NOW = agent_competition._epoch('2026-10-20T01:00:00Z')
RECENT = '2026-10-20T00:00:00.000000Z'


class StorageRemediationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / 'storage.db'
        initialize(self.db)

    def test_growing_csv_stores_only_new_original_fields(self):
        records = []
        for i in range(10):
            records.append((f'2026-10-19T01:00:{i:02d}Z', 'MEMBERSHIP', f'joined {i}\nmessage'))
            import_activity_csv(self.db, export_csv(records))
        with closing(connect(self.db)) as conn:
            payloads = [json.loads(row[0]) for row in conn.execute('SELECT payload_json FROM raw_records')]
            stored = [tuple(row.values()) for payload in payloads
                      for row in csv.DictReader(io.StringIO(payload['csv'], newline=''))]
            self.assertEqual(stored, records)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM activity_import_payloads').fetchone()[0], 10)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM official_activity').fetchone()[0], 10)
        self.assertTrue(import_activity_csv(self.db, export_csv(records))['already_imported'])
        self.assertEqual(import_activity_csv(self.db, export_csv(list(reversed(records))))['inserted'], 0)
        with closing(connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM raw_records').fetchone()[0], 10)

    def test_invalid_membership_timestamp_does_not_poison_score_and_legacy_is_reclassified(self):
        add_score_snapshot(self.db, '10', '2026-10-19T00:00:00Z')
        add_score_snapshot(self.db, '10', '2026-10-19T02:00:00Z')
        import_activity_csv(self.db, export_csv([('not a time', 'MEMBERSHIP', 'joined')]))
        self.assertEqual(score_overview(self.db)['reconciliation']['status'], 'matched_observed_scope')
        with closing(connect(self.db)) as conn, conn:
            conn.execute("UPDATE official_activity SET parse_status='invalid_time'")
        result = score_overview(self.db)
        self.assertEqual(result['reconciliation']['status'], 'matched_observed_scope')
        self.assertEqual(result['reconciliation']['intervals'][0]['invalid_time_unallocated'], 0)
        import_activity_csv(self.db, export_csv([('not a time', 'SCORE_RED', 'Awarded 5 points')]))
        self.assertEqual(score_overview(self.db)['reconciliation']['status'], 'unresolved')

    def test_filters_and_trending_order_are_applied_before_counter_enrichment(self):
        with closing(connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO team_accounts VALUES(7,'ours',1,'fixture',?)", (RECENT,))
            for pid in range(1, 101):
                conn.execute('INSERT INTO posts(post_id,author_id,kind,body,created_at,first_seen,last_seen)'
                             ' VALUES(?,?,?,?,?,?,?)',
                             (pid, 7 if pid % 10 == 0 else 9, 'post', 'wanted',
                              f'2026-10-19T00:{pid // 60:02d}:{pid % 60:02d}Z', RECENT, RECENT))
                conn.execute('INSERT INTO counter_snapshots(post_id,observed_at,like_count) VALUES(?,?,?)',
                             (pid, RECENT, pid))
                conn.execute("INSERT INTO feed_observations(post_id,source,observed_at,rank_position)"
                             " VALUES(?,'trending',?,?)", (pid, RECENT, 101 - pid))
        for feed in ('all', 'trending'):
            result = queries.posts(self.db, {'own': 'own', 'feed': feed, 'offset': 2, 'limit': 3})
            self.assertEqual([row['post_id'] for row in result['items']], [80, 70, 60])
            self.assertEqual([row['latest_counts']['like_count'] for row in result['items']], [80, 70, 60])
            self.assertEqual(result['total'], 10)
        self.assertEqual(queries.posts(self.db, {'own': 'unknown', 'limit': 1})['items'][0]['post_id'], 99)
        self.assertEqual(queries.posts(self.db, {'offset': 1000})['items'], [])
        with closing(connect(self.db)) as conn:
            plan = ' '.join(str(tuple(row)) for row in conn.execute(
                'EXPLAIN QUERY PLAN SELECT post_id FROM posts '
                'ORDER BY ctn_timestamp(created_at) DESC NULLS LAST,post_id DESC LIMIT 50'))
            self.assertIn('idx_posts_created_utc', plan)
            self.assertNotIn('TEMP B-TREE', plan)

    def _retention_fixture(self):
        with closing(connect(self.db)) as conn, conn:
            unreferenced = raw_record(conn, 'news', '/old', {'orphan': True}, OLD)
            referenced = raw_record(conn, 'news', '/kept', {'referenced': True}, OLD)
            official = raw_record(conn, 'official_activity_csv', '/legacy', {'csv': 'evidence'}, OLD)
            confirmed = raw_record(conn, 'agent:confirmed', '/receipt', {'confirmed': True}, OLD)
            recent = raw_record(conn, 'news', '/recent', {'fresh': True}, RECENT)
            conn.execute('INSERT INTO posts(post_id,first_seen,last_seen,raw_id) VALUES(1,?,?,?)',
                         (OLD, OLD, referenced))
            conn.executemany('INSERT INTO api_call_logs(occurred_at,method,path) VALUES(?,?,?)',
                             [(OLD, 'GET', '/old'), (RECENT, 'GET', '/recent')])
            conn.executemany('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)', [
                ('worker_result:expired', '{}', OLD),
                ('coordination:post:1', '{"until":1}', OLD),
                ('coordination:post:2', json.dumps({'until': NOW + 1000}), OLD),
                ('coordination:post:3', '{"until":"malformed"}', OLD),
                ('browser_receipt:keep', '{}', OLD),
                ('agent_tracking:action:keep', '{}', OLD),
                ('agent_competition_v1:publication:1', '{}', OLD),
                ('feed:latest', '{"cursor":"keep"}', OLD),
                ('worker_result:two-days', '{}', '2026-10-18T00:00:00.000000Z'),
                ('worker_result:recent', '{}', RECENT),
            ])
            conn.executemany("INSERT INTO collection_tasks(task_key,kind,payload_json,due_at,state,updated_at) "
                             "VALUES(?,'counter','{}',0,?,?)", [
                ('counter:1', 'done', '2026-10-19T23:00:00.000000Z'),
                ('counter:2', 'done', RECENT),
                ('counter:3', 'pending', OLD),
                ('counter:4', 'leased', OLD),
            ])
        return unreferenced, {referenced, official, confirmed, recent}

    def test_retention_is_bounded_dry_run_and_preserves_evidence_and_live_state(self):
        unreferenced, protected = self._retention_fixture()
        preview = prune_history(self.db, now=NOW)
        self.assertEqual(preview['counts'], {'api_call_logs': 1, 'checkpoints': 3, 'raw_records': 1,
                                             'collection_tasks': 1})
        with closing(connect(self.db)) as conn:
            self.assertIsNotNone(conn.execute('SELECT id FROM raw_records WHERE id=?', (unreferenced,)).fetchone())
        result = prune_history(self.db, now=NOW, dry_run=False)
        self.assertEqual(result['counts'], preview['counts'])
        with closing(connect(self.db)) as conn:
            self.assertEqual({row[0] for row in conn.execute('SELECT id FROM raw_records')}, protected)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0], 7)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM api_call_logs').fetchone()[0], 1)
            self.assertEqual({row[0] for row in conn.execute('SELECT task_key FROM collection_tasks')},
                             {'counter:2', 'counter:3', 'counter:4'})
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(prune_history(self.db, now=NOW, dry_run=False)['counts'],
                         {'api_call_logs': 0, 'checkpoints': 0, 'raw_records': 0, 'collection_tasks': 0})

    def test_retention_keeps_delta_provenance_and_limits_each_batch(self):
        import_activity_csv(self.db, export_csv([(OLD, 'MEMBERSHIP', 'joined')]))
        with closing(connect(self.db)) as conn, conn:
            conn.execute('UPDATE raw_records SET observed_at=?', (OLD,))
            conn.executemany('INSERT INTO api_call_logs(occurred_at,method,path) VALUES(?,?,?)',
                             [(OLD, 'GET', '/x')] * 5)
        result = prune_history(self.db, now=NOW, dry_run=False, batch_size=2)
        self.assertEqual(result['counts']['api_call_logs'], 2)
        self.assertEqual(result['counts']['raw_records'], 0)

    def test_schema_upgrade_preserves_business_rows_and_adds_new_tables_and_indexes(self):
        import_activity_csv(self.db, export_csv([(OLD, 'MEMBERSHIP', 'joined')]))
        with closing(connect(self.db)) as conn, conn:
            conn.execute('DROP TABLE activity_import_payloads')
            conn.execute('DROP INDEX idx_posts_created_utc')
            conn.execute('PRAGMA user_version=5')
        initialize(self.db)
        with closing(connect(self.db)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM official_activity').fetchone()[0], 1)
            self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE name='activity_import_payloads'").fetchone())
            self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE name='idx_posts_created_utc'").fetchone())

    def test_upgrade_replaces_checkpoint_time_index_with_partial_index(self):
        with closing(connect(self.db)) as conn, conn:
            conn.execute('DROP INDEX idx_checkpoints_retention_utc')
            conn.execute('CREATE INDEX idx_checkpoints_updated_utc ON checkpoints(ctn_timestamp(updated_at),key)')
        initialize(self.db)
        self._retention_fixture()
        with closing(connect(self.db)) as conn, conn:
            conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)',
                         ('counter_attempt:1', '{"status":"saved"}', OLD))
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE name='idx_checkpoints_updated_utc'").fetchone())
            covered = {row[0] for row in conn.execute(
                'SELECT key FROM checkpoints INDEXED BY idx_checkpoints_retention_utc '
                "WHERE key LIKE 'worker_result:%' OR key LIKE 'coordination:post:%' "
                "OR key LIKE 'agent_tracking:action:%'")}
        # Every row retention may delete or competition reads by age is indexed;
        # per-post counter_attempt progress and permanent receipts are not.
        self.assertEqual(covered, {'worker_result:expired', 'worker_result:two-days', 'worker_result:recent',
                                   'coordination:post:1', 'coordination:post:2', 'coordination:post:3',
                                   'agent_tracking:action:keep'})
        self.assertEqual(prune_history(self.db, now=NOW)['counts']['checkpoints'], 3)

    def test_prepared_competition_results_never_overwrite_concurrent_admissions(self):
        data = {'settings': {'enabled': True}, 'agents': {}, 'drafts': {}, 'rate': {}}
        with closing(connect(self.db)) as conn:
            agent_competition.sync(conn, data, NOW)
            agent_competition.configure(conn, data, {'expected_revision': 0, 'mode': 'resources'}, NOW)
            before = deepcopy(data)
            prepared = agent_competition.prepare_refresh(conn, data, NOW)
            self.assertEqual(data, before)
            data['rate']['competition']['fair']['publish'] = {'grants': {'new': {'at': NOW}}}
            self.assertFalse(agent_competition.apply_refresh(data, prepared))
            self.assertIn('new', data['rate']['competition']['fair']['publish']['grants'])
            fresh = agent_competition.prepare_refresh(conn, data, NOW)
            self.assertTrue(agent_competition.apply_refresh(data, fresh))
            self.assertEqual(data['rate']['competition']['last_refresh_at'], NOW)

    def test_named_write_domains_share_locks_only_when_expected(self):
        class FakePostgres:
            dialect = 'postgresql'
            in_transaction = False
            def __init__(self):
                self.calls = []
            def execute(self, sql, args=None):
                self.calls.append((sql, args))
                self.in_transaction = True
        first, same, other = FakePostgres(), FakePostgres(), FakePostgres()
        begin_write(first, 'agent_control')
        begin_write(same, 'agent_control')
        begin_write(other, 'official_imports')
        self.assertEqual(first.calls, same.calls)
        self.assertNotEqual(first.calls[-1], other.calls[-1])
        self.assertEqual(first.calls[0][0], 'BEGIN ISOLATION LEVEL READ COMMITTED')
        from ctn_data.coordination import cooldown_scope
        self.assertEqual(cooldown_scope('social', 'counters'),
                         cooldown_scope('social', '/api/posts/:id/counters'))

    def test_migration_cli_decodes_protected_target_file(self):
        from ctn_data import migrate_postgres
        target_file = self.db.parent / 'postgres_url'
        target_file.write_bytes(b'DPAPI1\nfixture-ciphertext')
        report_file = self.db.parent / 'migration-report.json'
        argv = ['migrate', '--source', str(self.db), '--target-file', str(target_file),
                '--report', str(report_file)]
        with patch('sys.argv', argv), patch('ctn_data.secret_files.read_secret',
                return_value=' postgres://fixture/isolated \n') as read, \
                patch.object(migrate_postgres, 'migrate',
                    return_value={'verified': True, 'tables': {}}) as migrate, \
                redirect_stdout(io.StringIO()):
            migrate_postgres.main()
        read.assert_called_once_with(target_file)
        migrate.assert_called_once_with(str(self.db), 'postgres://fixture/isolated')
        self.assertTrue(json.loads(report_file.read_text())['verified'])


if __name__ == '__main__':
    unittest.main()
