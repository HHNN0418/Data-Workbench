"""Run the pipeline status tests on a random temporary PostgreSQL database.

Creates ctn_verify_<random>, runs tests/test_pipeline.py against it and drops
it; never touches live CTN.
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
from tests.test_pipeline import PipelineTests

TABLES = ('post_annotations', 'account_annotations', 'feed_observations', 'counter_snapshots', 'counter_sampling',
          'post_attribution', 'agent_audit_events', 'llm_calls', 'official_activity', 'score_observation_keys',
          'score_snapshots', 'team_accounts', 'posts', 'accounts')


class PipelinePostgres(PipelineTests):
    dsn = None

    def setUp(self):
        from ctn_data import agent_targeting, npc_calibration
        # One DSN serves every test here: per-path caches must not carry results across tests.
        with agent_targeting._cache_lock:
            agent_targeting._cache.clear()
        with npc_calibration._cache_lock:
            npc_calibration._cache.clear()
        self.path = self.dsn
        initialize(self.path)
        conn = connect(self.path)
        with conn:
            for table in TABLES:
                conn.execute('DELETE FROM ' + table)
            conn.execute("DELETE FROM checkpoints WHERE key='agent_content_research_v1'")
        conn.close()
        self.seed()

    def tearDown(self):
        pass


def run():
    from ctn_data.secret_files import read_secret
    config = conninfo_to_dict(read_secret(ROOT / 'data/secrets/postgres_url').strip())
    database = 'ctn_verify_' + uuid.uuid4().hex[:12]
    admin = psycopg.connect(make_conninfo(**config), autocommit=True)
    admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
    try:
        PipelinePostgres.dsn = (f"postgresql://{quote(config['user'], safe='')}:{quote(config['password'], safe='')}"
                                f"@{config['host']}:{config['port']}/{database}")
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(PipelinePostgres))
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
