"""Storage regression checks in a random disposable PostgreSQL database only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

import psycopg
from psycopg import sql

from ctn_data import queries
from ctn_data.db import begin_write, connect, initialize, raw_record, SCHEMA_VERSION
from ctn_data.imports import add_score_snapshot, import_activity_csv
from ctn_data.retention import prune_history
from ctn_data.scoring import score_overview
from tests.test_imports import export_csv

OLD = '2026-08-01T00:00:00.000000Z'
RECENT = '2026-10-20T00:00:00.000000Z'
NOW = 1792458000


def run(target):
    initialize(target)
    checks, timings = {}, {}
    with closing(connect(target)) as first, closing(connect(target)) as other:
        begin_write(first, 'agent_control')
        started = time.perf_counter()
        begin_write(other, 'official_imports')
        timings['independent_domain_ms'] = round((time.perf_counter() - started) * 1000, 2)
        other.commit()
        assert timings['independent_domain_ms'] < 1000
        waiting, acquired = threading.Event(), threading.Event()
        def same_scope():
            with closing(connect(target)) as conn:
                waiting.set()
                begin_write(conn, 'agent_control')
                acquired.set()
                conn.commit()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(same_scope)
            assert waiting.wait(3)
            assert not acquired.wait(.2), 'same-domain writer did not wait'
            first.commit()
            future.result(timeout=5)
            assert acquired.is_set()
        checks['different_domains_progress_same_domain_serializes'] = True

    # Simulate upgrade from the prior version without touching real data.
    with closing(connect(target)) as conn, conn:
        conn.execute('DROP TABLE activity_import_payloads')
        conn.execute('DROP INDEX idx_posts_created_utc')
        conn.execute('UPDATE _ctn_schema SET version=5')
    initialize(target, force=True)
    with closing(connect(target)) as conn:
        assert conn.execute('SELECT version FROM _ctn_schema').fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("SELECT indexname FROM pg_indexes WHERE indexname='idx_posts_created_utc'").fetchone()
    checks['schema_upgrade_recreates_new_table_and_sort_index'] = True

    records = []
    for i in range(10):
        records.append((f'2026-10-19T01:00:{i:02d}Z', 'MEMBERSHIP', f'joined {i}'))
        import_activity_csv(target, export_csv(records))
    with closing(connect(target)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM official_activity').fetchone()[0] == 10
        assert conn.execute('SELECT COUNT(*) FROM activity_import_payloads').fetchone()[0] == 10
        payloads = [json.loads(row[0]) for row in conn.execute(
            "SELECT payload_json FROM raw_records WHERE source='official_activity_delta_csv'")]
        assert sum(len(row['original_row_numbers']) for row in payloads) == 10
    checks['growing_csv_stores_each_new_event_once'] = True

    add_score_snapshot(target, '10', '2026-10-19T00:00:00Z')
    add_score_snapshot(target, '10', '2026-10-19T02:00:00Z')
    import_activity_csv(target, export_csv([('bad-time', 'MEMBERSHIP', 'joined')]))
    assert score_overview(target)['reconciliation']['status'] == 'matched_observed_scope'
    with closing(connect(target)) as conn, conn:
        conn.execute("UPDATE official_activity SET parse_status='invalid_time' WHERE occurred_at IS NULL")
    assert score_overview(target)['reconciliation']['status'] == 'matched_observed_scope'
    checks['legacy_and_new_non_scoring_bad_times_do_not_poison_reconciliation'] = True

    with closing(connect(target)) as conn, conn:
        reuse_id = raw_record(conn, 'news', '/reuse', {'reuse': True}, OLD)
        orphan_id = raw_record(conn, 'news', '/orphan', {'orphan': True}, OLD)
        legacy_id = raw_record(conn, 'official_activity_csv', '/legacy', {'csv': 'original'}, OLD)
        conn.executemany('INSERT INTO api_call_logs(occurred_at,method,path) VALUES(?,?,?)',
                         [(OLD, 'GET', '/old'), (RECENT, 'GET', '/recent')])
        conn.executemany('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)', [
            ('worker_result:old', '{}', OLD),
            ('coordination:post:1', '{"until":1}', OLD),
            ('coordination:post:bad', '{"until":"malformed"}', OLD),
            ('coordination:post:bad-json', '{malformed', OLD),
            ('browser_receipt:keep', '{}', OLD),
            ('agent_tracking:action:keep', '{}', OLD),
        ])
    preview = prune_history(target, now=NOW)
    # Retention also prunes finished queue rows (added 2026-10-21); none exist here.
    assert preview['counts'] == {'api_call_logs': 1, 'checkpoints': 2, 'raw_records': 2, 'collection_tasks': 0}, preview
    with closing(connect(target)) as owner:
        assert raw_record(owner, 'news', '/reuse', {'reuse': True}, OLD) == reuse_id
        cleaned = prune_history(target, now=NOW, dry_run=False)
        assert cleaned['counts']['raw_records'] == 1, cleaned
        owner.execute('INSERT INTO posts(post_id,first_seen,last_seen,raw_id) VALUES(1,?,?,?)',
                      (OLD, OLD, reuse_id))
        owner.commit()
    with closing(connect(target)) as conn:
        assert conn.execute('SELECT id FROM raw_records WHERE id=?', (orphan_id,)).fetchone() is None
        assert conn.execute('SELECT id FROM raw_records WHERE id=?', (legacy_id,)).fetchone()
        assert conn.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0] == 4
    assert prune_history(target, now=NOW, dry_run=False)['counts']['raw_records'] == 0
    checks['retention_preserves_provenance_live_reuse_and_malformed_checkpoints'] = True

    with closing(connect(target)) as conn, conn:
        conn.execute("INSERT INTO team_accounts VALUES(7,'ours',1,'fixture',?)", (RECENT,))
        conn.executemany('INSERT INTO posts(post_id,author_id,kind,body,created_at,first_seen,last_seen)'
            ' VALUES(?,?,?,?,?,?,?)',
            [(i, 7 if i % 10 == 0 else 9, 'post', 'wanted',
              f'2026-10-19T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z',
              RECENT, RECENT) for i in range(2, 10002)])
        conn.executemany('INSERT INTO counter_snapshots(post_id,observed_at,like_count) VALUES(?,?,?)',
                         [(i, RECENT, i) for i in range(2, 10002)])
        conn.execute('ANALYZE posts')
        conn.execute('ANALYZE counter_snapshots')
    started = time.perf_counter()
    result = queries.posts(target, {'own': 'own', 'offset': 2, 'limit': 3})
    timings['filtered_page_ms'] = round((time.perf_counter() - started) * 1000, 2)
    assert [row['post_id'] for row in result['items']] == [9980, 9970, 9960], result
    assert [row['latest_counts']['like_count'] for row in result['items']] == [9980, 9970, 9960]
    with closing(connect(target)) as conn:
        plan = conn.raw.execute('EXPLAIN (ANALYZE, FORMAT JSON) SELECT post_id FROM posts '
            'ORDER BY ctn_timestamp(created_at) DESC NULLS LAST,post_id DESC LIMIT 50').fetchone()[0][0]
        assert 'idx_posts_created_utc' in json.dumps(plan), plan
        timings['indexed_page_sql_ms'] = plan['Execution Time']
        # The author condition now matches the numeric index, including on PG.
        author_plan = conn.raw.execute('EXPLAIN (FORMAT JSON) SELECT post_id FROM posts WHERE author_id=7').fetchone()[0]
        assert 'idx_posts_author_created' in json.dumps(author_plan), author_plan
    checks['page_filters_counts_and_postgres_sort_author_indexes'] = True
    return {'checks': checks, 'timings_ms': timings,
            'isolation': 'random disposable PostgreSQL database; no production table writes'}


def main():
    from ctn_data.secret_files import read_secret
    source = read_secret(Path('data/secrets/postgres_url')).strip()
    parts = urlsplit(source)
    name = 'ctn_storage_test_' + uuid.uuid4().hex
    assert re.fullmatch(r'ctn_storage_test_[0-9a-f]{32}', name)
    target = urlunsplit(parts._replace(path='/' + name))
    assert target != source
    admin = psycopg.connect(source, autocommit=True, connect_timeout=10)
    created = False
    try:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        created = True
        result = run(target)
    finally:
        try:
            if created:
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        finally:
            admin.close()
    result['temporary_database_removed'] = True
    destination = Path('reports/generated/storage-remediation-postgres.json')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
