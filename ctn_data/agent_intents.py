"""Operational intent protection, independent of erasable personal memory.

Only durable drafts and verified delivery states populate this ledger. No model
summary is used as evidence, and loading legacy state never sends or acknowledges
anything.
"""
from __future__ import annotations

import hashlib
import json

ACTIVE = frozenset(('pending', 'rejected', 'sending', 'uncertain'))
TERMINAL = frozenset(('discarded',))
MAX_ARCHIVED = 2000


def _bound_identity(data, draft):
    if draft.get('account_id') is not None:
        return draft
    account_id = data.get('agents', {}).get(draft.get('agent_id'), {}).get('account_id')
    return {**draft, 'account_id': account_id}


def _sources(draft):
    selected = set(draft.get('source_ids') or [])
    return [row for row in (draft.get('sources') or []) if isinstance(row, dict)
            and (not selected or row.get('id') in selected)]


def signature(draft):
    """Same targeted action on the same evidence, even if its wording changes."""
    action = draft.get('action')
    if action not in ('post', 'reply', 'repost', 'like', 'unlike', 'follow', 'unfollow'):
        return ''
    target = draft.get('target_post_id', draft.get('reply_to'))
    account = draft.get('target_account_id')
    sources = _sources(draft)
    if target is not None:
        sources = [s for s in sources if s.get('post_id') == target]
    elif account is not None:
        sources = [s for s in sources if s.get('author_id') == account]
    versions = sorted((str(s.get('id', '')), str(s.get('version') or
        hashlib.sha256(str(s.get('text', s.get('body', ''))).encode()).hexdigest()))
        for s in sources)
    # Independent original ideas may share reading material. Only exact content
    # is deduplicated here; target-based protection is for interactions.
    body = ' '.join(str(draft.get('body', '')).casefold().split()) if action == 'post' else ''
    if action == 'post':
        versions = []
    value = [draft.get('agent_id'), draft.get('account_id'), action, target, account, versions, body]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def remember(data, draft):
    if (not draft.get('agent_id') or not draft.get('id')
            or draft.get('origin') == 'manual' or draft.get('manual_only')):
        return
    key = signature(_bound_identity(data, draft))
    if not key or draft.get('state') not in TERMINAL:
        return
    ledger = data.setdefault('operations', {}).setdefault('intents', {})
    ledger[key] = {
        'agent_id': draft['agent_id'], 'draft_id': draft['id'],
        'action': draft.get('action'), 'target_post_id': draft.get('target_post_id'),
        'target_account_id': draft.get('target_account_id'), 'state': draft['state'],
        'source_versions': {s['id']: s.get('version', '') for s in _sources(draft) if s.get('id')},
        'notification_ids': list(draft.get('notification_ids') or []),
        'at': draft.get('published_at') or draft.get('created_at', ''),
    }


def migrate(data):
    operations = data.setdefault('operations', {})
    operations['version'] = 1
    for draft in data.get('drafts', {}).values():
        remember(data, draft)
    ledger = operations.setdefault('intents', {})
    if len(ledger) <= MAX_ARCHIVED:
        # Nothing can exceed the bound; skip reading every role's inbox (D89).
        return
    # Keep holds associated with unresolved notifications even beyond the
    # archived-history bound; discarding history must not silently reopen them.
    unresolved = {(aid, row.get('id', row.get('notification_id')))
                  for aid, memory in data.get('memory', {}).items() if isinstance(memory, dict)
                  for row in memory.get('inbox', []) if isinstance(row, dict) and row.get('status') != 'resolved'}
    archived = sorted((key for key, row in ledger.items()
                       if not any((row['agent_id'], nid) in unresolved
                                  for nid in row.get('notification_ids', []))),
                      key=lambda key: ledger[key].get('at', ''), reverse=True)
    for key in archived[MAX_ARCHIVED:]:
        del ledger[key]


def conflict(data, draft):
    if draft.get('origin') == 'manual' or draft.get('manual_only'):
        return None
    draft = _bound_identity(data, draft)
    key = signature(draft)
    if not key:
        return None
    for other in data.get('drafts', {}).values():
        other = _bound_identity(data, other)
        if other.get('id') == draft.get('id') or other.get('agent_id') != draft.get('agent_id'):
            continue
        # An edited target does not create room for two simultaneous replies.
        # The existing intention must first finish or be abandoned.
        same_active_target = (other.get('state') in ACTIVE
            and draft.get('action') == other.get('action')
            and draft.get('account_id') == other.get('account_id')
            and ((draft.get('target_post_id') is not None
                  and draft['target_post_id'] == other.get('target_post_id'))
                 or (draft.get('target_account_id') is not None
                     and draft['target_account_id'] == other.get('target_account_id'))))
        if same_active_target or (other.get('state') in ACTIVE | TERMINAL and signature(other) == key):
            return other
    prior = data.get('operations', {}).get('intents', {}).get(key)
    return prior if prior and prior.get('draft_id') != draft.get('id') else None


def claims(data, agent_id, *, abandoned_only=False):
    """Return pending input reservations without restoring forgotten memories."""
    versions, notices = set(), set()
    memory = data.get('memory', {}).get(agent_id, {})
    inbox = {row.get('id', row.get('notification_id')): row
             for row in (memory.get('inbox', []) if isinstance(memory, dict) else []) if isinstance(row, dict)}
    for draft in data.get('drafts', {}).values():
        if draft.get('agent_id') != agent_id:
            continue
        state = draft.get('state')
        if not abandoned_only and (state in ACTIVE or (state == 'skipped' and not draft.get('notifications_resolved'))):
            versions.update((s['id'], s.get('version', '')) for s in _sources(draft) if s.get('id'))
            notices.update(draft.get('notification_ids') or [])
        elif state == 'discarded':
            held = [nid for nid in draft.get('notification_ids', []) if nid in inbox
                    and inbox[nid].get('status') != 'resolved'
                    and inbox[nid].get('reconsidered_draft_id') != draft['id']
                    and draft['id'] not in inbox[nid].get('reconsidered_draft_ids', [])]
            if held:
                notices.update(held)
                versions.update((s['id'], s.get('version', '')) for s in _sources(draft) if s.get('id'))
    return versions, notices


def context_for(data, agent_id):
    """Compact operational constraints; never an autobiographical recollection."""
    rows = [d for d in data.get('drafts', {}).values()
            if d.get('agent_id') == agent_id and d.get('state') in ACTIVE | {'discarded'}]
    return [{'draft_id': d['id'], 'action': d.get('action'), 'state': d['state'],
             'target_post_id': d.get('target_post_id'), 'target_account_id': d.get('target_account_id'),
             'source_ids': d.get('source_ids', []), 'notification_ids': d.get('notification_ids', []),
             'requires_review': bool(d.get('requires_review')),
             'instruction': '不要重建同一待办或改写已放弃的同一回应；可以考虑有依据的其他互动。'}
            for d in sorted(rows, key=lambda d: d.get('created_at', ''), reverse=True)[:30]]
