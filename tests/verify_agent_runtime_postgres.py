"""Create/remove a random PostgreSQL DB for heartbeat/storage verification."""
from contextlib import closing
import os
import re
import unittest

from ctn_data.db import connect
from tests.test_agent_runtime_postgres import DATABASE_PREFIX, DSN_VARIABLE
from tests.verify_agent_interaction_policy_postgres import main as isolated_main


def run(target, database_name, report):
    assert re.fullmatch(DATABASE_PREFIX + r'[0-9a-f]{32}', database_name), 'unexpected runtime test database'
    with closing(connect(target)) as connection:
        assert connection.execute('SELECT current_database()').fetchone()[0] == database_name, 'runtime database mismatch'
    previous = os.environ.get(DSN_VARIABLE)
    os.environ[DSN_VARIABLE] = target
    try:
        # Reload after setting the explicit DSN, so the default discovery skip
        # never enables these tests against an unrelated configured database.
        import importlib
        from tests import test_agent_runtime_postgres
        module = importlib.reload(test_agent_runtime_postgres)
        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        report.update(tests_run=result.testsRun, failures=len(result.failures),
                      errors=len(result.errors), skipped=len(result.skipped),
                      external_http_calls=0, real_model_calls=0)
        report['checks']['runtime_postgres_tests_passed'] = result.wasSuccessful() and not result.skipped
        assert result.wasSuccessful() and not result.skipped, 'isolated runtime PostgreSQL regressions failed'
    finally:
        if previous is None:
            os.environ.pop(DSN_VARIABLE, None)
        else:
            os.environ[DSN_VARIABLE] = previous


if __name__ == '__main__':
    isolated_main(run_checks=run, database_prefix=DATABASE_PREFIX,
                  default_report='reports/generated/agent-heartbeat-postgres.json')
