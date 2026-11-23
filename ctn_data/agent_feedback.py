"""Bounded local feedback linking actors, authored posts and sampled counters.

This module performs no network or model calls. Public counters describe the
observed audience; they cannot establish NPC identities or booked team points.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import threading
import time
from urllib.parse import urlencode

from .db import timestamp_key

METRICS = ('like_count', 'reply_count', 'repost_count', 'view_count')
POST_LIMIT = 20
STALE_SECONDS = 3600
RULES_SOURCE = 'https://capturethenarrative.com/info/competitions/2026/scoring/'
_ACTIONS = {'post', 'reply', 'repost', 'like', 'unlike', 'follow', 'unfollow', 'delete_post'}
_POST_ACTIONS = {'post', 'reply', 'repost'}
_POINTS_REASON = ('公开互动包含未知身份与重复互动，且缺少会话去重、浏览上限和观点变化资料；'
                  '不能据此估计角色实得 NPC 积分。')


def _id(value):
    return value if type(value) is int and value > 0 else None


def _epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    if isinstance(value, datetime):
        value = value.isoformat()
    key = timestamp_key(value)
    return datetime.fromisoformat(key.replace('Z', '+00:00')).timestamp() if key else None


def _at(value):
    epoch = _epoch(value)
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z') if epoch is not None else None


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _rows(data):
    drafts = data.get('drafts', {})
    return [row for row in drafts.values() if isinstance(row, dict)] if isinstance(drafts, dict) else []


def _drafts(data, agent):
    """A changed binding must never inherit a previous account's feedback."""
    account_id = _id(agent.get('account_id'))
    if account_id is None:
        return []
    selected = [row for row in _rows(data)
                if row.get('agent_id') == agent.get('id') and row.get('state') == 'published'
                and row.get('action') in _ACTIONS and row.get('account_id') == account_id]
    selected.sort(key=lambda row: (_epoch(row.get('published_at') or row.get('created_at')) or 0,
                                   str(row.get('id', ''))), reverse=True)
    return selected


def _links(account_id):
    if account_id is None:
        return {'account': None, 'posts': None, 'analysis': None, 'score': '/score'}
    return {'account': '/accounts?' + urlencode({'q': account_id}),
            'posts': '/posts?' + urlencode({'feed': 'all', 'author_id': account_id}),
            'analysis': '/analysis?' + urlencode({'author_id': account_id, 'analysis_tab': 'cohorts'}),
            'score': '/score'}


def _post_sql(where):
    # Both engines have an index on (post_id, ctn_timestamp(observed_at), id).
    # One small indexed lookup per selected post avoids ranking all snapshots.
    return '''SELECT p.post_id,p.author_id,p.kind,SUBSTR(COALESCE(p.body,''),1,500) AS body,
        p.created_at,p.last_seen,p.reply_to_post_id,p.repost_of_post_id,
        c.id AS counter_snapshot_id,c.observed_at AS sampled_at,c.raw_id AS counter_raw_id,
        c.like_count,c.reply_count,c.repost_count,c.view_count,
        c.score_inputs_updated_at,r.source AS counter_source,
        b.id AS previous_counter_snapshot_id,b.observed_at AS previous_sampled_at,
        b.like_count AS previous_like_count,b.reply_count AS previous_reply_count,
        b.repost_count AS previous_repost_count,b.view_count AS previous_view_count
        FROM posts p LEFT JOIN counter_snapshots c ON c.id=(
          SELECT id FROM counter_snapshots WHERE post_id=p.post_id
          AND ctn_timestamp(observed_at) IS NOT NULL
          ORDER BY ctn_timestamp(observed_at) DESC,id DESC LIMIT 1)
        LEFT JOIN raw_records r ON r.id=c.raw_id
        LEFT JOIN counter_snapshots b ON b.id=(
          SELECT id FROM counter_snapshots WHERE post_id=p.post_id
          AND ctn_timestamp(observed_at)<ctn_timestamp(c.observed_at)
          ORDER BY ctn_timestamp(observed_at) DESC,id DESC LIMIT 1) ''' + where


def _post(row, drafts, now):
    post_id = row['post_id']
    matched = next((draft for draft in drafts if draft.get('action') in _POST_ACTIONS
                    and draft.get('post_id') == post_id), None)
    counts = {metric: _count(row[metric]) for metric in METRICS}
    previous = {metric: _count(row['previous_' + metric]) for metric in METRICS}
    sampled = _epoch(row['sampled_at'])
    before = _epoch(row['previous_sampled_at'])
    interactions = [counts[metric] for metric in METRICS[:3]]
    return {'post_id': post_id, 'author_id': row['author_id'],
            'action': matched.get('action') if matched else None,
            'draft_id': matched.get('id') if matched else None,
            'origin': matched.get('origin') if matched else None,
            'remote_deleted_at': matched.get('remote_deleted_at') if matched else None,
            'kind': row['kind'], 'body': row['body'], 'created_at': row['created_at'],
            'latest_counts': counts, 'sampled_at': _at(row['sampled_at']),
            'previous_sampled_at': _at(row['previous_sampled_at']), 'previous_counts': previous,
            'counter_delta': {metric: counts[metric] - previous[metric]
                              if before is not None and sampled is not None and before < sampled
                              and counts[metric] is not None and previous[metric] is not None else None
                              for metric in METRICS},
            'delta_elapsed_seconds': round(sampled - before, 1)
                                     if sampled is not None and before is not None and before < sampled else None,
            'stale': None if sampled is None else now - sampled > STALE_SECONDS,
            'sample_age_seconds': None if sampled is None else round(max(0, now - sampled), 1),
            'interaction_count': sum(interactions) if all(value is not None for value in interactions) else None,
            'provenance': {'source': row['counter_source'] or ('counter_snapshots' if sampled is not None else None),
                           'counter_snapshot_id': row['counter_snapshot_id'],
                           'previous_counter_snapshot_id': row['previous_counter_snapshot_id'],
                           'raw_id': row['counter_raw_id'],
                           'score_inputs_updated_at': row['score_inputs_updated_at']},
            'links': {'post': '/posts?' + urlencode({'post': post_id}),
                      'analysis': '/analysis?' + urlencode({'author_id': row['author_id'],
                                                           'focus_post_id': post_id, 'analysis_tab': 'growth'})}}


def _refresh_age(result, now):
    result = copy.deepcopy(result)
    for row in result['recent_posts'] + [action['performance'] for action in result['actions'] if action.get('performance')]:
        sampled = _epoch(row.get('sampled_at'))
        row['stale'] = None if sampled is None else now - sampled > STALE_SECONDS
        row['sample_age_seconds'] = None if sampled is None else round(max(0, now - sampled), 1)
    return result


class FeedbackCache:
    """Per-controller cache; account and outbox changes invalidate immediately.

    Keep one instance per database/controller. This intentionally has no global
    cache that might leak one workspace's observations into another workspace.
    """
    def __init__(self, ttl_seconds=30):
        self.ttl_seconds = max(0, min(300, float(ttl_seconds)))
        self._lock = threading.Lock()
        self._entries = {}

    def get(self, key, now):
        with self._lock:
            found = self._entries.get(key)
            if found and 0 <= now - found[0] < self.ttl_seconds:
                return _refresh_age(found[1], now)
        return None

    def put(self, key, result, now):
        with self._lock:
            self._entries = {entry: value for entry, value in self._entries.items()
                             if 0 <= now - value[0] < self.ttl_seconds}
            if len(self._entries) >= 100:
                self._entries.pop(next(iter(self._entries)))
            self._entries[key] = (now, copy.deepcopy(result))


def snapshot_for(conn, data, agent, now=None, *, cache=None):
    """Read this bound account's posts and the exact retained action ledger.

    Stats cover the latest 20 *authored* posts; a liked target is contextual
    feedback only. Missing values remain null, including partial-window totals.
    """
    clock = _epoch(now) if now is not None else time.time()
    clock = time.time() if clock is None else clock
    account_id = _id(agent.get('account_id'))
    drafts = _drafts(data, agent)
    signature = hashlib.sha256(json.dumps([(row.get('id'), row.get('action'), row.get('post_id'),
                                           row.get('target_post_id', row.get('reply_to')), row.get('target_account_id'),
                                           row.get('published_at'), row.get('remote_deleted_at'), bool(row.get('relationship_already_applied') or
                                               (row.get('result') or {}).get('already_applied'))) for row in drafts],
                                          ensure_ascii=False, default=str).encode()).hexdigest()
    deletions = {row.get('post_id'): row for row in data.get('rate', {}).get('remote_deleted_posts', {}).values()
                 if isinstance(row, dict) and row.get('account_id') == account_id and _id(row.get('post_id'))}
    deletion_signature = hashlib.sha256(json.dumps(deletions, sort_keys=True, default=str).encode()).hexdigest()
    key = (agent.get('id'), account_id, signature, deletion_signature)
    if cache is not None:
        result = cache.get(key, clock)
        if result is not None:
            return result
    result = {'account_id': account_id, 'status': 'unbound' if account_id is None else 'awaiting_posts',
              'generated_at': _at(clock), 'known_post_count': None,
              'confirmed_action_count': sum(not bool(row.get('relationship_already_applied') or
                  (row.get('result') or {}).get('already_applied')) for row in drafts),
              'verified_record_count': len(drafts),
              'action_counts': {action: sum(row['action'] == action and not bool(row.get('relationship_already_applied') or
                  (row.get('result') or {}).get('already_applied')) for row in drafts) for action in sorted(_ACTIONS)},
              'action_history_scope': 'retained_local_outbox',
              'window': {'limit': POST_LIMIT, 'post_count': 0, 'sampled_posts': 0},
              'metrics': {metric: None for metric in METRICS},
              'observed_metric_subtotals': {metric: None for metric in METRICS},
              'metric_coverage': {metric: 0 for metric in METRICS},
              'latest_sampled_at': None, 'recent_posts': [], 'actions': [],
              'score_estimate': {'status': 'unknown', 'is_official': False, 'points_decimal': None,
                                 'reason': _POINTS_REASON, 'source': RULES_SOURCE},
              'official': {'status': 'unattributed', 'points_decimal': None, 'events': [],
                           'team_snapshot': None,
                           'reason': '现有官方流水没有可核实的结构化账号/帖子归属；全队总分及其变化不能分摊到角色或帖子。'},
              'links': _links(account_id),
              'notes': ['互动统计仅含该账号最近20篇已采集本人帖；缺失字段为未知，覆盖不足时不声称完整总量。',
                        '完成动作数只覆盖仍保留的本地已核实操作记录，不是平台终身行为总量。',
                        '已核实删除的帖子保留历史资料并标记删除时间，不纳入当前互动汇总；历史采样计数不自动改写。',
                        '点赞对象的互动是对象背景，不能当作本角色发帖成效；互动变化也不能证明操作因果。',
                        '公开互动计数不是官方NPC得分；全队快照只作背景，不属于个人所得。']}
    if account_id is not None:
        result['known_post_count'] = conn.execute('SELECT COUNT(*) FROM posts WHERE author_id=?', (account_id,)).fetchone()[0]
        own = [dict(row) for row in conn.execute(_post_sql('WHERE p.author_id=? ORDER BY ctn_timestamp(p.created_at) DESC NULLS LAST,p.post_id DESC LIMIT ?'),
                                                (account_id, POST_LIMIT))]
        posts = {row['post_id']: _post(row, drafts, clock) for row in own}
        result['recent_posts'] = list(posts.values())
        if own:
            result['status'] = 'observed'
        own_ids = set(posts)
        additional = {_id(row.get('post_id')) if row['action'] in _POST_ACTIONS else
                      _id(row.get('target_post_id', row.get('reply_to'))) or _id(row.get('post_id'))
                      for row in drafts[:POST_LIMIT] if row['action'] not in ('follow', 'unfollow', 'delete_post')}
        additional = sorted(additional - own_ids - {None})
        if additional:
            placeholders = ','.join('?' for _ in additional)
            for row in conn.execute(_post_sql(f'WHERE p.post_id IN ({placeholders})'), additional):
                posts[row['post_id']] = _post(dict(row), drafts, clock)
        for post_id, post in posts.items():
            if post_id in deletions and post['author_id'] == account_id:
                post['remote_deleted_at'] = deletions[post_id].get('confirmed_at')
        for draft in drafts[:POST_LIMIT]:
            target = _id(draft.get('target_post_id', draft.get('reply_to')))
            post_id = _id(draft.get('post_id'))
            performance_id = (target or post_id) if draft['action'] in ('like', 'unlike') else post_id
            if draft['action'] in ('follow', 'unfollow'):
                performance_id = None
            performance = posts.get(performance_id)
            scope = ('deletion' if draft['action'] == 'delete_post' else
                     'relationship' if draft['action'] in ('follow', 'unfollow') else
                     'target_context' if draft['action'] in ('like', 'unlike') else
                     'own_post' if performance and performance['author_id'] == account_id else 'unknown')
            # A mismatched author is not proof that this account wrote the post.
            if scope in ('unknown', 'deletion'):
                performance = None
            result['actions'].append({'draft_id': draft.get('id'), 'action': draft['action'],
                'status': 'published', 'post_id': post_id, 'target_post_id': target,
                'target_account_id': _id(draft.get('target_account_id')),
                'origin': draft.get('origin'),
                'remote_deleted_at': draft.get('remote_deleted_at') or
                    (deletions.get(target, {}).get('confirmed_at') if draft['action'] == 'delete_post' else None),
                'already_applied': bool(draft.get('relationship_already_applied') or
                    (draft.get('result') or {}).get('already_applied')),
                'published_at': _at(draft.get('published_at')),
                'performance_post_id': performance_id, 'performance_scope': scope, 'performance': performance})
        window = [row for row in result['recent_posts'] if not row.get('remote_deleted_at')]
        result['window'].update(post_count=len(window), sampled_posts=sum(row['sampled_at'] is not None for row in window))
        for metric in METRICS:
            observed = [row['latest_counts'][metric] for row in window if row['latest_counts'][metric] is not None]
            result['metric_coverage'][metric] = len(observed)
            result['observed_metric_subtotals'][metric] = sum(observed) if observed else None
            result['metrics'][metric] = sum(observed) if window and len(observed) == len(window) else None
        result['latest_sampled_at'] = max((row['sampled_at'] for row in window if row['sampled_at']), default=None)
    # Match the score page's observation-time ordering, including historical
    # imports. This small ledger lookup never reconstructs account analytics or
    # scans official activity; per-controller caching bounds repeat polling.
    snapshot = conn.execute('''SELECT id,observed_at,total_decimal,source FROM score_snapshots
        ORDER BY ctn_timestamp(observed_at) DESC NULLS LAST,id DESC LIMIT 1''').fetchone()
    if snapshot:
        result['official']['team_snapshot'] = {**dict(snapshot), 'basis': 'latest_observed_snapshot',
                                              'attribution': 'team_only', 'link': '/score'}
    if cache is not None:
        cache.put(key, result, clock)
    return result


def context_for(conn, data, agent, now=None, *, cache=None):
    """Small factual feedback for the actor's next decision; no added model call."""
    feedback = snapshot_for(conn, data, agent, now, cache=cache)
    posts = feedback['recent_posts'][:6]
    return {'interpretation': ('Only the exact bound account and confirmed local actions are linked. '
                'Counters are sampled public observations, not NPC identities, causal effects, official points, '
                'or proof of another person\'s beliefs. Missing counts are unknown, not zero. '
                'Manual-origin actions were arranged by the operator. A remote_deleted_at timestamp marks '
                'a confirmed deletion; its historical text and counters are not proof the post is still visible. '
                'Use these observations to improve topics, expression and interactions and compete for more '
                'verifiable points and resource opportunities within competition rules. Avoid ineffective '
                'repetition; respond to relevant conversation and new evidence.'),
            'account_id': feedback['account_id'], 'status': feedback['status'],
            'generated_at': feedback['generated_at'], 'known_post_count': feedback['known_post_count'],
            'window': feedback['window'], 'metric_coverage': feedback['metric_coverage'],
            'recent_authored_posts': [{key: row.get(key) for key in
                ('post_id', 'action', 'origin', 'remote_deleted_at', 'created_at', 'latest_counts', 'sampled_at', 'stale', 'provenance',
                 'previous_sampled_at', 'counter_delta', 'delta_elapsed_seconds')}
                | {'body': row['body'][:180]} for row in posts],
            'score_estimate': feedback['score_estimate'],
            'team_official_snapshot': feedback['official']['team_snapshot'],
            'official_points': None, 'official_attribution': feedback['official']['reason']}
