"""Run cache contracts in a disposable PostgreSQL database, leaving live data alone."""
from contextlib import closing
import json
from pathlib import Path
import sys
import unittest
from urllib.parse import quote
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from ctn_data.db import connect, initialize, utc_now
from tests.test_analysis_cache import AnalysisCacheTests, AnalysisProbe


def run():
    # Credentials stay in memory and never enter command arguments or reports.
    from ctn_data.secret_files import read_secret
    config = conninfo_to_dict(read_secret(Path('data/secrets/postgres_url')).strip())
    database = 'ctn_analysis_verify_' + uuid.uuid4().hex[:12]
    with psycopg.connect(make_conninfo(**config), autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
        config['dbname'] = database
        target = ('postgresql://' + quote(config['user'], safe='') + ':'
                  + quote(config['password'], safe='') + '@' + config['host'] + ':'
                  + config.get('port', '5432') + '/' + database)
        try:
            initialize(target)

            class PostgreSQLAnalysisCacheTests(AnalysisCacheTests):
                def setUp(self):
                    super().setUp()
                    self.path = target
                    self.probe = AnalysisProbe()
                    with closing(connect(target)) as conn, conn:
                        conn.execute('DELETE FROM analysis_cache')
                        conn.execute('DELETE FROM posts')

            suite = unittest.defaultTestLoader.loadTestsFromTestCase(PostgreSQLAnalysisCacheTests)
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            report = {'verified_at': utc_now(), 'backend': 'postgresql',
                      'tests': result.testsRun, 'failures': len(result.failures),
                      'errors': len(result.errors),
                      'isolation': 'Disposable random database; no production writes'}
            destination = Path('reports/generated/analysis-cache-postgres-verification.json')
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(report, indent=2), encoding='utf-8')
            if not result.wasSuccessful():
                raise RuntimeError('PostgreSQL analysis cache verification failed')
            print(json.dumps(report))
        finally:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(database)))


if __name__ == '__main__':
    run()
