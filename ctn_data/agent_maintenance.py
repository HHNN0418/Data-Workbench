"""Drain agent work before local reloads, retaining the previous run preference.

No SDK, model, or HTTP calls are made here. A maintenance token fences reload
owners; an interrupted reload remains paused until that owner finishes/aborts.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import time
import uuid

from . import content_research_schedule as research_schedule
from .collector import CollectionBusy
from .db import begin_write, connect, utc_now


class MaintenanceError(ValueError):
    pass


def _identifier(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise MaintenanceError('维护任务 ID 无效') from None
    return value


def snapshot(data, now=None, *, research_status=None):
    now = time.time() if now is None else now
    hold = data.get('rate', {}).get('maintenance', {})
    active_jobs = [agent['id'] for agent in data.get('agents', {}).values()
                   if agent.get('generation', {}).get('status') in ('queued', 'running', 'cancelling')
                   and agent.get('generation', {}).get('heartbeat_until', 0) > now]
    active_leases = [key for key, lease in data.get('leases', {}).items()
                     if (key.startswith('agent:') or key == 'publisher')
                     and lease.get('until', 0) > now
                     and not (hold.get('active') and lease.get('maintenance_id') == hold.get('id'))]
    sending = [row['id'] for row in data.get('drafts', {}).values()
               if row.get('state') == 'sending' and row.get('lease_until', 0) > now]
    registering = [row['id'] for row in data.get('agents', {}).values()
                   if row.get('account_state') == 'registering' and row.get('registration_until', 0) > now]
    research_status = research_status or {}
    active_research = int(bool(research_status.get('run_id'))
                          and (research_status.get('lease_until') or 0) > now)
    return {'active': hold.get('active') is True, 'id': hold.get('id'),
            'reason': hold.get('reason', ''), 'phase': hold.get('phase'),
            'started_at': hold.get('started_at'), 'active_generations': len(active_jobs),
            'active_operations': len(active_leases), 'sending_actions': len(sending),
            'registering_accounts': len(registering), 'active_research': active_research,
            'drained': not (active_jobs or active_leases or sending or registering or active_research)}


def store_snapshot(store):
    """Read both work domains; only fence_drained proves an expired owner is gone."""
    with closing(connect(store.db_path)) as conn:
        return snapshot(store._load(conn), research_status=research_schedule._load(conn)['status'])


def _event(data, kind, message):
    data.setdefault('events', []).append({'id': str(uuid.uuid4()), 'at': utc_now(),
        'kind': kind, 'message': message, 'agent_id': None, 'draft_id': None})


def begin(store, maintenance_id, reason='服务更新，正在等待已有任务结束'):
    _identifier(maintenance_id)
    def change(data, conn):
        begin_write(conn, 'content_research')
        research = research_schedule._load(conn)
        research_owner = research['status'].get('maintenance_id')
        if research_owner and research_owner != maintenance_id:
            raise MaintenanceError('另一项内容研究维护正在进行')
        existing = data.setdefault('rate', {}).get('maintenance', {})
        if existing.get('active'):
            if existing.get('id') != maintenance_id:
                raise MaintenanceError('另一项服务维护正在进行')
        else:
            data['rate']['maintenance'] = {'id': maintenance_id, 'active': True, 'phase': 'draining',
                'reason': reason, 'started_at': utc_now(), 'resume_enabled': bool(data['settings']['enabled'])}
            data['settings']['enabled'] = False
            _event(data, 'maintenance_started', '服务更新：已暂停领取新任务，等待已有任务结束')
        research['status']['maintenance_id'] = maintenance_id
        research_schedule._save(conn, research)
        return snapshot(data, research_status=research['status'])
    return store.change(change)


def finish(store, maintenance_id, *, aborted=False):
    _identifier(maintenance_id)
    def change(data, conn):
        begin_write(conn, 'content_research')
        research = research_schedule._load(conn)
        hold = data.setdefault('rate', {}).get('maintenance', {})
        if hold.get('id') != maintenance_id:
            raise MaintenanceError('维护任务已改变，未恢复运行设置')
        if not hold.get('active'):
            return snapshot(data, research_status=research['status'])
        if not aborted and not snapshot(data, research_status=research['status'])['drained']:
            raise MaintenanceError('仍有任务执行中，未恢复运行设置')
        for key, lease in list(data.get('leases', {}).items()):
            if lease.get('maintenance_id') == maintenance_id:
                del data['leases'][key]
        data['settings']['enabled'] = bool(hold['resume_enabled'])
        if research['status'].get('maintenance_id') == maintenance_id:
            research['status'].pop('maintenance_id')
            research_schedule._save(conn, research)
        hold.update(active=False, phase='aborted' if aborted else 'completed', finished_at=utc_now())
        _event(data, 'maintenance_aborted' if aborted else 'maintenance_completed',
               '服务更新已取消，已恢复原运行设置' if aborted else '服务更新已完成，已恢复原运行设置')
        return snapshot(data, research_status=research['status'])
    if aborted:
        return store.change(change)
    try:
        # Keep the same lock order as fence_drained. An expired heartbeat alone
        # is not proof that an existing model transfer has finished.
        with research_schedule.owner_lock(store.db_path):
            return store.change(change)
    except CollectionBusy:
        raise MaintenanceError('仍有内容研究执行中，未恢复运行设置') from None


def fence_drained(store, maintenance_id):
    """Atomically fence even older workers whose only gate is a role lease."""
    _identifier(maintenance_id)
    def change(data, conn):
        begin_write(conn, 'content_research')
        research = research_schedule._load(conn)
        hold = data.get('rate', {}).get('maintenance', {})
        if not hold.get('active') or hold.get('id') != maintenance_id:
            raise MaintenanceError('维护任务已改变，取消重载')
        if data['settings']['enabled']:
            raise MaintenanceError('维护期间运行设置已改变，取消重载')
        if research['status'].get('maintenance_id') != maintenance_id:
            raise MaintenanceError('内容研究维护状态已改变，取消重载')
        if not snapshot(data, research_status=research['status'])['drained']:
            return None
        until = time.time() + 3600
        for agent_id in data.get('agents', {}):
            data['leases']['agent:' + agent_id] = {
                'owner': 'maintenance', 'token': str(uuid.uuid4()),
                'maintenance_id': maintenance_id, 'until': until}
        hold['phase'] = 'drained'
        return snapshot(data, research_status=research['status'])
    try:
        # Acquire the nonblocking transfer-owner lock before either DB domain
        # lock. The durable maintenance marker prevents a new scheduled claim
        # after this proof is released and before the process is stopped.
        with research_schedule.owner_lock(store.db_path):
            return store.change(change)
    except CollectionBusy:
        return None


def wait_drained(store, maintenance_id, *, timeout=300, interval=.5):
    _identifier(maintenance_id)
    deadline = time.monotonic() + timeout
    while True:
        state = store_snapshot(store)
        if not state['active'] or state['id'] != maintenance_id:
            raise MaintenanceError('维护任务已改变，取消重载')
        if state['drained']:
            fenced = fence_drained(store, maintenance_id)
            if fenced is not None:
                return fenced
        if time.monotonic() >= deadline:
            raise MaintenanceError('已有任务尚未结束，取消重载，保留当前服务')
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


def _failure_diagnostics(exc):
    """Return exception identities and SQLSTATE, never connection/error text."""
    cause = exc.__cause__ or exc.__context__
    details = {'error_module': type(exc).__module__}
    if cause is not None:
        details['cause_type'] = type(cause).__name__
    sqlstate, current, seen = None, exc, set()
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        candidate = getattr(current, 'sqlstate', None)
        if isinstance(candidate, str) and re.fullmatch(r'[0-9A-Z]{5}', candidate):
            sqlstate = candidate
            break
        current = current.__cause__ or current.__context__
    if sqlstate:
        details['sqlstate'] = sqlstate
    return details


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--secret-file', type=Path, required=True)
    parser.add_argument('action', choices=('begin', 'status', 'wait', 'finish', 'abort'))
    parser.add_argument('--id', required=True)
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args()
    try:
        _identifier(args.id)
        if not 0 <= args.timeout <= 3600:
            raise MaintenanceError('维护等待时间无效')
        from .agent_control import AgentStore
        from .secret_files import read_secret
        store = AgentStore.open_existing(read_secret(args.secret_file).strip())
        if args.action == 'begin':
            result = begin(store, args.id)
        elif args.action == 'wait':
            result = wait_drained(store, args.id, timeout=args.timeout)
        elif args.action in ('finish', 'abort'):
            result = finish(store, args.id, aborted=args.action == 'abort')
        else:
            result = store_snapshot(store)
        print(json.dumps({'ok': True, **result}, ensure_ascii=False))
    except Exception as exc:
        # Never print a DSN or storage exception containing connection details.
        print(json.dumps({'ok': False, 'error': str(exc) if isinstance(exc, MaintenanceError)
                          else '服务维护状态操作失败', 'error_type': type(exc).__name__,
                          **_failure_diagnostics(exc)}, ensure_ascii=False))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
