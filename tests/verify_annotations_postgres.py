"""Run the annotation tests on a random temporary PostgreSQL database.

Creates ctn_verify_<random>, runs tests/test_annotations.py against it and
drops it; never touches live CTN.
"""
import sys
import unittest
import uuid
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from ctn_data.db import connect, initialize
from tests.test_annotations import AnnotationTests


class AnnotationPostgres(AnnotationTests):
    dsn = None

    def setUp(self):
        self.path = self.dsn
        initialize(self.path)
        conn = connect(self.path)
        with conn:
            for table in ('post_annotations', 'account_annotations', 'posts'):
                conn.execute('DELETE FROM ' + table)
        conn.close()

    def tearDown(self):
        pass


def run():
    from ctn_data.secret_files import read_secret
    config = conninfo_to_dict(read_secret(ROOT / 'data/secrets/postgres_url').strip())
    database = 'ctn_verify_' + uuid.uuid4().hex[:12]
    admin = psycopg.connect(make_conninfo(**config), autocommit=True)
    admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
    try:
        AnnotationPostgres.dsn = (f"postgresql://{quote(config['user'], safe='')}:{quote(config['password'], safe='')}"
                                  f"@{config['host']}:{config['port']}/{database}")
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(AnnotationPostgres))
        return 0 if result.wasSuccessful() else 1
    finally:
        from ctn_data import postgres
        close = getattr(postgres, 'close_pools', None)
        if callable(close):
            close()
        admin.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(database)))
        admin.close()


if __name__ == '__main__':
    sys.exit(run())
