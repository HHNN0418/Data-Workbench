"""Competition insights over a recent window (GET /api/insights).

One read of the window's posts with their stored annotations and latest
counters, aggregated into four views: opinion (stance mix by hour), own
performance against citizens, posting time and content features. Engagement
is weighted like Red points (like 5, repost 10, reply 15; views left out) but
counts every interacting account, so it is a proxy, not official score.

The read takes several seconds on live data, so results are cached and
refreshed in the background while the previous result is served.
"""
from __future__ import annotations

import json
import statistics
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import connect, utc_now
from .imports import normalize_timestamp

CACHE_SECONDS = 600
DEFAULT_HOURS = 72
MIN_HOURS, MAX_HOURS = 6, 336
# Engagement keeps arriving for a while; younger posts would look weaker.
MATURE_HOURS = 2
STANCES = ('yes', 'no', 'undecided', 'unrelated')
GROUPS = ('citizens', 'own', 'bots', 'news')
LENGTH_LABELS = ('<80', '80–159', '160–280', '>280')
TOP_OWN_POSTS = 5
SYDNEY = ZoneInfo('Australia/Sydney')

# Points, author group and the normalised timestamp are computed in SQL so the
# Python pass over ~150k rows stays short. Bots are account_annotations.is_bot,
# the rule shared with agent_targeting.bot_accounts.
WINDOW_SQL = '''SELECT p.post_id,p.author_id,p.kind,ctn_timestamp(p.created_at) AS created,
    CASE WHEN length(p.body)<80 THEN 0 WHEN length(p.body)<160 THEN 1 WHEN length(p.body)<=280 THEN 2 ELSE 3 END AS length_bucket,
    CASE WHEN p.embed_json IS NOT NULL AND p.embed_json NOT IN ('','null') THEN 1
         WHEN p.body LIKE '%http%' THEN 1 ELSE 0 END AS has_link,
    pa.stance,pa.concerns_json,
    CASE WHEN t.is_own=1 THEN 'own'
         WHEN aa.is_bot=1 THEN 'bots'
         WHEN LOWER(a.username) IN ('dailykingstonia','kingstoniaherald') THEN 'news' ELSE 'citizens' END AS grp,
    CASE WHEN c.id IS NULL THEN NULL
         ELSE 5*COALESCE(c.like_count,0)+10*COALESCE(c.repost_count,0)+15*COALESCE(c.reply_count,0) END AS points,
    LOWER(a.username) AS username
  FROM posts p
  LEFT JOIN post_annotations pa ON pa.post_id=p.post_id
  LEFT JOIN account_annotations aa ON aa.account_id=p.author_id
  LEFT JOIN team_accounts t ON t.account_id=p.author_id
  LEFT JOIN accounts a ON a.account_id=p.author_id
  LEFT JOIN counter_sampling s ON s.post_id=p.post_id
  LEFT JOIN counter_snapshots c ON c.post_id=s.post_id AND ctn_timestamp(c.observed_at)=s.sampled_at
  WHERE ctn_timestamp(p.created_at)>=? AND ctn_timestamp(p.created_at)<=?'''


def _summary(values, top_cut=None):
    if not values:
        return {'posts': 0, 'median': None, 'mean': None, 'p90': None, 'engaged_share': None, 'top_decile_share': None}
    ordered = sorted(values)
    return {'posts': len(values), 'median': statistics.median(ordered), 'mean': round(statistics.fmean(ordered), 1),
            'p90': ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))],
            'engaged_share': round(sum(value > 0 for value in ordered) / len(ordered), 3),
            'top_decile_share': None if top_cut is None else round(sum(value >= top_cut for value in ordered) / len(ordered), 3)}


def _share(counts):
    sided = counts['yes'] + counts['no']
    total = sum(counts[stance] for stance in STANCES)
    return {'yes_of_sided': round(counts['yes'] / sided, 3) if sided else None,
            'undecided_of_all': round(counts['undecided'] / total, 3) if total else None, 'posts': total}


def build(db_path, *, hours=DEFAULT_HOURS, now=None):
    now = (datetime.fromisoformat(normalize_timestamp(now).replace('Z', '+00:00')) if now
           else datetime.now(timezone.utc))
    since = now - timedelta(hours=hours)
    mature_key = normalize_timestamp(now - timedelta(hours=MATURE_HOURS))
    # The window never spans a Sydney DST change during the campaign, so one
    # offset turns the normalised UTC text into local hours without parsing.
    offset = int(now.astimezone(SYDNEY).utcoffset().total_seconds() // 3600)
    conn = connect(db_path)
    try:
        rows = conn.execute(WINDOW_SQL, (normalize_timestamp(since), normalize_timestamp(now))).fetchall()
    finally:
        conn.close()

    opinion = defaultdict(lambda: {group: Counter() for group in GROUPS})
    by_group, by_hour = defaultdict(list), defaultdict(lambda: defaultdict(list))
    own_accounts, own_kinds, own_posts = defaultdict(list), defaultdict(list), []
    features, author_points = defaultdict(lambda: defaultdict(list)), defaultdict(int)
    unannotated = 0
    for row in rows:
        created = row['created']
        if not created:
            continue
        group, value, stance = row['grp'], row['points'], row['stance']
        if stance is None:
            unannotated += 1
        else:
            opinion[created[:13]][group][stance] += 1
        if value is None:
            continue
        if row['author_id'] is not None:
            author_points[row['author_id']] += value
        if created > mature_key:
            continue
        by_group[group].append(value)
        by_hour[(int(created[11:13]) + offset) % 24][group].append(value)
        if group == 'own':
            own_accounts[(row['author_id'], row['username'])].append(value)
            own_kinds[row['kind'] or 'post'].append(value)
            own_posts.append((value, row['post_id']))
        if group in ('citizens', 'own'):
            for name, key in (('length', LENGTH_LABELS[row['length_bucket']]), ('link', bool(row['has_link'])),
                              ('stance', stance or 'unannotated'), ('kind', row['kind'] or 'post')):
                features[name][(group, key)].append(value)
            if row['concerns_json'] and row['concerns_json'] != '[]':
                for concern in json.loads(row['concerns_json']):
                    features['concern'][(group, concern)].append(value)

    citizens = sorted(by_group['citizens'])
    top_cut = citizens[int(len(citizens) * 0.9)] if citizens else None
    timeseries = []
    for bucket in sorted(opinion):
        # Hourly buckets are UTC hours; whole-hour offsets make them Sydney hours too.
        entry = {'bucket_at': bucket + ':00:00Z', 'timezone': 'Australia/Sydney'}
        for group in GROUPS:
            counts = opinion[bucket][group]
            entry[group] = {stance: counts[stance] for stance in STANCES}
        timeseries.append(entry)

    def recent(group, start, end):
        counts = Counter()
        low, high = normalize_timestamp(start)[:13], normalize_timestamp(end)[:13]
        for bucket, groups in opinion.items():
            if low <= bucket < high:
                counts.update(groups[group])
        return _share(counts)

    top = sorted(own_posts, reverse=True)[:TOP_OWN_POSTS]
    bodies = {}
    if top:
        conn = connect(db_path)
        try:
            ids = [post_id for _, post_id in top]
            bodies = {row['post_id']: row['body'] for row in conn.execute(
                'SELECT post_id,body FROM posts WHERE post_id IN (' + ','.join('?' for _ in ids) + ')', ids)}
        finally:
            conn.close()
    return {
        'generated_at': utc_now(),
        'window': {'hours': hours, 'from': normalize_timestamp(since), 'to': normalize_timestamp(now),
                   'mature_hours': MATURE_HOURS},
        'opinion': {'timeseries': timeseries,
                    'last_6h': {group: recent(group, now - timedelta(hours=6), now + timedelta(hours=1)) for group in GROUPS},
                    'previous_6h': {group: recent(group, now - timedelta(hours=12), now - timedelta(hours=6)) for group in GROUPS},
                    'unannotated_posts': unannotated},
        'own_performance': {
            'groups': {group: _summary(by_group[group], top_cut) for group in GROUPS},
            'accounts': sorted(({'author_id': author, 'username': name, **_summary(values, top_cut), 'points': sum(values)}
                                for (author, name), values in own_accounts.items()), key=lambda row: -row['points']),
            'kinds': {kind: _summary(values, top_cut) for kind, values in sorted(own_kinds.items())},
            'top_posts': [{'post_id': post_id, 'points': value, 'excerpt': ' '.join((bodies.get(post_id) or '').split())[:160]}
                          for value, post_id in top]},
        'timing': [{'hour': hour, **{group: _summary(by_hour[hour][group], top_cut) for group in ('citizens', 'own')}}
                   for hour in range(24)],
        'content': {name: [{'group': group, 'value': key, **_summary(values, top_cut)}
                           for (group, key), values in sorted(groups.items(), key=lambda item: (item[0][0], str(item[0][1])))]
                    for name, groups in sorted(features.items())},
        'top_decile_points': top_cut,
        # Kept out of the response; the accounts page ranks authors by it.
        'author_points': dict(author_points),
        'caveats': ['互动分按 Red 权重估算（点赞 5、转发 10、回复 15，不含浏览），包含所有账号的互动，不等于官方积分',
                    f'互动统计只计发布满 {MATURE_HOURS} 小时、且已有互动快照的帖子；快照时间各不相同',
                    '立场是关键词规则的估计，描述的是帖子文本，不等于发帖人真实投票意向',
                    '机器人按共享规则判定（与 Agent 选目标一致）：行为判定命中，或长时间活跃特征命中 ≥2 个；只有重复文本的账号仍算公民',
                    '时段按悉尼时间；样本少的时段中位数波动大，请结合 posts 数看']}


def public(result):
    return {key: value for key, value in result.items() if key != 'author_points'}


class Insights:
    """One cached result per window length, refreshed in the background when stale."""
    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._refreshing = set()
        self._cached = {}

    def _refresh(self, hours):
        try:
            self._cached[hours] = (time.monotonic(), build(self.db_path, hours=hours))
        finally:
            with self._lock:
                self._refreshing.discard(hours)

    def result(self, hours=DEFAULT_HOURS):
        """The full cached result (including author_points) and whether it is stale."""
        if type(hours) is not int or not MIN_HOURS <= hours <= MAX_HOURS:
            raise ValueError(f'hours 必须是 {MIN_HOURS}–{MAX_HOURS} 的整数')
        cached = self._cached.get(hours)
        if cached is None:
            with self._lock:
                if hours not in self._cached:
                    self._cached[hours] = (time.monotonic(), build(self.db_path, hours=hours))
                return self._cached[hours][1], False
        stale = time.monotonic() - cached[0] >= CACHE_SECONDS
        if stale:
            with self._lock:
                if hours not in self._refreshing:
                    self._refreshing.add(hours)
                    threading.Thread(target=self._refresh, args=(hours,), name='ctn-insights', daemon=True).start()
        return cached[1], stale

    def peek(self, hours=DEFAULT_HOURS, *, start=True):
        """The cached result without waiting; with ``start`` a missing or stale one is rebuilt in the background."""
        cached = self._cached.get(hours)
        if start and (cached is None or time.monotonic() - cached[0] >= CACHE_SECONDS):
            with self._lock:
                if hours not in self._refreshing:
                    self._refreshing.add(hours)
                    threading.Thread(target=self._refresh, args=(hours,), name='ctn-insights', daemon=True).start()
        return cached[1] if cached else None

    def get(self, hours=DEFAULT_HOURS):
        value, stale = self.result(hours)
        return {**public(value), 'stale': stale}
