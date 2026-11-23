"""Small read-only status summary for the navigation shell (GET /api/nav/status).

Counts and booleans only. Each section is computed separately: a failing
section becomes {"error": ...} and the others are still returned. Results are
shared for 30 seconds; while one request refreshes, others get the previous one.
"""
from __future__ import annotations

import json
import threading
import time
from urllib.parse import urlsplit

from .db import connect, utc_now

CACHE_SECONDS = 30
ACTIVE_GENERATION = ('queued', 'running', 'cancelling')
UNFINISHED_DRAFT = ('draft', 'failed', 'uncertain', 'sending')


def _public_status(draft):
    """The status the Agents page shows; see agent_control.snapshot."""
    return {'pending': 'draft', 'rejected': 'failed'}.get(draft.get('state'), draft.get('state'))


def unfinished_draft(draft, agent):
    """Same rule as unfinishedDraft() in frontend/src/pages/Agents.tsx."""
    status = _public_status(draft)
    if status == 'skipped' and draft.get('policy_blocked'):
        return False
    return status in UNFINISHED_DRAFT or (
        draft.get('action') == 'skip' and status == 'skipped' and bool(draft.get('notification_ids'))
        and not draft.get('notifications_resolved') and not draft.get('notification_confirmation_authorized_at')
        and ((agent or {}).get('mode') == 'review' or bool(draft.get('requires_review'))))


def provider_ready(provider):
    """Same rule as providerReady() in frontend/src/pages/aiProviders.ts."""
    if not isinstance(provider, dict) or not provider.get('enabled'):
        return False
    try:
        local = urlsplit(provider.get('base_url') or '').hostname in ('localhost', '127.0.0.1', '::1')
    except ValueError:
        local = False
    return bool(provider.get('has_key') or local)


def _agents(conn, controller):
    from . import agent_control, agent_notifications
    from .agent_interaction_policy import blocked_account_ids, blocked_usernames
    prefix = agent_control.PREFIX
    # Only the parts this summary needs: no memory views, events or rate history.
    data = controller.store._load_selected(conn, ('settings', 'agents', 'drafts'))
    data['settings'] = {**agent_control.DEFAULT_SETTINGS, **data['settings']}
    rate = conn.execute('SELECT value FROM meta WHERE key=?', (prefix + 'rate',)).fetchone()
    maintenance = (json.loads(rate['value']) if rate else {}).get('maintenance') or {}
    agents = data['agents']
    pending = [draft for draft in data['drafts'].values()
               if unfinished_draft(draft, agents.get(draft.get('agent_id')))]
    inbox_unresolved = 0
    data['memory'] = {}
    for row in conn.execute('SELECT key,value FROM meta WHERE key LIKE ?', (prefix + 'memory/%/inbox',)):
        agent_id = row['key'][len(prefix):].split('/')[1]
        agent = agents.get(agent_id, {'id': agent_id})
        blocked = names = set()
        if data['settings'].get('block_agent_interactions', True):
            blocked, names = blocked_account_ids(conn, data, agent), blocked_usernames(conn, data, agent)
        for notice in json.loads(row['value']) or []:
            # Same rule as notificationUnfinished() on the Agents page.
            if notice.get('status') not in ('unresolved', 'ack_pending') or notice.get('policy_ignored'):
                continue
            if agent_notifications.notice_is_policy_ignored(data, agent_id, notice, conn,
                                                             blocked_ids=blocked, blocked_names=names):
                continue
            inbox_unresolved += 1
    ids = list(agents)
    last_error = conn.execute("SELECT MAX(at) FROM agent_audit_events WHERE kind='error' AND agent_id IN ("
                              + ','.join('?' for _ in ids) + ')', ids).fetchone()[0] if ids else None
    return {'team_enabled': bool(data['settings'].get('enabled')),
            'maintenance': {'active': maintenance.get('active') is True,
                            'reason': maintenance.get('reason') if maintenance.get('active') is True else None},
            'roles_total': len(agents), 'roles_enabled': sum(bool(a.get('enabled')) for a in agents.values()),
            'roles_running': sum((a.get('generation') or {}).get('status') in ACTIVE_GENERATION for a in agents.values()),
            'roles_with_error': sum(bool(a.get('last_error')) for a in agents.values()),
            'linked_accounts': sum(bool(a.get('account_id')) for a in agents.values()),
            'max_accounts': data['settings'].get('max_accounts'),
            'pending_actions': len(pending),
            'uncertain_actions': sum(draft.get('state') == 'uncertain' for draft in pending),
            'inbox_unresolved': inbox_unresolved, 'last_error_at': last_error}, data['settings']


def _review(controller, settings, public_providers):
    from . import agent_review
    config = agent_review._configuration({'settings': settings})
    provider = next((row for row in public_providers if row.get('id') == config['provider_id']), None)
    connected = bool(agent_review._get_provider(controller, config['provider_id'])) and provider_ready(provider)
    enabled = bool(config['enabled'])
    return {'enabled': enabled, 'skip_all': not enabled, 'connection_ok': connected,
            'outbound_paused': enabled and not connected}


def _facts(conn, settings):
    config = settings.get('fact_bank') or {}
    return {'pending': conn.execute("SELECT COUNT(*) FROM facts WHERE status='pending'").fetchone()[0],
            'approval': config.get('review_mode', 'manual'), 'extraction_enabled': bool(config.get('extract_enabled')),
            'pipeline': config.get('pipeline', 'single')}


def _research(research):
    snapshot = research.snapshot()
    status, config = snapshot['status'], snapshot['settings']
    state = 'running' if status.get('running') else 'failed' if status.get('last_error') else 'idle'
    return {'enabled': bool(config.get('enabled')), 'mode': config.get('mode'), 'state': state,
            'last_finished_at': status.get('last_finished_at'), 'next_due_at': status.get('next_run_at')}


def _collection(conn, scheduler):
    from .queries import request_traffic
    status = scheduler.status()
    errors = [f'{lane}: {error}' for lane, error in (status.get('lane_errors') or {}).items() if error]
    return {'scheduler_enabled': bool(status.get('enabled')),
            # Our definition: collection is switched on and no lane reports an error.
            'healthy': bool(status.get('enabled')) and not errors, 'lane_errors': errors,
            'last_finished_at': status.get('last_finished_at'),
            'rate_limited_5m': request_traffic(conn)['current']['rate_limited']}


def _score_sync(conn):
    from .browser_status import collection_status, latest_portal
    values = {row['key']: row['value'] for row in conn.execute(
        "SELECT key,value FROM meta WHERE key IN ('browser_extension_id','browser_heartbeat')")}
    portal, ingested_at = latest_portal(conn)
    status = collection_status(values.get('browser_extension_id'), json.loads(values.get('browser_heartbeat', 'null')),
                               portal, ingested_at)
    return {key: status.get(key) for key in ('healthy', 'state', 'label', 'last_collected_at')}


def build(db_path, *, controller, providers, research, scheduler):
    result = {'generated_at': utc_now()}

    def section(name, compute, *, database=False):
        # Each database section has its own connection: a failed statement must
        # not abort the transaction the other sections read in.
        try:
            if not database:
                result[name] = compute()
                return
            conn = connect(db_path)
            try:
                result[name] = compute(conn)
            finally:
                conn.close()
        except Exception as exc:
            result[name] = {'error': type(exc).__name__}

    settings, public = {}, {'providers': []}

    def agents(conn):
        nonlocal settings
        summary, settings = _agents(conn, controller)
        return summary

    def ai():
        nonlocal public
        public = providers.list_public()
        return {'providers': len(public['providers']), 'default_ok': provider_ready(
            next((row for row in public['providers'] if row.get('is_default')), None))}

    section('agents', agents, database=True)
    section('ai', ai)
    section('review', lambda: _review(controller, settings, public['providers']))
    section('facts', lambda conn: _facts(conn, settings), database=True)
    section('research', lambda: _research(research))
    section('collection', lambda conn: _collection(conn, scheduler), database=True)
    section('score_sync', _score_sync, database=True)
    return result


class NavStatus:
    def __init__(self, db_path, **sources):
        self.db_path, self.sources = db_path, sources
        self._lock = threading.Lock()
        self._cached = None

    def get(self):
        cached = self._cached
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        if not self._lock.acquire(blocking=cached is None):
            return cached[1]
        try:
            cached = self._cached
            if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
                return cached[1]
            value = build(self.db_path, **self.sources)
            self._cached = (time.monotonic(), value)
            return value
        finally:
            self._lock.release()
