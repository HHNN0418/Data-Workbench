"""Annotation progress and what it found (GET /api/processing/annotations).

annotations.AnnotationWorker labels every collected post with a stance,
concerns and thread depth (rules in agent_targeting) and marks bot accounts.
This view shows how far it has got (annotated posts, backlog against the
current classifier version, last pass) and the distributions over the last
WINDOW_HOURS, for all posts and for citizens only (our accounts and accounts
marked as bots left out), since targeting acts on the citizen view.

The distributions are grouped in SQL, so the transfer stays small on ~70k
posts a day. Read-only.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import agent_targeting
from .annotations import classifier_version
from .db import connect, utc_now
from .imports import normalize_timestamp
from .results_view import _parse
from .stance_labels import CLASSIFIER as MODEL_LABEL

WINDOW_HOURS = 24
STANCES = ('yes', 'no', 'undecided', 'unrelated')
DEPTH_BUCKETS = ('0', '1', '2', '3', '4–6', '7+', 'unknown')
NOTES = ['立场和顾虑是关键词规则（agent_targeting），只用来排序阅读和选目标，不是人工判断',
         '「只看市民」去掉了我方账号和被标为机器人的账号',
         f'楼层 0 是原帖；对方帖子在第 {agent_targeting.MAX_REPLY_DEPTH} 层或更深时，Agent 不再回复',
         '积压是还没有标注、或者用旧版规则标注的帖子；规则一改，旧标注会全部重做',
         '有模型立场标签的帖子（立场来源为模型时）算作已按当前规则标注，不计入积压']


def _depth_bucket(depth):
    if depth is None:
        return 'unknown'
    if depth < 4:
        return str(depth)
    return '4–6' if depth < agent_targeting.MAX_REPLY_DEPTH else '7+'


def _concerns(text):
    try:
        values = json.loads(text or '[]')
    except (TypeError, ValueError):
        return []
    return [value for value in values if isinstance(value, str)] if isinstance(values, list) else []


def build(db_path, *, worker=None, now=None, hours=WINDOW_HOURS):
    now = _parse(now) if now else datetime.now(timezone.utc)
    since = normalize_timestamp(now - timedelta(hours=hours))
    version = classifier_version()
    conn = connect(db_path)
    try:
        posts_total = conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
        # Rows carrying a model label (D84) are current, not outdated keyword rows, as in pipeline._process.
        annotated, current, latest = conn.execute(
            'SELECT COUNT(*),SUM(CASE WHEN classifier IN (?,?) THEN 1 ELSE 0 END),MAX(updated_at) FROM post_annotations',
            (version, MODEL_LABEL)).fetchone()
        backlog = conn.execute('''SELECT COUNT(*) FROM posts p LEFT JOIN post_annotations a ON a.post_id=p.post_id
            WHERE a.post_id IS NULL OR a.classifier NOT IN (?,?)''', (version, MODEL_LABEL)).fetchone()[0]
        bots = conn.execute('SELECT COUNT(*) FROM account_annotations WHERE is_bot=1').fetchone()[0]
        grouped = conn.execute(
            '''SELECT pa.stance,pa.concerns_json,pa.thread_depth,COALESCE(t.is_own,0) AS own,
                      COALESCE(aa.is_bot,0) AS bot,COUNT(*) AS n
               FROM post_annotations pa LEFT JOIN team_accounts t ON t.account_id=pa.author_id
               LEFT JOIN account_annotations aa ON aa.account_id=pa.author_id
               WHERE ctn_timestamp(pa.created_at)>=? AND ctn_timestamp(pa.created_at)<=?
               GROUP BY pa.stance,pa.concerns_json,pa.thread_depth,COALESCE(t.is_own,0),COALESCE(aa.is_bot,0)''',
            (since, normalize_timestamp(now))).fetchall()
        posts_window = conn.execute('SELECT COUNT(*) FROM posts WHERE ctn_timestamp(created_at)>=? '
                                    'AND ctn_timestamp(created_at)<=?', (since, normalize_timestamp(now))).fetchone()[0]
    finally:
        conn.close()
    stance_all, stance_citizen = Counter(), Counter()
    depth_all, depth_citizen = Counter(), Counter()
    concerns = {}
    with_concern = {'all': 0, 'citizen': 0}
    total = {'all': 0, 'citizen': 0, 'own': 0, 'bot': 0}
    for row in grouped:
        # PostgreSQL returns COUNT as bigint and COALESCE of integers as int; keep plain ints.
        n, citizen = int(row['n']), not int(row['own']) and not int(row['bot'])
        stance, depth = row['stance'], _depth_bucket(row['thread_depth'])
        found = _concerns(row['concerns_json'])
        total['all'] += n
        total['own'] += n if int(row['own']) else 0
        total['bot'] += n if int(row['bot']) and not int(row['own']) else 0
        stance_all[stance] += n
        depth_all[depth] += n
        if found:
            with_concern['all'] += n
        if citizen:
            total['citizen'] += n
            stance_citizen[stance] += n
            depth_citizen[depth] += n
            if found:
                with_concern['citizen'] += n
        for concern in found:
            entry = concerns.setdefault(concern, {'concern': concern, 'all': 0, 'citizen': 0,
                                                  **{f'citizen_{key}': 0 for key in STANCES}})
            entry['all'] += n
            if citizen:
                entry['citizen'] += n
                entry[f'citizen_{stance}'] = entry.get(f'citizen_{stance}', 0) + n

    def distribution(counts, keys, size):
        return [{'key': key, 'count': counts.get(key, 0),
                 'share': round(counts.get(key, 0) / size, 4) if size else None} for key in keys]
    extra_stances = sorted(set(stance_all) - set(STANCES))
    last = getattr(worker, 'last_result', None) if worker is not None else None
    return {'generated_at': utc_now(), 'window': {'hours': hours, 'from': since, 'to': normalize_timestamp(now)},
            'progress': {'posts_total': posts_total, 'annotated': annotated, 'current_classifier': int(current or 0),
                         'backlog': backlog, 'classifier': version, 'last_annotated_at': latest,
                         'coverage': round(int(current or 0) / posts_total, 4) if posts_total else None,
                         'bot_accounts': bots, 'last_pass': last},
            'window_totals': {**total, 'posts': posts_window, 'with_concern': with_concern},
            'stance': {'all': distribution(stance_all, (*STANCES, *extra_stances), total['all']),
                       'citizen': distribution(stance_citizen, (*STANCES, *extra_stances), total['citizen'])},
            'depth': {'all': distribution(depth_all, DEPTH_BUCKETS, total['all']),
                      'citizen': distribution(depth_citizen, DEPTH_BUCKETS, total['citizen'])},
            'concerns': sorted(concerns.values(), key=lambda entry: (-entry['citizen'], -entry['all'], entry['concern'])),
            'max_reply_depth': agent_targeting.MAX_REPLY_DEPTH,
            'notes': NOTES, 'read_only': True}
