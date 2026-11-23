"""Measure old/new local contention in a disposable PostgreSQL database.

The legacy comparison reconstructs the former synchronous progress path with
AgentStore.change; all DNS, sockets, HTTP and model responses remain synthetic.
An explicit 20 second setup budget is exercised, irrespective of defaults.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import re
import statistics
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from ctn_data import agent_budget, ai_providers
from ctn_data.agent_control import AgentController, AgentStore, PREFIX
from ctn_data.agent_generation import GenerationManager, _Task
from ctn_data.db import begin_write, connect, utc_now
from tests.verify_agent_interaction_policy_postgres import main as isolated_main


DATABASE_PREFIX = 'ctn_agent_contention_test_'


class _TraceCursor:
    def __init__(self, cursor, counters, lock):
        self.cursor, self.counters, self.lock = cursor, counters, lock

    def __getattr__(self, key):
        return getattr(self.cursor, key)

    def __iter__(self):
        for row in self.cursor:
            # Fixture payloads are ASCII, so character and UTF-8 byte counts
            # match. Never include source/body values in the report.
            with self.lock:
                self.counters['meta_selected_bytes'] += len(row['value'])
            yield row


class _TraceConnection:
    def __init__(self, connection, counters, lock):
        self.connection, self.counters, self.lock = connection, counters, lock

    def __getattr__(self, key):
        return getattr(self.connection, key)

    def execute(self, query, parameters=None):
        before = time.perf_counter()
        cursor = self.connection.execute(query, parameters)
        with self.lock:
            if 'pg_advisory_xact_lock' in query:
                self.counters['advisory_wait_ms'].append((time.perf_counter() - before) * 1000)
            if query.startswith('INSERT INTO meta'):
                self.counters['meta_upserts'] += 1
        if query.startswith('SELECT key,value FROM meta'):
            return _TraceCursor(cursor, self.counters, self.lock)
        return cursor

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)


def _metrics(values):
    return {'count': len(values), 'median_ms': round(statistics.median(values), 3),
            'max_ms': round(max(values), 3), 'total_ms': round(sum(values), 3)}


def _fixture(target, store):
    owner = str(uuid.uuid4())
    now = time.time()
    agents, leases, tasks = {}, {}, []
    for index in range(4):
        aid, job_id, token = 'fixture-role-' + str(index), str(uuid.uuid4()), str(uuid.uuid4())
        agents[aid] = {'id': aid, 'name': 'Role ' + str(index), 'account_id': 100 + index,
            'persona_revision': 1, 'version': 1, 'generation': {'id': job_id, 'owner': owner,
                'lease_token': token, 'status': 'running', 'stage': 'reading',
                'heartbeat_until': now + 45, 'started_at': utc_now(), 'elapsed_seconds': 0}}
        leases['agent:' + aid] = {'owner': owner, 'token': token, 'generation_job_id': job_id,
                                'until': now + 300}
        tasks.append(_Task(aid, job_id, False, lease_token=token))
    state = {'settings': {'enabled': False}, 'agents': agents, 'leases': leases, 'events': [],
        'drafts': {'history': {'id': 'history', 'agent_id': tasks[0].agent_id,
            'origin': 'manual', 'state': 'published', 'body': 'x' * 12_000_000}},
        'memory': {tasks[0].agent_id: {'summary': 'x' * 3_000_000}}, 'rate': {}, 'operations': {}}
    with closing(connect(target)) as connection:
        with connection:
            connection.execute('DELETE FROM meta WHERE key LIKE ?', (PREFIX + '%',))
            connection.execute('DELETE FROM agent_audit_events')
            for key, value in state.items():
                connection.execute('INSERT INTO meta(key,value) VALUES(?,?)',
                                   (PREFIX + key, json.dumps(value)))
    # Normalize legacy defaults once outside the measured window. Both paths
    # then start from warmed application state: measurements must not credit
    # the new path for avoiding a one-time business migration.
    store.change(lambda data, connection: None)
    controller = SimpleNamespace(store=store, owner=owner, clock=time.time)
    controller._event = lambda *a, **kw: AgentController._event(controller, *a, **kw)
    manager = GenerationManager(controller, heartbeat_interval=60, progress_interval=.01)
    return controller, manager, tasks


def _budget_comparison(target, store, legacy):
    controller, _, _ = _fixture(target, store)
    counters = {'meta_selected_bytes': 0, 'meta_upserts': 0, 'advisory_wait_ms': []}
    lock = threading.Lock()
    latencies = []

    def writer(index):
        before = time.perf_counter()
        bucket = 'llm:fixture-' + str(index)
        agent_budget.reserve(controller, bucket)
        agent_budget._record_usage(controller, bucket, 'attempts')
        with lock:
            latencies.append((time.perf_counter() - before) * 1000)

    with patch('ctn_data.agent_control.connect', side_effect=lambda t: _TraceConnection(connect(t), counters, lock)):
        if legacy:
            with patch.object(store, 'change_budget', side_effect=store.change):
                with ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(writer, range(4)))
        else:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(writer, range(4)))
    counters['advisory_wait'] = _metrics(counters.pop('advisory_wait_ms'))
    counters['per_role_reserve_and_usage'] = _metrics(latencies)
    return counters


def _progress_comparison(target, store, legacy):
    _, manager, tasks = _fixture(target, store)
    counters = {'meta_selected_bytes': 0, 'meta_upserts': 0, 'advisory_wait_ms': []}
    lock = threading.Lock()
    blocker = connect(target)
    ready = threading.Barrier(5)
    workers = []
    flushed = threading.Condition()
    saved = [0]
    latencies = []
    original_runtime = store.change_runtime

    def runtime(callback, **options):
        value = original_runtime(callback, **options)
        with flushed:
            saved[0] += 1
            flushed.notify_all()
        return value

    def invoke(task):
        ready.wait(timeout=5)
        before = time.perf_counter()
        if legacy:
            # Former _on_progress -> _touch -> full AgentStore.change. This
            # is the first callback for each role, so the old 3s throttle did
            # not suppress any of these four writes.
            manager._touch(task, {'stage': 'connect', 'received_bytes': 0})
        else:
            manager._on_progress(task, {'stage': 'connect', 'received_bytes': 0})
        with lock:
            latencies.append((time.perf_counter() - before) * 1000)

    try:
        begin_write(blocker, 'agent_control')
        with patch('ctn_data.agent_control.connect', side_effect=lambda t: _TraceConnection(connect(t), counters, lock)), \
                patch.object(store, 'change_runtime', side_effect=store.change if legacy else runtime), \
                ThreadPoolExecutor(max_workers=4) as pool:
            if not legacy:
                for task in tasks:
                    thread = threading.Thread(target=manager._heartbeat, args=(task,), daemon=True)
                    workers.append(thread)
                    thread.start()
            futures = [pool.submit(invoke, task) for task in tasks]
            ready.wait(timeout=5)
            held_at = time.perf_counter()
            time.sleep(.2)
            held_ms = (time.perf_counter() - held_at) * 1000
            if not legacy:
                assert all(future.done() for future in futures), 'async progress waited for a database lock'
            blocker.commit()
            for future in futures:
                future.result(timeout=15)
            if not legacy:
                with flushed:
                    assert flushed.wait_for(lambda: saved[0] >= 4, timeout=5), 'coalesced progress was not persisted'
            for task in tasks:
                task.heartbeat_stop.set()
            for worker in workers:
                worker.join(timeout=3)
        state = store.read_runtime()
        assert all(state['agents'][task.agent_id]['generation']['stage'] == 'connect' for task in tasks)
        assert all(not task.cancellation.is_set() for task in tasks)
    finally:
        blocker.rollback()
        blocker.close()
        for task in tasks:
            task.heartbeat_stop.set()
        for worker in workers:
            worker.join(timeout=3)
    counters['advisory_wait'] = _metrics(counters.pop('advisory_wait_ms'))
    counters['progress_callback'] = _metrics(latencies)
    counters['explicit_database_lock_hold_ms'] = round(held_ms, 3)
    counters['progress_database_transactions'] = 4 if legacy else saved[0]
    counters['legacy_20s_budget_lost_to_local_callback_ms'] = round(max(latencies), 3) if legacy else 0
    return counters


def _twenty_second_transport_under_db_lock(target, store):
    _, manager, tasks = _fixture(target, store)
    task = tasks[0]
    at_dns = []
    callback_ms = []

    class Socket:
        def settimeout(self, value):
            pass
        def close(self):
            pass
        def shutdown(self, how):
            pass

    class Response:
        status = 200
        def __init__(self):
            self.pending = True
        def getheader(self, key):
            return 'application/json' if key == 'Content-Type' else None
        def read1(self, size):
            if self.pending:
                self.pending = False
                return b'{"fixture":true}'
            return b''

    class Connection:
        def __init__(self, *a, **kw):
            pass
        def request(self, *a, **kw):
            pass
        def getresponse(self):
            return Response()
        def close(self):
            pass

    def progress(value):
        before = time.perf_counter()
        manager._on_progress(task, value)
        callback_ms.append((time.perf_counter() - before) * 1000)

    def resolve(host, port, deadline, **options):
        at_dns.append(deadline - time.monotonic())
        return '127.0.0.1'

    blocker = connect(target)
    try:
        begin_write(blocker, 'agent_control')
        with patch.object(ai_providers, '_resolve_address', side_effect=resolve), \
                patch.object(ai_providers.socket, 'create_connection', return_value=Socket()), \
                patch.object(ai_providers.http.client, 'HTTPConnection', Connection):
            before = time.perf_counter()
            result = ai_providers._request_json('http://127.0.0.1:12345/fixture', b'{}', {},
                wait_for_completion=True, connection_timeout_seconds=20,
                cancellation_event=task.cancellation, on_progress=progress)
            duration_ms = (time.perf_counter() - before) * 1000
        # Blocker remains held through the entire DNS/connect/send/body path.
        assert blocker.in_transaction and result['fixture'] is True
    finally:
        blocker.rollback()
        blocker.close()
    assert max(callback_ms) < 50, 'database blocked a transport callback'
    assert at_dns and at_dns[0] > 19.5, 'local database work consumed the explicit 20s setup budget'
    return {'explicit_setup_budget_seconds': 20, 'remaining_at_dns_seconds': round(at_dns[0], 6),
        'fake_transport_elapsed_ms': round(duration_ms, 3), 'callback_latency': _metrics(callback_ms),
        'database_lock_held_for_entire_transport': True,
        'real_network_or_model_calls': 0, 'diagnostics': result.get('_diagnostics', {})}


def run(target, database_name, report):
    assert re.fullmatch(DATABASE_PREFIX + r'[0-9a-f]{32}', database_name), 'unexpected contention test database'
    with closing(connect(target)) as connection:
        assert connection.execute('SELECT current_database()').fetchone()[0] == database_name
    store = AgentStore(target)
    report['fixture'] = {'roles': 4, 'draft_material_bytes': 12_000_000,
        'memory_material_bytes': 3_000_000, 'real_lock_hold_seconds': .2,
        'business_defaults_normalized_before_each_measured_phase': True,
        'legacy_method': 'first synchronous progress callback per role -> full AgentStore.change',
        'new_method': 'memory-only _on_progress -> one background runtime flush per role'}
    report['old_budget'] = _budget_comparison(target, store, legacy=True)
    report['new_budget'] = _budget_comparison(target, store, legacy=False)
    report['old_progress'] = _progress_comparison(target, store, legacy=True)
    report['new_progress'] = _progress_comparison(target, store, legacy=False)
    report['new_transport_at_original_20_second_budget'] = _twenty_second_transport_under_db_lock(target, store)
    assert report['old_progress']['progress_callback']['max_ms'] >= 200
    assert report['new_progress']['progress_callback']['max_ms'] < 50
    assert report['new_budget']['meta_selected_bytes'] < report['old_budget']['meta_selected_bytes'] / 100
    assert report['new_progress']['meta_selected_bytes'] < report['old_progress']['meta_selected_bytes'] / 100
    report['checks'].update({'legacy_callback_really_waited_for_postgres_lock': True,
        'new_callback_returns_without_waiting_for_postgres': True,
        'new_async_progress_eventually_persisted': True,
        'budget_and_progress_skip_large_business_state': True,
        'explicit_original_20_second_network_budget_passed_under_db_lock': True,
        'success_does_not_depend_on_increased_setup_timeout': True})
    report['external_http_calls'] = report['real_model_calls'] = 0


if __name__ == '__main__':
    isolated_main(run_checks=run, database_prefix=DATABASE_PREFIX,
        default_report='reports/generated/agent-heartbeat-contention-postgres.json')
