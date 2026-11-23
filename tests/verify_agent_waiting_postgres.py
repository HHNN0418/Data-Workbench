"""Run waiting regressions against a verified disposable PostgreSQL database."""
from contextlib import closing
import re
import unittest
from unittest.mock import patch

from ctn_data.agent_control import AgentController
from ctn_data.db import connect, initialize
from tests import test_agent_actions as fixture
from tests.test_agent_waiting import AgentWaitingTests
from tests.verify_agent_interaction_policy_postgres import main as isolated_main


PREFIX = 'ctn_agent_waiting_test_'


def run(target, database_name, report):
    assert re.fullmatch(PREFIX + r'[0-9a-f]{32}', database_name), 'unexpected test database'
    with closing(connect(target)) as conn:
        assert conn.execute('SELECT current_database()').fetchone()[0] == database_name, 'test database mismatch'
    initialize(target)
    checks = report['checks']
    methods = (
        'test_pending_publish_waits_until_due_then_completes_same_job_and_clears_errors',
        'test_repeated_publish_deferrals_refresh_wait_without_reviving_failure',
        'test_publisher_busy_is_five_second_wait_and_retry_completes_same_job',
        'test_old_job_success_cannot_clear_a_new_jobs_failure',
        'test_cancel_waiting_job_holds_its_pending_action_for_review',
        'test_uncertain_delivery_stays_failed_and_is_never_automatically_resent',
    )

    def controller(_sqlite_path, *args, **kwargs):
        return AgentController(target, *args, **kwargs)

    for method in methods:
        with closing(connect(target)) as conn:
            assert conn.execute('SELECT current_database()').fetchone()[0] == database_name, 'test database changed'
            # Each fixture starts from empty actor state in this random test DB.
            # Production tables are never opened by the fixture controller.
            with conn:
                conn.execute('DELETE FROM meta WHERE key LIKE ?', ('agent_control_v1:%',))
        result = unittest.TestResult()
        with patch.object(fixture, 'AgentController', side_effect=controller):
            AgentWaitingTests(method).run(result)
        checks[method.removeprefix('test_')] = result.wasSuccessful()
        if not result.wasSuccessful():
            raise AssertionError('isolated waiting regression failed: ' + method)
    report['real_model_calls'] = 0
    report['external_http_calls'] = 0
    report['checks']['all_social_and_model_calls_mocked'] = True


if __name__ == '__main__':
    isolated_main(run_checks=run, database_prefix=PREFIX,
                  default_report='reports/generated/agent-waiting-postgres.json')
