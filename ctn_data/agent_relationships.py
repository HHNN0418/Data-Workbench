"""Exact, durable social relationships, separate from a role's editable memory.

These observations establish only a follow/like relationship. They do not imply
friendship, agreement or a person's stance. All helpers are local; SDK reads are
performed by the executor before its short state transaction.
"""
from __future__ import annotations

from datetime import datetime, timezone


# Compatibility metadata for older readers; relationship changes have no local
# time restriction. Official API budgets are enforced by agent_budget instead.
COOLDOWN_SECONDS = 0
MAX_FOLLOWING_PAGES = 100


def _epoch(value):
    if isinstance(value, (int, float)):
        return float(value)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def key(account_id, kind, target_id):
    if type(account_id) is not int or account_id <= 0 or type(target_id) is not int or target_id <= 0:
        raise ValueError('relationship IDs must be positive integers')
    if kind not in ('follow', 'like'):
        raise ValueError('unknown relationship kind')
    return f'{account_id}:{kind}:{target_id}'


def observe(data, account_id, kind, target_id, present, observed_at, source, *, changed=False):
    """Save a fresh exact fact; ``None`` explicitly means the read was incomplete."""
    if present is not None and type(present) is not bool:
        raise ValueError('relationship state must be true, false or unknown')
    if _epoch(observed_at) is None:
        raise ValueError('relationship observation requires a timestamp')
    rows = data.setdefault('rate', {}).setdefault('relationships', {})
    relationship_key = key(account_id, kind, target_id)
    old = rows.get(relationship_key, {})
    row = {'account_id': account_id, 'kind': kind, 'present': present,
           'target_account_id': target_id if kind == 'follow' else None,
           'target_post_id': target_id if kind == 'like' else None,
           'observed_at': observed_at, 'source': str(source)[:100],
           'last_changed_at': old.get('last_changed_at')}
    if present is None and type(old.get('present')) is bool:
        row['last_known_present'] = old['present']
        row['last_known_at'] = old.get('observed_at')
    elif present is None:
        for field in ('last_known_present', 'last_known_at'):
            if field in old:
                row[field] = old[field]
    if changed:
        row['last_changed_at'] = observed_at
    rows[relationship_key] = row
    return row


def desired(action):
    return action in ('follow', 'like')


def _already_applied(draft):
    return bool(draft.get('relationship_already_applied') or
                (draft.get('result') or {}).get('already_applied'))


def kind_for(action):
    return 'follow' if action in ('follow', 'unfollow') else 'like'


def target_for(draft):
    return draft.get('target_account_id') if draft.get('action') in ('follow', 'unfollow') else draft.get('target_post_id', draft.get('reply_to'))


def allowed_account_targets(draft, *, include_policy_blocked=False):
    result = {row['author_id'] for row in draft.get('sources', [])
              if isinstance(row, dict) and type(row.get('author_id')) is int and row['author_id'] > 0
              and (include_policy_blocked or not row.get('interaction_blocked'))}
    context = draft.get('relationship_context', {})
    if isinstance(context, dict):
        for row in context.get('relations', []):
            if (isinstance(row, dict) and type(row.get('target_account_id')) is int
                    and (include_policy_blocked or not row.get('interaction_blocked'))):
                result.add(row['target_account_id'])
    return result


def allowed_post_targets(draft, *, include_policy_blocked=False):
    # target_blocked (bot-like author or too deep a thread) is never a target;
    # unlike it is not a policy block that a later review could release.
    result = {row['post_id'] for row in draft.get('sources', [])
              if isinstance(row, dict) and type(row.get('post_id')) is int and row['post_id'] > 0
              and not row.get('target_blocked')
              and (include_policy_blocked or not row.get('interaction_blocked'))}
    if draft.get('action') == 'unlike':
        context = draft.get('relationship_context', {})
        if isinstance(context, dict):
            for row in context.get('relations', []):
                if (isinstance(row, dict) and row.get('kind') == 'like' and row.get('present') is True
                        and type(row.get('target_post_id')) is int
                        and (include_policy_blocked or not row.get('interaction_blocked'))):
                    result.add(row['target_post_id'])
    return result


def with_interaction_policy(conn, data, agent, sources, context):
    """Keep reading evidence while removing internal social targets from choices."""
    from . import agent_interaction_policy
    blocked = agent_interaction_policy.blocked_account_ids(conn, data, agent)
    post_ids = {row['post_id'] for row in sources if type(row.get('post_id')) is int}
    post_ids.update(row['target_post_id'] for row in context.get('relations', [])
                    if type(row.get('target_post_id')) is int)
    authors = {row['post_id']: row['author_id'] for row in sources
               if type(row.get('post_id')) is int and type(row.get('author_id')) is int
               and row['author_id'] > 0}
    if blocked and post_ids:
        placeholders = ','.join('?' for _ in post_ids)
        for row in conn.execute(
                f'SELECT post_id,author_id FROM posts WHERE post_id IN ({placeholders})', tuple(post_ids)):
            if type(row['author_id']) is int and row['author_id'] > 0:
                authors.setdefault(row['post_id'], row['author_id'])
    def mark(row, denied):
        value = {key: item for key, item in row.items() if key != 'interaction_blocked'}
        if denied:
            value['interaction_blocked'] = True
        return value
    marked = [mark(row, row.get('author_id') in blocked or authors.get(row.get('post_id')) in blocked)
              for row in sources]
    relations = [mark(row, row.get('target_account_id') in blocked
                      or authors.get(row.get('target_post_id')) in blocked)
                 for row in context.get('relations', [])]
    return marked, {**context, 'relations': relations}


def cooldown_remaining(data, agent, draft, now):
    """Deprecated compatibility helper: there is no local reversal cooldown."""
    return 0


def record_success(data, draft, agent, observed_at):
    action = draft['action']
    if action not in ('like', 'unlike', 'follow', 'unfollow'):
        return None
    already = _already_applied(draft)
    row = observe(data, agent['account_id'], kind_for(action), target_for(draft), desired(action),
                  observed_at, 'exact authenticated relationship' if already else 'authenticated operation acknowledgement',
                  changed=not already)
    # A later read-only reconciliation did not perform a second mutation.
    # Preserve the actual mutation time instead of its later verification time.
    started = _epoch(draft.get('send_started_at'))
    if not already and started is not None:
        row['last_changed_at'] = datetime.fromtimestamp(started, timezone.utc).isoformat().replace('+00:00', 'Z')
    return row


def snapshot_for(data, agent):
    account_id = agent.get('account_id')
    facts = {relationship_key: dict(row) for relationship_key, row in
             data.get('rate', {}).get('relationships', {}).items()
             if row.get('account_id') == account_id and account_id is not None}
    # Before explicit relationship facts existed, exact authenticated receipts
    # were retained as published drafts. They remain useful historical evidence
    # after clearing editable memory. A newer explicit fact, even unknown,
    # always wins; prose summaries and ordinary reading never create a relation.
    historical = {}
    for draft in data.get('drafts', {}).values():
        if (draft.get('state') != 'published' or draft.get('account_id') != account_id
                or account_id is None or draft.get('action') not in ('like', 'unlike', 'follow', 'unfollow')):
            continue
        action, target = draft['action'], target_for(draft)
        stamp = _epoch(draft.get('published_at'))
        if type(target) is not int or target <= 0 or stamp is None:
            continue
        relationship_key = key(account_id, kind_for(action), target)
        if relationship_key in facts or stamp <= historical.get(relationship_key, (float('-inf'),))[0]:
            continue
        changed = None if _already_applied(draft) else draft['published_at']
        historical[relationship_key] = (stamp, {'account_id': account_id, 'kind': kind_for(action),
            'target_account_id': target if action in ('follow', 'unfollow') else None,
            'target_post_id': target if action in ('like', 'unlike') else None,
            'present': desired(action), 'observed_at': draft['published_at'],
            'last_changed_at': changed, 'source': 'retained confirmed action receipt',
            'observation_scope': 'historical state at published_at; execution rechecks the current relation'})
    facts.update({relationship_key: row for relationship_key, (_, row) in historical.items()})
    rows = list(facts.values())
    rows.sort(key=lambda row: row.get('observed_at', ''), reverse=True)
    return {'account_id': account_id, 'relations': rows[:200], 'cooldown_seconds': COOLDOWN_SECONDS,
            'meaning': 'Observed follow/like state only; no friendship, agreement or stance is inferred.'}


def context_for(data, agent, sources=None):
    result = snapshot_for(data, agent)
    if sources is not None:
        authors = {row['author_id'] for row in sources if isinstance(row, dict) and type(row.get('author_id')) is int}
        posts = {row['post_id'] for row in sources if isinstance(row, dict) and type(row.get('post_id')) is int}
        relevant, other = [], []
        for row in result['relations']:
            (relevant if row.get('target_account_id') in authors or row.get('target_post_id') in posts else other).append(row)
        result['relations'] = (relevant + other)[:60]
    return result
