"""Reload holds fence new work without interrupting existing operations."""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from ctn_data import agent_maintenance as maintenance
from ctn_data import content_research_schedule as research_schedule
from ctn_data.agent_control import AgentController, AgentError, AgentStore
from ctn_data.db import begin_write, connect


class MaintenanceWorkflows(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.controller = AgentController(Path(temporary.name) / 'test.db', Path(temporary.name) / 'keys')
        self.addCleanup(self.controller.stop)
        self.controller.save({'name': 'Reader', 'username': 'reader_fixture'})
        self.aid = next(iter(self.controller.store.read()['agents']))
        self.store = self.controller.store
        self.mid = str(uuid.uuid4())

    def hold(self, enabled=True):
        self.controller.settings({'enabled': enabled})
        return maintenance.begin(self.store, self.mid)

    def research_state(self):
        with contextlib.closing(connect(self.store.db_path)) as conn:
            return research_schedule._load(conn)

    def update_research(self, *, settings=None, status=None):
        with contextlib.closing(connect(self.store.db_path)) as conn, conn:
            begin_write(conn, 'content_research')
            state = research_schedule._load(conn)
            state['settings'].update(settings or {})
            state['status'].update(status or {})
            research_schedule._save(conn, state)
        return state

    def test_drain_fences_and_finish_restore_both_run_preferences(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.mid = str(uuid.uuid4())
                held = self.hold(enabled)
                self.assertTrue(held['active'])
                self.assertFalse(self.store.read()['settings']['enabled'])
                drained = maintenance.wait_drained(self.store, self.mid, timeout=0)
                self.assertTrue(drained['drained'])
                self.assertEqual(drained['phase'], 'drained')
                lease = self.store.read()['leases']['agent:' + self.aid]
                self.assertEqual(lease['maintenance_id'], self.mid)
                self.assertGreater(lease['until'], time.time())
                state = self.controller.snapshot()
                self.assertEqual(state['agents'][0]['busy_reason'], 'maintenance')
                self.assertTrue(state['maintenance']['active'])
                result = maintenance.finish(self.store, self.mid)
                self.assertFalse(result['active'])
                self.assertEqual(self.store.read()['settings']['enabled'], enabled)
                self.assertNotIn('agent:' + self.aid, self.store.read()['leases'])
                self.assertTrue(self.store.read()['settings']['block_agent_interactions'])

    def test_existing_operation_can_finish_before_fences_are_acquired(self):
        self.controller.settings({'enabled': True})
        with self.controller._claim(self.aid) as token:
            maintenance.begin(self.store, self.mid)
            self.assertIsNone(maintenance.fence_drained(self.store, self.mid))
            with self.assertRaises(maintenance.MaintenanceError):
                maintenance.wait_drained(self.store, self.mid, timeout=0)
            self.assertEqual(self.store.read()['leases']['agent:' + self.aid]['token'], token)
        self.assertTrue(maintenance.wait_drained(self.store, self.mid, timeout=0)['drained'])
        maintenance.finish(self.store, self.mid)

    def test_new_generation_claim_and_enable_cannot_cross_hold(self):
        self.hold()
        with patch.object(self.controller, '_sdk', side_effect=AssertionError('No SDK')):
            with self.assertRaisesRegex(AgentError, '维护'):
                self.controller.start_generation(self.aid)
            with self.assertRaisesRegex(AgentError, '维护'):
                with self.controller._claim(self.aid):
                    self.fail('maintenance admitted a new claim')
        with self.assertRaisesRegex(AgentError, '维护'):
            self.controller.settings({'enabled': True})
        with patch.object(self.controller, '_competition_tick') as competition:
            self.controller.tick()
            competition.assert_not_called()
        maintenance.finish(self.store, self.mid, aborted=True)

    def test_timeout_then_abort_restores_original_server_setting(self):
        self.hold()
        self.store.change(lambda data, conn: data['agents'][self.aid].update(generation={
            'id': str(uuid.uuid4()), 'owner': 'fixture', 'status': 'running',
            'heartbeat_until': time.time() + 60}))
        with self.assertRaisesRegex(maintenance.MaintenanceError, '尚未结束'):
            maintenance.wait_drained(self.store, self.mid, timeout=0)
        self.assertTrue(self.store.read()['rate']['maintenance']['active'])
        self.assertNotIn('agent:' + self.aid, self.store.read()['leases'])
        maintenance.finish(self.store, self.mid, aborted=True)
        self.assertTrue(self.store.read()['settings']['enabled'])

    def test_foreign_token_and_duplicate_begin_do_not_overwrite_owner_or_preference(self):
        self.hold()
        original = copy.deepcopy(self.store.read()['rate']['maintenance'])
        maintenance.begin(self.store, self.mid)
        self.assertEqual(self.store.read()['rate']['maintenance'], original)
        for operation in (maintenance.begin, maintenance.finish, maintenance.fence_drained):
            with self.subTest(operation=operation.__name__), self.assertRaises(maintenance.MaintenanceError):
                operation(self.store, str(uuid.uuid4()))
        self.assertEqual(self.store.read()['rate']['maintenance'], original)

    def test_new_lease_between_inspection_and_fence_is_not_deleted(self):
        self.hold()
        self.assertTrue(maintenance.snapshot(self.store.read())['drained'])
        lease = {'owner': 'old-worker', 'token': 'new-claim', 'until': time.time() + 100}
        self.store.change(lambda data, conn: data['leases'].update({'agent:' + self.aid: lease}))
        self.assertIsNone(maintenance.fence_drained(self.store, self.mid))
        self.assertEqual(self.store.read()['leases']['agent:' + self.aid], lease)

    def test_fences_stop_legacy_claim_logic_and_release_only_their_own_leases(self):
        self.hold()
        maintenance.wait_drained(self.store, self.mid, timeout=0)
        def legacy_claim(data, conn):
            if data['leases'].get('agent:' + self.aid, {}).get('until', 0) > time.time():
                raise AgentError('already busy', 409)
            data['leases']['agent:' + self.aid] = {'token': 'unexpected'}
        with self.assertRaises(AgentError):
            self.store.change(legacy_claim)
        replacement = {'owner': 'other', 'token': 'replacement', 'until': time.time() + 100}
        self.store.change(lambda data, conn: data['leases'].update({'agent:' + self.aid: replacement}))
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.finish(self.store, self.mid)
        maintenance.finish(self.store, self.mid, aborted=True)
        self.assertEqual(self.store.read()['leases']['agent:' + self.aid], replacement)

    def test_uncertain_delivery_and_publisher_audit_survive_reload_hold(self):
        did = str(uuid.uuid4())
        publisher = {'owner': 'prior', 'token': 'publisher', 'until': time.time() - 1}
        draft = {'id': did, 'agent_id': self.aid, 'state': 'uncertain', 'action': 'reply',
                 'body': 'A pending check', 'created_at': '2026-10-20T14:00:00Z'}
        self.store.change(lambda data, conn: (
            data['drafts'].update({did: draft}), data['leases'].update(publisher=publisher)))
        self.hold()
        maintenance.wait_drained(self.store, self.mid, timeout=0)
        maintenance.finish(self.store, self.mid)
        self.assertEqual(self.store.read()['drafts'][did]['state'], 'uncertain')
        self.assertEqual(self.store.read()['leases']['publisher'], publisher)

    def test_recovery_snapshot_hides_private_generation_lease_token(self):
        self.store.change(lambda data, conn: (
            data['agents'][self.aid].update(generation={'id': 'old', 'owner': 'prior',
                'status': 'failed', 'lease_token': 'private-role-token'}),
            data['leases'].update({'agent:' + self.aid: {'owner': 'prior', 'token': 'private-role-token',
                'generation_job_id': 'old', 'until': time.time() + 100}})))
        agent = self.controller.snapshot()['agents'][0]
        self.assertTrue(agent['busy'])
        self.assertEqual(agent['busy_reason'], 'lease_recovery')
        self.assertNotIn('lease_token', agent['generation'])

    def test_existing_store_runs_maintenance_without_ddl_or_capability_upgrade(self):
        self.controller.settings({'enabled': True})
        previous_actions = ['post', 'reply', 'repost', 'like', 'skip']
        self.store.change(lambda data, conn: (
            data['rate'].update(action_capabilities_version=1),
            data['agents'][self.aid].update(allowed_actions=previous_actions)))
        with (patch('ctn_data.agent_control.initialize', side_effect=AssertionError('No schema DDL')),
              patch('ctn_data.agent_audit.initialize', side_effect=AssertionError('No audit DDL'))):
            existing = AgentStore.open_existing(self.controller.db_path)
            maintenance.begin(existing, self.mid)
            self.assertTrue(maintenance.wait_drained(existing, self.mid, timeout=0)['drained'])
            maintenance.finish(existing, self.mid)
        data = existing.read()
        self.assertTrue(data['settings']['enabled'])
        self.assertEqual(data['rate']['action_capabilities_version'], 1)
        self.assertEqual(data['agents'][self.aid]['allowed_actions'], previous_actions)

    def test_cli_status_uses_existing_store_and_cannot_run_initialization(self):
        output = io.StringIO()
        argv = ['maintenance', '--secret-file', 'not-read', 'status', '--id', self.mid]
        with patch('sys.argv', argv), patch('ctn_data.secret_files.read_secret', return_value=str(self.controller.db_path)), \
             patch('ctn_data.agent_control.initialize', side_effect=AssertionError('No schema DDL')), \
             patch('ctn_data.agent_audit.initialize', side_effect=AssertionError('No audit DDL')), \
             contextlib.redirect_stdout(output):
            maintenance.main()
        state = json.loads(output.getvalue())
        self.assertTrue(state['ok'])
        self.assertFalse(state['active'])
        self.assertEqual(state['active_research'], 0)

    def test_live_research_blocks_drain_and_finish_without_interrupting_it(self):
        before = self.update_research(
            settings={'enabled': True, 'provider_id': 'fixture', 'auto_activate': True},
            status={'run_id': 'live-research', 'lease_until': time.time() + 60,
                    'cancel_requested': False})
        state = self.hold()
        self.assertEqual(state['active_research'], 1)
        self.assertFalse(state['drained'])
        self.assertIsNone(maintenance.fence_drained(self.store, self.mid))
        with self.assertRaisesRegex(maintenance.MaintenanceError, '尚未结束'):
            maintenance.wait_drained(self.store, self.mid, timeout=0)
        with self.assertRaisesRegex(maintenance.MaintenanceError, '仍有任务'):
            maintenance.finish(self.store, self.mid)
        during = self.research_state()
        self.assertEqual(during['settings'], before['settings'])
        self.assertEqual(during['status']['run_id'], 'live-research')
        self.assertFalse(during['status']['cancel_requested'])
        self.assertEqual(during['status']['maintenance_id'], self.mid)
        self.update_research(status={'run_id': None, 'lease_until': None})
        self.assertTrue(maintenance.wait_drained(self.store, self.mid, timeout=0)['drained'])
        self.assertFalse(maintenance.finish(self.store, self.mid)['active'])
        self.assertEqual(self.research_state()['settings'], before['settings'])
        self.assertNotIn('maintenance_id', self.research_state()['status'])

    def test_expired_research_lease_requires_exclusive_transfer_owner_before_fence(self):
        self.update_research(status={'run_id': 'expired-but-connected',
                                     'lease_until': time.time() - 1})
        with research_schedule.owner_lock(self.store.db_path):
            self.hold()
            self.assertEqual(maintenance.store_snapshot(self.store)['active_research'], 0)
            # An expired heartbeat must never authorize Stop-Process while the
            # model request still owns its independent process/session lock.
            with patch.object(self.store, 'change', side_effect=AssertionError('Lock order')):
                self.assertIsNone(maintenance.fence_drained(self.store, self.mid))
            with self.assertRaisesRegex(maintenance.MaintenanceError, '尚未结束'):
                maintenance.wait_drained(self.store, self.mid, timeout=0)
            with self.assertRaisesRegex(maintenance.MaintenanceError, '仍有内容研究'):
                maintenance.finish(self.store, self.mid)
            self.assertEqual(self.store.read()['rate']['maintenance']['phase'], 'draining')
        drained = maintenance.wait_drained(self.store, self.mid, timeout=0)
        self.assertTrue(drained['drained'])
        self.assertEqual(drained['active_research'], 0)
        self.assertEqual(self.research_state()['status']['run_id'], 'expired-but-connected')
        maintenance.finish(self.store, self.mid)

    def test_maintenance_gate_blocks_new_research_and_abort_preserves_preferences(self):
        scheduler = research_schedule.ResearchAutomation(
            self.store.db_path, provider_for=Mock(), prepare=Mock(), run=Mock())
        for enabled in (True, False):
            with self.subTest(research_enabled=enabled):
                self.mid = str(uuid.uuid4())
                before = self.update_research(settings={'enabled': enabled, 'provider_id': 'fixture'},
                                              status={'run_id': None, 'next_run_at': None})
                self.hold()
                self.assertEqual(self.research_state()['status']['maintenance_id'], self.mid)
                self.assertIsNone(scheduler._claim())
                scheduler.tick()
                scheduler.provider_for.assert_not_called()
                scheduler.prepare.assert_not_called()
                scheduler.run.assert_not_called()
                maintenance.finish(self.store, self.mid, aborted=True)
                self.assertEqual(self.research_state()['settings'], before['settings'])
                self.assertNotIn('maintenance_id', self.research_state()['status'])
                claim = scheduler._claim()
                self.assertEqual(claim is not None, enabled)

    def test_research_gate_and_agent_pause_commit_atomically(self):
        self.controller.settings({'enabled': True})
        with patch.object(research_schedule, '_save', side_effect=RuntimeError('test write failure')):
            with self.assertRaises(RuntimeError):
                maintenance.begin(self.store, self.mid)
        data = self.store.read()
        self.assertTrue(data['settings']['enabled'])
        self.assertFalse(data['rate'].get('maintenance', {}).get('active'))
        self.assertNotIn('maintenance_id', self.research_state()['status'])

    def test_abort_only_removes_its_own_research_marker(self):
        self.hold()
        foreign = str(uuid.uuid4())
        self.update_research(status={'maintenance_id': foreign})
        with self.assertRaisesRegex(maintenance.MaintenanceError, '维护状态已改变'):
            maintenance.fence_drained(self.store, self.mid)
        maintenance.finish(self.store, self.mid, aborted=True)
        self.assertEqual(self.research_state()['status']['maintenance_id'], foreign)

    def test_cli_begin_status_and_abort_include_live_research(self):
        self.update_research(status={'run_id': 'live-research', 'lease_until': time.time() + 60})
        for action in ('begin', 'status', 'abort'):
            output = io.StringIO()
            argv = ['maintenance', '--secret-file', 'not-read', action, '--id', self.mid]
            with patch('sys.argv', argv), \
                 patch('ctn_data.secret_files.read_secret', return_value=str(self.store.db_path)), \
                 contextlib.redirect_stdout(output):
                maintenance.main()
            state = json.loads(output.getvalue())
            self.assertTrue(state['ok'])
            self.assertEqual(state['active_research'], 1)
            self.assertFalse(state['drained'])


class ReloadScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('powershell.exe') or shutil.which('pwsh'), 'PowerShell unavailable')
    def test_stop_requires_a_fresh_drained_research_result(self):
        script = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:CTN_RELOAD_TEST_PATH, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Invalid reload script' }
$names = @('Assert-CTNDrained', 'Stop-CTNDrainedListener')
foreach ($name in $names) {
    $definition = $ast.FindAll({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true)
    if ($definition.Count -ne 1) { throw 'Missing guard function' }
    . ([scriptblock]::Create($definition[0].Extent.Text))
}
$ctnMaintenanceId = 'test-maintenance'
$script:stopCalls = 0
$script:waitCalls = 0
function Stop-Process { param($Id, $ErrorAction) $script:stopCalls++ }
function Assert-CTNSameListener { param($Expected) }
function Invoke-CTNMaintenance {
    param($Action)
    if ($Action -ne 'wait') { throw 'Unexpected action' }
    $script:waitCalls++
    if ($script:failWait) { throw 'Research transfer still owns the lock' }
    return $script:fresh
}
function New-Drain {
    return [pscustomobject]@{
        id = $ctnMaintenanceId; active = $true; phase = 'drained'; drained = $true
        active_generations = 0; active_operations = 0; sending_actions = 0
        registering_accounts = 0; active_research = 0
    }
}
$old = New-Drain
foreach ($case in @('research', 'missing_research', 'owner_busy')) {
    $script:fresh = New-Drain
    $script:failWait = $case -eq 'owner_busy'
    if ($case -eq 'research') { $script:fresh.active_research = 1 }
    if ($case -eq 'missing_research') { $script:fresh.PSObject.Properties.Remove('active_research') }
    $blocked = $false
    try { Stop-CTNDrainedListener -Expected ([pscustomobject]@{ ProcessId = 123 }) -DrainState $old }
    catch { $blocked = $true }
    if (-not $blocked -or $script:stopCalls -ne 0) { throw ('Unsafe stop: ' + $case) }
}
$script:fresh = New-Drain
$script:failWait = $false
Stop-CTNDrainedListener -Expected ([pscustomobject]@{ ProcessId = 123 }) -DrainState $old
if ($script:stopCalls -ne 1 -or $script:waitCalls -ne 4) { throw 'Fresh fence was not required' }
Write-Output 'reload-guards-ok'
"""
        environment = dict(os.environ, CTN_RELOAD_TEST_PATH=str(
            Path(__file__).resolve().parents[1] / 'scripts' / 'Reload-CTN.ps1'))
        result = subprocess.run(
            [shutil.which('powershell.exe') or shutil.which('pwsh'),
             '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script],
            capture_output=True, text=True, timeout=30, env=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('reload-guards-ok', result.stdout)


class ExistingStoreFailureTests(unittest.TestCase):
    def test_empty_database_is_rejected_without_initializing_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'empty.sqlite3'
            sqlite3.connect(database).close()
            with self.assertRaises(sqlite3.OperationalError):
                AgentStore.open_existing(database)
            conn = sqlite3.connect(database)
            try:
                self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
            finally:
                conn.close()

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'missing.sqlite3'
            with self.assertRaises(AgentError):
                AgentStore.open_existing(database)
            self.assertFalse(database.exists())

    def test_failure_diagnostics_do_not_echo_raw_errors_or_invalid_sqlstates(self):
        cause = RuntimeError('postgresql://private-user:private-password@server/private-db')
        cause.sqlstate = '55P03'
        failure = sqlite3.OperationalError('private-password')
        failure.__cause__ = cause
        details = maintenance._failure_diagnostics(failure)
        self.assertEqual(details, {'error_module': 'sqlite3', 'cause_type': 'RuntimeError', 'sqlstate': '55P03'})
        self.assertNotIn('private', json.dumps(details))
        cause.sqlstate = 'postgresql://private-password'
        self.assertNotIn('sqlstate', maintenance._failure_diagnostics(failure))


if __name__ == '__main__':
    unittest.main()
