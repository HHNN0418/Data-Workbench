"""Durable personal experiences, separate from the bounded prompt working set.

Only actual supplied reading excerpts, evidence-backed impressions and recorded
operation states are admitted. This module neither calls models nor fetches old
posts the role has never read. All writes belong to AgentStore's transaction.
"""
from __future__ import annotations

import hashlib
import json
import re

from . import agent_memory as memory
from .db import utc_now

KINDS = ('reading', 'outcome', 'viewpoints', 'questions', 'people')
SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_memory_records(
 id TEXT PRIMARY KEY,agent_id TEXT NOT NULL,reset_at TEXT NOT NULL,
 kind TEXT NOT NULL,record_key TEXT NOT NULL,at TEXT NOT NULL,
 payload_json TEXT NOT NULL,search_text TEXT NOT NULL,captured_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_memory_role_kind_time
 ON agent_memory_records(agent_id,reset_at,kind,at,id);
CREATE TABLE IF NOT EXISTS agent_memory_links(
 record_id TEXT NOT NULL REFERENCES agent_memory_records(id) ON DELETE CASCADE,
 agent_id TEXT NOT NULL,reset_at TEXT NOT NULL,source_id TEXT NOT NULL,
 post_id INTEGER,author_id INTEGER,PRIMARY KEY(record_id,source_id));
CREATE INDEX IF NOT EXISTS idx_memory_link_author ON agent_memory_links(agent_id,reset_at,author_id,record_id);
CREATE INDEX IF NOT EXISTS idx_memory_link_post ON agent_memory_links(agent_id,reset_at,post_id,record_id);
CREATE INDEX IF NOT EXISTS idx_memory_link_source ON agent_memory_links(agent_id,reset_at,source_id,record_id);
"""


def initialize(conn):
    conn.executescript(SCHEMA)


def _epoch(data, aid):
    return memory._memory(data, aid, create=False).get('reset_at') or ''


def _stamp(row):
    return memory._at(row.get('last_read_at') or row.get('at') or row.get('read_at'))


def _identity(kind, row):
    if kind == 'reading':
        return row.get('source_id', '') + '@' + row.get('version', '')
    if kind == 'outcome':
        return row.get('draft_id', '')
    return json.dumps([row.get('id'), row.get('at'), row.get('text')], ensure_ascii=False)


def _clean(kind, row):
    # Never archive raw decision narratives as recollections of events.
    fields = {
        'reading': ('source_id', 'version', 'kind', 'title', 'excerpt', 'excerpt_length',
                    'supplied_text_length', 'text_length', 'text_truncated', 'coverage',
                    'post_id', 'author_id', 'root_post_id', 'reply_to_post_id', 'post_kind',
                    'url', 'origin', 'source_role', 'source_created_at', 'source_observed_at',
                    'context_only', 'thread_context', 'first_read_at', 'last_read_at',
                    'read_count', 'notification_ids', 'epistemic_status'),
        'outcome': ('draft_id', 'action', 'state', 'origin', 'at', 'body', 'target_post_id',
                    'target_account_id', 'target_author_id', 'post_id', 'message', 'reason',
                    'source_ids', 'state_history', 'already_applied', 'remote_deleted_at',
                    'relationship_only', 'epistemic_status'),
    }.get(kind, ('id', 'at', 'text', 'draft_id', 'author_id', 'account_id', 'source_ids',
                 'evidence', 'previous_text', 'epistemic_status', 'is_action_intention'))
    return {key: row[key] for key in fields if key in row}


def _links(kind, row):
    if kind == 'reading':
        return [(row.get('source_id', ''), row.get('post_id'), row.get('author_id'))]
    if kind == 'outcome':
        return [('@target', row.get('target_post_id') or row.get('post_id'),
                 row.get('target_author_id') or row.get('target_account_id'))]
    return [(e.get('source_id', ''), e.get('post_id'), e.get('author_id'))
            for e in memory._rows(row.get('evidence'))]


def _store(conn, aid, epoch, kind, supplied):
    if kind not in KINDS or not isinstance(supplied, dict):
        return
    row = _clean(kind, supplied)
    stamp = _stamp(row)
    if not stamp or not memory._since_reset({'reset_at': epoch}, stamp):
        return
    if kind == 'reading' and (not row.get('source_id') or row.get('epistemic_status') != 'observed_quote'):
        return
    if kind in ('viewpoints', 'questions', 'people') and (
            not row.get('text') or not row.get('source_ids') or not memory._rows(row.get('evidence'))
            or row.get('epistemic_status') != 'subjective_impression'):
        return
    if kind == 'outcome' and not row.get('draft_id'):
        return
    key = _identity(kind, row)
    record_id = hashlib.sha256(json.dumps([aid, epoch, kind, key], ensure_ascii=False).encode()).hexdigest()
    prior = None
    if kind in ('reading', 'outcome'):
        prior = conn.execute('SELECT at,payload_json FROM agent_memory_records WHERE id=?', (record_id,)).fetchone()
    if prior and kind == 'outcome' and memory._epoch(prior['at']) > memory._epoch(stamp):
        # A delayed retry must not turn a previously verified delivery back
        # into its older pending/uncertain state.
        return
    if kind == 'reading':
        if prior:
            old = json.loads(prior['payload_json'])
            row['first_read_at'] = min((old.get('first_read_at') or stamp, row.get('first_read_at') or stamp),
                                       key=memory._epoch)
            row['last_read_at'] = max((old.get('last_read_at') or stamp, stamp), key=memory._epoch)
            extra = int(memory._epoch(stamp) > memory._epoch(old.get('last_read_at')))
            row['read_count'] = max(old.get('read_count', 1) + extra, row.get('read_count', 1))
            stamp = row['last_read_at']
    text = ' '.join(str(row.get(key) or '') for key in
                    ('title', 'excerpt', 'text', 'previous_text', 'body', 'reason', 'source_id',
                     'id', 'draft_id', 'author_id', 'account_id', 'target_author_id', 'target_account_id'))
    text += ' ' + ' '.join(str(value) for value in row.get('source_ids', []))
    text += ' ' + ' '.join(str(e.get(key) or '') for e in memory._rows(row.get('evidence'))
                           for key in ('source_id', 'title', 'excerpt'))
    conn.execute('INSERT INTO agent_memory_records(id,agent_id,reset_at,kind,record_key,at,payload_json,search_text,captured_at) '
                 'VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                 'at=excluded.at,payload_json=excluded.payload_json,search_text=excluded.search_text',
                 (record_id, aid, epoch, kind, key, stamp, json.dumps(row, ensure_ascii=False), text.casefold(), utc_now()))
    for source_id, post_id, author_id in _links(kind, row):
        conn.execute('INSERT INTO agent_memory_links(record_id,agent_id,reset_at,source_id,post_id,author_id) '
                     'VALUES(?,?,?,?,?,?) ON CONFLICT(record_id,source_id) DO UPDATE SET '
                     'post_id=excluded.post_id,author_id=excluded.author_id',
                     (record_id, aid, epoch, source_id, memory._id(post_id), memory._id(author_id)))


def capture(conn, data):
    """Bootstrap recoverable legacy memory once, then drain exact write events."""
    memories = data.get('memory', {})
    # Lazily loaded memory (D89): a role this write never touched was already
    # bootstrapped by an earlier write that did, so skip reading the rest.
    loaded = getattr(memories, 'loaded_items', None)
    for aid, own in (loaded() if loaded else memories.items()):
        if not isinstance(own, dict) or own.get('archive_version') == 1:
            continue
        epoch = _epoch(data, aid)
        episodes = own.get('episodes') or {}
        for kind, rows in [('reading', episodes.get('readings')), ('outcome', episodes.get('outcomes')),
                           *[(key, (own.get('subjective') or {}).get(key)) for key in KINDS[2:]]]:
            for row in memory._rows(rows):
                _store(conn, aid, epoch, kind, row)
        # Recover only retained, actual operation records. No fabricated reading
        # is reconstructed from the global collected-post database.
        legacy_scope = {**own, 'activity': []}
        for draft in data.get('drafts', {}).values():
            if draft.get('agent_id') == aid:
                for row in memory._legacy_outcomes({'drafts': {draft['id']: draft}}, aid, legacy_scope):
                    _store(conn, aid, epoch, 'outcome', row)
        for row in memory._legacy_outcomes({'drafts': {}}, aid, own):
            _store(conn, aid, epoch, 'outcome', row)
        if not episodes.get('readings') and own.get('last_sources'):
            temporary = {}
            rows = memory.record_read(temporary, aid, own['last_sources'], own.get('last_read_at'))
            for row in rows:
                _store(conn, aid, epoch, 'reading', row)
        own['archive_version'] = 1
    for item in data.pop('_memory_records', []):
        aid = item.get('agent_id')
        if aid and item.get('reset_at', '') == _epoch(data, aid):
            _store(conn, aid, _epoch(data, aid), item.get('kind'), item.get('row'))


def stats(conn, data, aid):
    counts = {key: 0 for key in KINDS}
    latest = None
    for row in conn.execute('SELECT kind,COUNT(*) AS n,MAX(at) AS latest FROM agent_memory_records '
                            'WHERE agent_id=? AND reset_at=? GROUP BY kind', (aid, _epoch(data, aid))):
        counts[row['kind']] = row['n']
        latest = max(latest or '', row['latest'] or '') or None
    return {'counts': counts, 'total': sum(counts.values()), 'latest_at': latest,
            'retention': 'persistent_since_upgrade'}


def _terms(q):
    return list(dict.fromkeys(word for word in re.findall(r"[\w\u3400-\u9fff]+", q.casefold())
                              if len(word) >= 2))[:8]


def _like(term):
    return '%' + term.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'


def _decode(row):
    result = json.loads(row['payload_json'])
    result['archive_id'] = row['id']
    return result


def recall(conn, data, agent, sources, now, limit=30):
    """Retrieve older related people, source versions and events, not just recency."""
    aid, epoch = agent['id'], _epoch(data, agent['id'])
    sources = memory._rows(sources)
    authors = {value for row in sources if (value := memory._id(row.get('author_id')))}
    targets = {value for row in sources for key in ('post_id', 'root_post_id', 'reply_to_post_id')
               if (value := memory._id(row.get(key)))}
    ids = {row['id'] for row in sources if isinstance(row.get('id'), str)}
    text = ' '.join(str(row.get('title', '')) + ' ' + str(row.get('text', row.get('excerpt', '')))[:1000]
                    for row in sources[:12])
    # Rank rarer words before ubiquitous glue words. Person/source links remain
    # indexed and do not depend on a name appearing in the quoted text.
    stop = {'the', 'and', 'that', 'this', 'with', 'from', 'have', 'for', 'are', 'was', 'but', 'you', 'your', 'not'}
    terms = [term for term in sorted(memory._tokens(text), key=lambda value: (-len(value), value))
             if term not in stop][:8]
    link_filters = []
    for column, values in (('author_id', authors), ('post_id', targets), ('source_id', ids)):
        if values:
            values = sorted(values, key=str)[:40]
            link_filters.append((
                'r.id IN (SELECT record_id FROM agent_memory_links WHERE agent_id=? AND reset_at=? '
                'AND ' + column + ' IN (' + ','.join('?' for _ in values) + '))',
                (aid, epoch, *values)))
    result = {}
    for kind in KINDS:
        base = 'SELECT r.* FROM agent_memory_records r WHERE r.agent_id=? AND r.reset_at=? AND r.kind=?'
        pool = {row['id']: row for row in conn.execute(base + ' ORDER BY r.at DESC,r.id LIMIT 30', (aid, epoch, kind))}
        # Retrieve each exact link independently. An OR with generic topic
        # words followed by a recency LIMIT can discard the oldest exact person
        # or source match before Python relevance ranking ever sees it.
        for condition, parameters in link_filters:
            pool.update({row['id']: row for row in conn.execute(
                base + ' AND (' + condition + ') ORDER BY r.at DESC,r.id LIMIT 120',
                (aid, epoch, kind, *parameters))})
        if terms:
            predicates = ["r.search_text LIKE ? ESCAPE '!'" for _ in terms]
            patterns = [_like(term) for term in terms]
            matches = ' + '.join('CASE WHEN ' + predicate + ' THEN 1 ELSE 0 END'
                                 for predicate in predicates)
            # A rare old event with several matching details must beat newer
            # rows that happen to share one common title or incidental word.
            text_query = base.replace('SELECT r.*', 'SELECT r.*,(' + matches + ') AS topic_matches', 1)
            pool.update({row['id']: row for row in conn.execute(
                text_query + ' AND (' + ' OR '.join(predicates) + ') '
                'ORDER BY topic_matches DESC,r.at DESC,r.id LIMIT 120',
                (*patterns, aid, epoch, kind, *patterns))})
        decoded = [_decode(row) for row in pool.values()]
        decoded.sort(key=lambda row: memory._rank(row, ids, authors, targets, memory._tokens(text), now), reverse=True)
        result[{'reading': 'readings', 'outcome': 'outcomes'}.get(kind, kind)] = decoded[:max(1, min(60, limit))]
    return result


def query(conn, data, agent_id, q='', kind='all', limit=20, offset=0):
    if kind not in (*KINDS, 'all') or not isinstance(q, str) or len(q) > 240:
        raise ValueError('Invalid memory history filter')
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
        raise ValueError('Invalid memory history page')
    where, parameters = 'agent_id=? AND reset_at=?', [agent_id, _epoch(data, agent_id)]
    if kind != 'all':
        where += ' AND kind=?'
        parameters.append(kind)
    # A user's search is literal text, including %/_; terms are joined with AND.
    for term in q.casefold().split()[:8]:
        where += " AND search_text LIKE ? ESCAPE '!'"
        parameters.append(_like(term))
    total = conn.execute('SELECT COUNT(*) FROM agent_memory_records WHERE ' + where, parameters).fetchone()[0]
    rows = conn.execute('SELECT * FROM agent_memory_records WHERE ' + where + ' ORDER BY at DESC,id LIMIT ? OFFSET ?',
                        (*parameters, limit, offset))
    items = []
    labels = {'reading': '读过的内容', 'outcome': '操作经历', 'viewpoints': '主观看法',
              'questions': '当时的疑问', 'people': '对人的主观印象'}
    for stored in rows:
        row = _decode(stored)
        items.append({'id': stored['id'], 'kind': stored['kind'], 'at': stored['at'],
                      'title': row.get('title') or labels[stored['kind']],
                      'excerpt': row.get('text') or row.get('excerpt') or row.get('body') or row.get('message') or row.get('reason') or '',
                      'epistemic_status': row.get('epistemic_status'), 'state': row.get('state'),
                      'action': row.get('action'), 'origin': row.get('origin'),
                      'author_id': row.get('author_id') or row.get('target_author_id') or row.get('target_account_id'),
                      'source_ids': row.get('source_ids') or ([row['source_id']] if row.get('source_id') else []),
                      'sources': [{'source_id': e.get('source_id'), 'title': e.get('title')}
                                  for e in memory._rows(row.get('evidence'))],
                      'post_id': row.get('post_id'), 'target_post_id': row.get('target_post_id'),
                      'coverage': row.get('coverage'), 'previous_text': row.get('previous_text')})
    return {**stats(conn, data, agent_id), 'items': items, 'total': total, 'query': q,
            'offset': offset, 'limit': limit, 'has_more': offset + len(items) < total}


def export_for(conn, aid):
    return [dict(row) for row in conn.execute('SELECT * FROM agent_memory_records WHERE agent_id=? ORDER BY at,id', (aid,))]


def forget(conn, aid):
    conn.execute('DELETE FROM agent_memory_links WHERE agent_id=?', (aid,))
    conn.execute('DELETE FROM agent_memory_records WHERE agent_id=?', (aid,))
