"""Isolated PostgreSQL smoke for notification recovery and reload fencing."""
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import re
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

from ctn_data import agent_maintenance as maintenance
from ctn_data.agent_control import AgentController, AgentError, AgentStore
from ctn_data.db import connect, utc_now
from tests.verify_agent_interaction_policy_postgres import main as isolated_main


PREFIX = 'ctn_agent_reliability_test_'


def run(target, database_name, report):
    assert re.fullmatch(PREFIX + r'[0-9a-f]{32}', database_name), 'unexpected reliability database'
    with closing(connect(target)) as conn:
        assert conn.execute('SELECT current_database()').fetchone()[0] == database_name, 'reliability database mismatch'
    checks = report['checks']
    sdk = Mock(side_effect=AssertionError('real social SDK access is forbidden'))
    provider = {'id': 'fixture-reliability', 'protocol': 'openai_chat', 'model': 'fixture-model'}
    with tempfile.TemporaryDirectory(prefix='ctn-agent-reliability-') as directory:
        controller = AgentController(target, Path(directory) / 'keys',
            provider_store=SimpleNamespace(get=lambda _: provider), sdk_factory=sdk)
        try:
            controller.save({'name': 'Fixture Reader', 'username': 'fixture_reader', 'provider_id': provider['id']})
            aid = next(iter(controller.store.read()['agents']))
            sources = [{'id': 'post:' + str(pid), 'kind': 'post', 'post_kind': 'post',
                        'post_id': pid, 'author_id': author, 'text': 'Isolated discussion',
                        'version': 'fixture-v1', 'notification_ids': [notice]}
                       for pid, author, notice in ((800, 99, 101), (801, 98, 102))]
            def seed(data, conn):
                data['agents'][aid].update(account_id=11, account_state='ready')
                data['memory'][aid] = {'inbox': [
                    {'id': 101, 'kind': 'reply', 'post_id': 800, 'actor_account_id': 99, 'status': 'unresolved'},
                    {'id': 102, 'kind': 'reply', 'post_id': 801, 'actor_account_id': 98, 'status': 'unresolved'}]}
            controller.store.change(seed)
            output = {'action': 'reply', 'body': 'A valid response to the other discussion.',
                      'target_post_id': 801, 'source_ids': ['post:801'], 'notification_ids': [101],
                      'reason': 'The selected discussion is relevant.', 'memory': ''}
            with patch.object(controller, '_read_sources', return_value=sources) as mock_read_sources, \
                    patch('ctn_data.agent_budget.model_call', return_value={
                        'text': json.dumps(output), 'model': 'fixture-model', 'usage': {}}) as mock_model, \
                    patch('ctn_data.ai_providers.complete', side_effect=AssertionError('real model access is forbidden')) as live_model:
                controller.run(aid)
            data = controller.store.read()
            generated = next(iter(data['drafts'].values()))
            assert generated['state'] == 'pending' and generated['target_post_id'] == 801, 'valid mismatched-notice action was discarded'
            assert generated['notification_ids'] == [], 'wrong-target notification remained attached'
            assert generated['notification_binding_adjustment']['dropped_notification_ids'] == [101], 'binding adjustment was not audited'
            own = data['memory'][aid]
            assert own['inbox'][0]['status'] == 'unresolved', 'original notice was resolved'
            assert 101 not in own.get('decision_notification_ids', []), 'original notice was consumed by the wrong-target decision'
            assert mock_model.call_count == mock_read_sources.call_count == 1, 'mocked decision did not use the real controller path'
            live_model.assert_not_called()
            checks['mismatched_notification_keeps_valid_action_and_leaves_original_unresolved'] = True

            uncertain_id = str(uuid.uuid4())
            publisher = {'owner': 'previous-process', 'token': 'previous-publisher', 'until': time.time() - 1}
            uncertain = {'id': uncertain_id, 'agent_id': aid, 'account_id': 11,
                         'generation_job_id': 'expired-job', 'state': 'uncertain', 'action': 'reply',
                         'body': 'An already attempted action; never resend automatically.',
                         'created_at': utc_now(), 'post_id': None, 'version': 1}
            def stale(data, conn):
                data['agents'][aid]['generation'] = {'id': 'expired-job', 'owner': 'previous-process',
                    'lease_token': 'previous-role', 'status': 'running', 'stage': 'headers',
                    'heartbeat_until': time.time() - 1}
                data['leases']['agent:' + aid] = {'owner': 'previous-process', 'token': 'previous-role',
                    'generation_job_id': 'expired-job', 'until': time.time() + 255}
                data['leases']['publisher'] = copy.deepcopy(publisher)
                data['drafts'][uncertain_id] = copy.deepcopy(uncertain)
                return controller.generations.recover(data)
            assert controller.store.change(stale) == 1, 'expired generation was not recovered'
            data = controller.store.read()
            assert 'agent:' + aid not in data['leases'], 'proven expired generation lease was retained'
            assert data['agents'][aid]['generation']['role_lease_released'], 'lease release was not recorded'
            assert data['drafts'][uncertain_id] == uncertain and data['leases']['publisher'] == publisher, 'recovery changed uncertain delivery audit'
            checks['expired_bidirectionally_bound_lease_is_released_without_touching_uncertain_delivery'] = True

            replacement = {'owner': 'replacement-process', 'token': 'replacement-role',
                           'generation_job_id': 'replacement-job', 'until': time.time() + 120}
            def replaced(data, conn):
                data['agents'][aid]['generation'] = {'id': 'expired-job-2', 'owner': 'previous-process',
                    'lease_token': 'previous-role-2', 'status': 'running', 'stage': 'headers',
                    'heartbeat_until': time.time() - 1}
                data['leases']['agent:' + aid] = copy.deepcopy(replacement)
                controller.generations.recover(data)
            controller.store.change(replaced)
            data = controller.store.read()
            assert data['leases']['agent:' + aid] == replacement, 'recovery removed a replacement lease'
            assert data['agents'][aid]['generation']['recovery_waiting_for_lease'], 'conservative recovery wait was not exposed'
            checks['replacement_owner_token_and_job_lease_remains_unchanged'] = True
            def clear_replacement(data, conn):
                assert data['leases']['agent:' + aid] == replacement, 'fixture replacement lease unexpectedly changed'
                del data['leases']['agent:' + aid]
            controller.store.change(clear_replacement)

            # Match the deployed CLI path: fixtures create the schema once,
            # whereas every maintenance connection must open it without DDL.
            with patch('ctn_data.agent_control.initialize', side_effect=AssertionError('maintenance attempted schema initialization')):
                maintenance_store = AgentStore.open_existing(target)
            cli_checks = 0
            for enabled in (True, False):
                mid = str(uuid.uuid4())
                controller.settings({'enabled': enabled})
                begun = maintenance.begin(maintenance_store, mid)
                assert begun['active'] and not controller.store.read()['settings']['enabled'], 'maintenance did not stop new work'
                captured = StringIO()
                with patch('sys.argv', ['agent-maintenance', '--secret-file', 'fixture-only', 'status', '--id', mid]), \
                        patch('ctn_data.secret_files.read_secret', return_value=target), \
                        patch('ctn_data.agent_control.initialize', side_effect=AssertionError('CLI attempted schema initialization')), \
                        patch('ctn_data.agent_audit.initialize', side_effect=AssertionError('CLI attempted audit initialization')), \
                        redirect_stdout(captured):
                    maintenance.main()
                cli_status = json.loads(captured.getvalue())
                assert cli_status['ok'] and cli_status['active'] and cli_status['id'] == mid, 'real maintenance CLI could not open existing PostgreSQL state'
                cli_checks += 1
                try:
                    controller.start_generation(aid)
                except AgentError as exc:
                    assert exc.status_code == 409 and '维护' in str(exc), 'maintenance generation gate returned the wrong failure'
                else:
                    raise AssertionError('maintenance admitted a new generation')
                try:
                    with controller._claim(aid):
                        raise AssertionError('maintenance admitted a new role claim')
                except AgentError as exc:
                    assert exc.status_code == 409, 'maintenance role gate returned the wrong status'
                drained = maintenance.wait_drained(maintenance_store, mid, timeout=0)
                assert drained['drained'] and drained['phase'] == 'drained', 'maintenance failed to drain/fence idle work'
                fenced = controller.store.read()['leases']['agent:' + aid]
                assert fenced['owner'] == 'maintenance' and fenced['maintenance_id'] == mid, 'role fence was not persisted'
                finished = maintenance.finish(maintenance_store, mid)
                assert not finished['active'] and controller.store.read()['settings']['enabled'] is enabled, 'maintenance did not restore the original run preference'
                assert 'agent:' + aid not in controller.store.read()['leases'], 'maintenance left its role fence behind'
            checks['maintenance_begin_blocks_new_generation_and_claim_then_fences_drained_work'] = True
            checks['maintenance_finish_restores_both_original_enabled_preferences'] = True

            controller.settings({'enabled': True})
            mid = str(uuid.uuid4())
            maintenance.begin(maintenance_store, mid)
            with patch('ctn_data.agent_control.initialize', side_effect=AssertionError('fence contender attempted schema initialization')):
                stores = [AgentStore.open_existing(target), AgentStore.open_existing(target)]
            barrier = threading.Barrier(2)
            legacy_token = str(uuid.uuid4())
            def fence():
                barrier.wait(timeout=5)
                return 'fenced' if maintenance.fence_drained(stores[0], mid) is not None else 'busy'
            def legacy_claim():
                barrier.wait(timeout=5)
                def claim(data, conn):
                    if data['leases'].get('agent:' + aid, {}).get('until', 0) > time.time():
                        raise AgentError('legacy role is busy', 409)
                    data['leases']['agent:' + aid] = {'owner': 'legacy-worker', 'token': legacy_token,
                                                     'until': time.time() + 120}
                try:
                    stores[1].change(claim)
                    return 'claimed'
                except AgentError as exc:
                    assert exc.status_code == 409, 'legacy claim returned the wrong failure'
                    return 'busy'
            with ThreadPoolExecutor(max_workers=2) as executor:
                fencing = executor.submit(fence)
                claiming = executor.submit(legacy_claim)
                fence_result, claim_result = fencing.result(timeout=10), claiming.result(timeout=10)
            assert (fence_result, claim_result) in (('fenced', 'busy'), ('busy', 'claimed')), 'concurrent fence and legacy claim both won or both lost'
            winner = controller.store.read()['leases']['agent:' + aid]
            if claim_result == 'claimed':
                assert winner['token'] == legacy_token and winner['owner'] == 'legacy-worker', 'maintenance overwrote the winning legacy claim'
                def release_legacy(data, conn):
                    assert data['leases']['agent:' + aid]['token'] == legacy_token, 'legacy winner changed before fixture cleanup'
                    del data['leases']['agent:' + aid]
                controller.store.change(release_legacy)
            else:
                assert winner['owner'] == 'maintenance' and winner['maintenance_id'] == mid, 'legacy claim overwrote the winning maintenance fence'
            maintenance.wait_drained(maintenance_store, mid, timeout=0)
            maintenance.finish(maintenance_store, mid)
            assert controller.store.read()['settings']['enabled'] is True, 'concurrent fence completion lost the original preference'
            checks['concurrent_postgres_fence_and_legacy_claim_have_exactly_one_winner'] = True
            report['concurrent_outcomes'] = {'fence': fence_result, 'legacy_claim': claim_result}
            after = controller.store.read()
            assert after['drafts'][uncertain_id] == uncertain and after['leases']['publisher'] == publisher, 'maintenance touched retained delivery audit'
            assert after['memory'][aid]['inbox'][0]['status'] == 'unresolved', 'maintenance resolved the original notice'
            checks['all_maintenance_paths_preserve_uncertain_audit_and_unresolved_notification'] = True
            sdk.assert_not_called()
            report.update(external_http_calls=0, real_model_calls=0,
                          mock_model_calls=mock_model.call_count, mock_read_sources_calls=mock_read_sources.call_count,
                          maintenance_store_mode='open_existing', maintenance_cli_status_checks=cli_checks)
        finally:
            controller.stop()


if __name__ == '__main__':
    isolated_main(run_checks=run, database_prefix=PREFIX,
                  default_report='reports/generated/agent-reliability-postgres.json')
