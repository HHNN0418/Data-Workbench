"""Disposable PostgreSQL validation of small Agent housekeeping transactions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import statistics
import tempfile
import threading
import time
from unittest.mock import patch
import uuid

from ctn_data.agent_control import AgentController, PREFIX
from ctn_data.db import begin_write, connect
from tests.verify_agent_interaction_policy_postgres import main as isolated_main
from tests.verify_agent_runtime_contention_postgres import _TraceConnection

ROOT = Path(__file__).resolve().parents[1]
DATABASE_PREFIX = 'ctn_bookkeeping_test_'


class TraceConnection(_TraceConnection):
    def __init__(self, connection, counters, lock):
        super().__init__(connection, counters, lock)
        self.lock_acquired = None

    def execute(self, query, parameters=None):
        cursor = super().execute(query, parameters)
        with self.lock:
            if 'pg_advisory_xact_lock' in query:
                self.lock_acquired = time.perf_counter()
            if query.startswith('INSERT INTO meta'):
                self.counters['meta_upsert_bytes'] += len(parameters[1].encode())
        return cursor

    def __exit__(self, *args):
        result = super().__exit__(*args)
        if self.lock_acquired is not None:
            with self.lock:
                self.counters['lock_hold_ms'].append((time.perf_counter() - self.lock_acquired) * 1000)
        return result


def measure(controller, function):
    counters = {'meta_selected_bytes': 0, 'meta_upserts': 0, 'meta_upsert_bytes': 0,
                'advisory_wait_ms': [], 'lock_hold_ms': []}
    lock = threading.Lock()
    with patch('ctn_data.agent_control.connect',
               side_effect=lambda target: TraceConnection(connect(target), counters, lock)):
        started = time.perf_counter()
        function()
        counters['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 3)
    counters['lock_hold_ms'] = [round(value, 3) for value in counters['lock_hold_ms']]
    counters['advisory_wait_ms'] = [round(value, 3) for value in counters['advisory_wait_ms']]
    return counters


def raw_set(target, key, value):
    with closing(connect(target)) as connection:
        with connection:
            connection.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                               (PREFIX + key, value))


def run(target, database_name, report):
    checks = report['checks']
    with closing(connect(target)) as connection:
        assert connection.execute('SELECT current_database()').fetchone()[0] == database_name
    previous = ROOT / 'backups/system-optimization-20261021/runtime-contention/ctn_data/agent_control.py'
    spec = importlib.util.spec_from_file_location('ctn_data._bookkeeping_postgres_baseline', previous)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory(dir=ROOT / '.tmp') as temporary:
        now = [1792548000.0]
        controller = AgentController(target, Path(temporary), clock=lambda: now[0])
        controller.save({'name': 'Synthetic role', 'username': 'pg_bookkeeping_role'})
        aid = next(iter(controller.store.read()['agents']))
        drafts = {}
        for number in range(500):
            did = str(uuid.uuid4())
            drafts[did] = {'id': did, 'agent_id': aid, 'state': 'published', 'action': 'post',
                           'origin': 'manual', 'created_at': '2026-10-20T01:00:00Z',
                           'body': str(number) + 'x' * 30000}
        raw_set(target, 'drafts', json.dumps(drafts))
        raw_set(target, 'memory', json.dumps({aid: {'archive_version': 1, 'summary': 'm' * 2_000_000}}))
        raw_set(target, 'rate', json.dumps({'synthetic_pressure': 'q' * 950_000}))
        controller.store.change(lambda data, connection: None)
        baseline = module.AgentController.__new__(module.AgentController)
        baseline.__dict__ = controller.__dict__.copy()
        report['fixture'] = {'roles': 1, 'drafts': 500, 'draft_json_bytes': len(json.dumps(drafts).encode()),
                             'memory_content_bytes': 2_000_000, 'rate_content_bytes': 950_000}
        samples = {'cooldown_before': [], 'cooldown_after': [], 'gate_before': [], 'gate_after': []}
        for repeat in range(5):
            variants = [('cooldown_before', baseline), ('cooldown_after', controller)]
            if repeat % 2:
                variants.reverse()
            for label, current in variants:
                now[0] += 1
                samples[label].append(measure(current, lambda: current._cooldown(60)))
            samples['gate_before'].append(measure(controller, controller.store.read))
            samples['gate_after'].append(measure(controller, controller.store.read_budget))
        report['measurements'] = samples
        report['medians'] = {key: {
            'elapsed_ms': round(statistics.median(row['elapsed_ms'] for row in rows), 3),
            'meta_selected_bytes': int(statistics.median(row['meta_selected_bytes'] for row in rows)),
            'lock_hold_ms': round(statistics.median([value for row in rows for value in row['lock_hold_ms']]), 3)
                            if any(row['lock_hold_ms'] for row in rows) else None}
            for key, rows in samples.items()}
        checks['cooldown_reads_only_rate'] = all(row['meta_selected_bytes'] < 1_000_000 for row in samples['cooldown_after'])
        checks['maintenance_gate_reads_only_rate'] = all(row['meta_selected_bytes'] < 1_000_000 for row in samples['gate_after'])
        assert checks['cooldown_reads_only_rate'] and checks['maintenance_gate_reads_only_rate']
        state_before = controller.store.read()
        controller._cooldown(90)
        state_after = controller.store.read()
        checks['other_state_unchanged'] = all(state_before[key] == state_after[key] for key in state_before if key != 'rate')
        assert checks['other_state_unchanged']

        # Both paths must still serialize with the ordinary AgentStore lock.
        with closing(connect(target)) as blocker:
            begin_write(blocker, 'agent_control')
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(controller._cooldown, 120)
                    time.sleep(.2)
                    assert not future.done(), 'bookkeeping bypassed the AgentStore writer lock'
                    blocker.commit()
                    future.result(timeout=15)
            finally:
                blocker.rollback()
        checks['shared_agent_control_lock'] = True
        controller.store.change_budget(lambda data, connection: data['rate'].update(maintenance={'active': True}))
        raw_set(target, 'drafts', '{invalid unrelated drafts')
        raw_set(target, 'memory', '{invalid unrelated memory')
        controller.tick()
        controller._cooldown(180)
        checks['maintenance_and_cooldown_skip_invalid_business_json'] = True
        raw_set(target, 'drafts', '{}')
        raw_set(target, 'memory', '{}')
        controller.store.change_budget(lambda data, connection: data['rate'].update(maintenance={'active': False}))
        for replacement in (False, True):
            raw_set(target, 'drafts', '{}')
            with controller._claim(aid):
                raw_set(target, 'drafts', '{invalid unrelated drafts')
                if replacement:
                    controller.store.change_runtime(lambda data, connection: data['leases']['agent:' + aid].update(
                        token='replacement-token', owner='replacement-owner'))
            lease = controller.store.read_runtime()['leases'].get('agent:' + aid)
            if replacement:
                assert lease['token'] == 'replacement-token' and lease['owner'] == 'replacement-owner'
            else:
                assert lease is None
        checks['lease_release_skips_drafts_and_preserves_replacement_owner'] = True
        report.update(external_http_calls=0, real_model_calls=0)


if __name__ == '__main__':
    isolated_main(run_checks=run, database_prefix=DATABASE_PREFIX,
                  default_report='reports/generated/agent-bookkeeping-postgres-20261021.json')
