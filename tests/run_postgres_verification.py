"""Create and destroy an isolated PostgreSQL database; never touches live CTN."""
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from ctn_data.db import backup, connect, utc_now
from ctn_data.migrate_postgres import migrate, schema_tables, identifier


def run():
    from ctn_data.secret_files import read_secret
    config = conninfo_to_dict(read_secret(Path('data/secrets/postgres_url')).strip())
    database = 'ctn_verify_' + uuid.uuid4().hex[:12]
    admin = psycopg.connect(make_conninfo(**config), autocommit=True)
    admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
    config['dbname'] = database
    # Public adapter uses URI targets exclusively.
    from urllib.parse import quote
    dsn = f"postgresql://{quote(config['user'],safe='')}:{quote(config['password'],safe='')}@{config['host']}:{config['port']}/{database}"
    os.environ['CTN_TEST_POSTGRES_DSN'] = dsn
    try:
        from tests import test_postgres
        suite = unittest.defaultTestLoader.loadTestsFromModule(test_postgres)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if not result.wasSuccessful():
            raise RuntimeError('PostgreSQL contract tests failed')
        Path('reports/generated').mkdir(parents=True, exist_ok=True)
        Path('reports/generated/postgres-contract-verification.json').write_text(
            json.dumps({'verified_at':utc_now(),'passed':result.testsRun,'failed':len(result.failures),
                        'errors':len(result.errors),'skipped':len(result.skipped),
                        'isolation':'random temporary database; no writes to production CTN'},indent=2),encoding='utf-8')
        if '--contracts-only' in sys.argv:
            return
        conn = connect(dsn)
        with conn:
            for table in reversed(schema_tables()):
                if table != 'meta':
                    conn.execute('DELETE FROM ' + identifier(table))
            conn.execute("DELETE FROM meta WHERE key NOT IN ('mode','created_at')")
        conn.close()
        source = Path(sys.argv[1]) if len(sys.argv) > 1 else sorted(Path('backups').glob('pre-postgres-*.sqlite3'))[-1]
        report = migrate(source, dsn)
        from ctn_data import queries
        from ctn_data.analytics import analyze
        from ctn_data.server import DEFAULT_CONFIG
        from ctn_data.collector import _counter_schedule
        checks = {}
        for name, action in {
            'overview': lambda: queries.overview(dsn),
            'posts': lambda: queries.posts(dsn, {}),
            'trending': lambda: queries.posts(dsn, {'feed':'trending'}),
            'accounts': lambda: queries.accounts(dsn, {}),
            'news': lambda: queries.news(dsn, {}),
            'activity': lambda: queries.activity(dsn, {}),
            'operations': lambda: queries.operations(dsn, DEFAULT_CONFIG),
            'analysis': lambda: analyze(dsn, {}),
        }.items():
            action()
            checks[name] = 'passed'
        conn = connect(dsn)
        _counter_schedule(conn, DEFAULT_CONFIG)
        conn.close()
        checks['counter_schedule'] = 'passed'
        report['read_models'] = checks
        destination = backup(dsn, 'reports/generated/postgres-verification')
        report['portable_backup'] = str(destination)
        report['contracts_passed'] = result.testsRun
        Path('reports/generated/postgres-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'migration_verified': report['verified'], 'tables': len(report['tables']),
                          'rows': sum(item['rows'] for item in report['tables'].values()), 'backup':str(destination)}))
    finally:
        admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(database)))
        admin.close()


if __name__ == '__main__':
    run()
