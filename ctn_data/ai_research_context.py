"""Structured observations for AI; arithmetic stays outside the language model."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import math

from .db import connect, timestamp_key, utc_now

MAX_TIMELINE_BUCKETS = 48
MAX_DETAIL_POSTS = 40


def _where(scope):
    clauses = ["(p.visibility IS NULL OR LOWER(p.visibility) IN ('','public'))",
               "(a.private IS NULL OR a.private=0)"]
    args = []
    if scope.get('own') == 'own':
        clauses.append('t.is_own=1')
    elif scope.get('own') == 'unknown':
        clauses.append('t.team_label IS NULL')
    if scope.get('author_id') is not None:
        clauses.append('p.author_id=?')
        args.append(scope['author_id'])
    for key, operator in [('since', '>='), ('until', '<=')]:
        if scope.get(key):
            clauses.append(f'ctn_timestamp(p.created_at){operator}?')
            args.append(scope[key])
    return ' WHERE ' + ' AND '.join(clauses), args


def _valid_count(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _pair(rows):
    if len(rows) < 2:
        return {'status': 'insufficient_snapshots', 'snapshots_available': len(rows)}
    previous, latest = rows[1], rows[0]
    start, end = timestamp_key(previous['observed_at']), timestamp_key(latest['observed_at'])
    minutes = (datetime.fromisoformat(end.replace('Z', '+00:00')) -
               datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds() / 60
    deltas = {}
    for key in ('like_count', 'reply_count', 'repost_count', 'view_count'):
        before, after = previous[key], latest[key]
        deltas[key] = after - before if _valid_count(before) and _valid_count(after) else None
    complete = all(deltas[key] is not None for key in ('like_count', 'reply_count', 'repost_count'))
    interaction_delta = sum(deltas[key] for key in ('like_count', 'reply_count', 'repost_count')) if complete else None
    decreased = any(value is not None and value < 0 for value in deltas.values())
    return {'status': 'counter_decreased' if decreased else 'observed_pair' if minutes > 0 else 'same_timestamp',
            'from': start, 'until': end, 'elapsed_minutes': round(minutes, 3), 'deltas': deltas,
            'interaction_delta': interaction_delta,
            'interactions_per_minute': round(interaction_delta / minutes, 4)
            if interaction_delta is not None and minutes > 0 and not decreased else None}


def build_research_context(db_path, scope, sources):
    """Read corpus aggregates and a bounded selection of observed relationships.

    This is a separate database snapshot from source selection. Interval pairs
    describe actual observed times, never an invented 30/60/180 minute baseline.
    """
    where, args = _where(scope)
    joins = (' FROM posts p LEFT JOIN accounts a ON a.account_id=p.author_id '
             'LEFT JOIN team_accounts t ON t.account_id=p.author_id')
    all_ids = list(dict.fromkeys(source['post_id'] for source in sources if source.get('kind') == 'post' and source.get('post_id')))
    # Interleave groups to keep popular and comparison posts in detailed evidence.
    groups = [[source['post_id'] for source in sources if source.get('kind') == 'post' and source.get('group') == group]
              for group in ('popular', 'comparison', 'retrieved')]
    balanced = []
    for index in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if index < len(group) and group[index] not in balanced:
                balanced.append(group[index])
    detail_ids = (balanced or all_ids)[:MAX_DETAIL_POSTS]
    with closing(connect(db_path)) as conn:
        conn.execute('BEGIN')
        totals = conn.execute('''SELECT COUNT(*) AS posts,COUNT(DISTINCT p.author_id) AS authors,
            SUM(CASE WHEN TRIM(COALESCE(p.body,''))<>'' THEN 1 ELSE 0 END) AS posts_with_text,
            SUM(CASE WHEN p.reply_to_post_id IS NOT NULL THEN 1 ELSE 0 END) AS replies,
            SUM(CASE WHEN p.repost_of_post_id IS NOT NULL THEN 1 ELSE 0 END) AS reposts,
            MIN(ctn_timestamp(p.created_at)) AS first_published_at,
            MAX(ctn_timestamp(p.created_at)) AS last_published_at''' + joins + where, args).fetchone()
        corpus = {key: int(totals[key] or 0) for key in ('posts', 'authors', 'posts_with_text', 'replies', 'reposts')}
        corpus.update({key: totals[key] for key in ('first_published_at', 'last_published_at')})
        kinds = conn.execute("SELECT COALESCE(p.kind,'unknown') AS kind,COUNT(*) AS posts" + joins + where +
                             " GROUP BY COALESCE(p.kind,'unknown') ORDER BY posts DESC LIMIT 20", args).fetchall()
        timeline = conn.execute("SELECT SUBSTR(ctn_timestamp(p.created_at),1,13) AS hour_utc,COUNT(*) AS posts" +
                                joins + where + ' AND ctn_timestamp(p.created_at) IS NOT NULL GROUP BY '
                                'SUBSTR(ctn_timestamp(p.created_at),1,13) ORDER BY hour_utc DESC LIMIT ?',
                                args + [MAX_TIMELINE_BUCKETS + 1]).fetchall()
        histories, relations, reply_counts = {}, [], {}
        if detail_ids:
            marks = ','.join('?' for _ in detail_ids)
            pairs = conn.execute(f'''SELECT post_id,observed_at,like_count,reply_count,repost_count,view_count FROM (
                SELECT post_id,observed_at,like_count,reply_count,repost_count,view_count,
                ROW_NUMBER() OVER(PARTITION BY post_id ORDER BY ctn_timestamp(observed_at) DESC,id DESC) AS rn
                FROM counter_snapshots WHERE post_id IN ({marks}) AND ctn_timestamp(observed_at) IS NOT NULL
                ) observations WHERE rn<=2 ORDER BY post_id,rn''', detail_ids).fetchall()
            for row in pairs:
                histories.setdefault(row['post_id'], []).append(row)
            relations = conn.execute(f'''SELECT post_id,reply_to_post_id,root_post_id,repost_of_post_id
                FROM posts WHERE post_id IN ({marks}) ORDER BY post_id''', detail_ids).fetchall()
            replies = conn.execute('SELECT p.reply_to_post_id,COUNT(*) AS replies' + joins + where +
                                   f' AND p.reply_to_post_id IN ({marks}) GROUP BY p.reply_to_post_id',
                                   args + detail_ids).fetchall()
            reply_counts = {row['reply_to_post_id']: int(row['replies']) for row in replies}
    details = [{'source_id': f'post:{post_id}', 'post_id': post_id, **_pair(histories.get(post_id, []))} for post_id in detail_ids]
    return {'captured_at': utc_now(), 'corpus': corpus,
            'post_kinds': [{'kind': row['kind'], 'posts': int(row['posts'])} for row in kinds],
            'publication_timeline': [dict(row) for row in reversed(timeline[:MAX_TIMELINE_BUCKETS])],
            'timeline_timezone': 'UTC', 'timeline_truncated': len(timeline) > MAX_TIMELINE_BUCKETS,
            'snapshot_intervals': details,
            'relations': [{**dict(row), 'known_direct_replies_in_scope': reply_counts.get(row['post_id'], 0)} for row in relations],
            'detail_posts': len(detail_ids), 'selected_posts': len(all_ids),
            'method': 'SQL aggregates cover the full post filter; detail pairs and relationships cover up to 40 selected posts, balanced across sample groups. Separate read snapshot.',
            'limits': ['发帖量时间线来自已采集帖子，不代表全站曝光或观点改变。',
                       '相邻快照按各帖实际观测时间计算，间隔不同，不能直接当作同龄增长比较或因果效应。',
                       '关系仅为已记录的回复/转发链接；缺失节点与未采集回复未知，不能推断机器人身份。',
                       '新闻叙事日期与帖子现实发布时间分开解释；公开互动不能换算官方积分。']}
