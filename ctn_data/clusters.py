"""Behaviour clusters of accounts over the last 48 hours (GET /api/clusters).

Accounts are linked when they posted the same non-chain text (>= 3 accounts,
blue_reports._texts) or when one account reposted a cluster at least four
times (repost boosters, deep-analysis competitors.md). Clusters are operator
batches inferred from behaviour, not confirmed teams. Our own accounts are
reported separately as a baseline and never join a cluster.

Engagement is counted from citizen-proxy accounts only (citizens.py): replies
x15 and reposts x10. Likes and views cannot be attributed to an author, so
they are left out; the estimate is a lower bound, not official score.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import blue_reports
from .citizens import citizen_authors
from .db import connect, utc_now
from .imports import normalize_timestamp
from .pipeline import post_reply_ratio

LOGGER = logging.getLogger(__name__)

WINDOW_HOURS = 48
MIN_ACCOUNTS = 3
BOOSTER_MIN_REPOSTS = 4
TOP_CLUSTERS = 30
EXCERPT_CHARS = 120
CACHE_SECONDS = 600
NOTE = '簇是按行为聚出来的账号组，不等于已确认的队伍'
CITIZEN_POINTS = {'reply': 15, 'repost': 10}


def _excerpt(text):
    text = ' '.join((text or '').split())
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS - 1] + '…'


def _read(conn, since, until):
    posts = [dict(row) for row in conn.execute(
        '''SELECT p.post_id,p.author_id,p.kind,p.reply_to_post_id,p.repost_of_post_id,p.body,
                  ctn_timestamp(p.created_at) AS created,pa.stance
           FROM posts p LEFT JOIN post_annotations pa ON pa.post_id=p.post_id
           WHERE ctn_timestamp(p.created_at)>=? AND ctn_timestamp(p.created_at)<=? AND p.author_id IS NOT NULL''',
        (since, until))]
    authors = {post['post_id']: post['author_id'] for post in posts}
    missing = sorted({post[key] for post in posts for key in ('reply_to_post_id', 'repost_of_post_id')
                      if post[key] and post[key] not in authors})
    for offset in range(0, len(missing), 500):
        batch = missing[offset:offset + 500]
        authors.update((row['post_id'], row['author_id']) for row in conn.execute(
            'SELECT post_id,author_id FROM posts WHERE post_id IN (' + ','.join('?' for _ in batch) + ')', batch))
    trending = {row['post_id'] for row in conn.execute(
        "SELECT DISTINCT post_id FROM feed_observations WHERE source='trending' AND ctn_timestamp(observed_at)>=?", (since,))}
    own = {row['account_id'] for row in conn.execute('SELECT account_id FROM team_accounts WHERE is_own=1')}
    return posts, authors, trending, own


def _components(posts, authors, own):
    """Union-find over shared texts, then repost boosters; own accounts stay out."""
    texts = blue_reports._texts([{**post, 'body': post['body'] or '', 'at': blue_reports._epoch(post['created'])}
                                 for post in posts if post['kind'] != 'repost' and post['author_id'] not in own])
    components = texts['components']
    linked = set(components.parent)
    boosts = Counter()
    for post in posts:
        target = authors.get(post['repost_of_post_id']) if post['repost_of_post_id'] else None
        if post['kind'] == 'repost' and target in linked and post['author_id'] not in own:
            boosts[(post['author_id'], components.find(target))] += 1
    for (booster, root), count in boosts.items():
        if count >= BOOSTER_MIN_REPOSTS:
            components.union([root, booster])
    groups = defaultdict(set)
    for node in list(components.parent):
        groups[components.find(node)].add(node)
    return {root: members for root, members in groups.items() if len(members) >= MIN_ACCOUNTS}, texts


def _profile(members, posts, authors, trending, citizens, annotations, accounts, start, texts, blue):
    hourly = [0] * WINDOW_HOURS
    member_writes = Counter()
    kinds, stances = Counter(), Counter()
    replies_known = replies_to_citizens = originals = originals_trending = 0
    citizen_actions, citizen_replies_per_post = Counter(), Counter()
    signatures = Counter()
    by_id = {}
    for post in posts:
        author = post['author_id']
        target_author = authors.get(post['reply_to_post_id'] or post['repost_of_post_id'])
        if author in members:
            by_id[post['post_id']] = post
            member_writes[author] += 1
            at = blue_reports._epoch(post['created'])
            if at is not None:
                index = int((at - start) // 3600)
                if 0 <= index < WINDOW_HOURS:
                    hourly[index] += 1
            kinds[post['kind']] += 1
            if post['stance']:
                stances[post['stance']] += 1
            if post['kind'] == 'reply' and target_author is not None:
                replies_known += 1
                replies_to_citizens += target_author in citizens
            if post['kind'] == 'post':
                originals += 1
                originals_trending += post['post_id'] in trending
            signature = texts['signature_of'].get(post['post_id'])
            if signature in texts['shared']:
                signatures[signature] += 1
        elif author in citizens and target_author in members and post['kind'] in CITIZEN_POINTS:
            citizen_actions[post['kind']] += 1
            if post['kind'] == 'reply':
                citizen_replies_per_post[post['reply_to_post_id']] += 1
    sided = sum(stances[key] for key in ('yes', 'no', 'undecided'))
    bots = [annotations[member] for member in members if annotations.get(member, {}).get('is_bot')]
    reasons = Counter(reason for row in bots for reason in row['reasons'])
    examples = []
    if signatures:
        signature = signatures.most_common(1)[0][0]
        post = next(post for post in by_id.values() if texts['signature_of'].get(post['post_id']) == signature)
        examples.append({'post_id': post['post_id'], 'why': 'shared_text', 'excerpt': _excerpt(post['body'])})
    engaged = [post_id for post_id, _ in citizen_replies_per_post.most_common() if post_id in by_id]
    for post_id in engaged + [post['post_id'] for post in by_id.values() if post['kind'] == 'post' and post['body']]:
        if len(examples) >= 2:
            break
        if all(example['post_id'] != post_id for example in examples):
            examples.append({'post_id': post_id, 'why': 'citizen_replies' if post_id in citizen_replies_per_post else 'original',
                             'excerpt': _excerpt(by_id[post_id]['body'])})
    writes = sum(kinds.values())
    points = sum(CITIZEN_POINTS[kind] * count for kind, count in citizen_actions.items())
    blue_items = [item for item in (blue or {}).get('candidates', []) if item.get('author_id') in members]
    return {
        'accounts': len(members), 'members_total': len(members),
        'members': sorted(({'account_id': member, 'username': accounts.get(member),
                            'is_bot': annotations.get(member, {}).get('is_bot', False),
                            'bot_reasons': annotations.get(member, {}).get('reasons', []),
                            'writes': member_writes[member]} for member in members),
                          key=lambda row: (-row['writes'], row['username'] or '', row['account_id'])),
        'writes': writes, 'hourly_writes': hourly,
        'kinds': {kind: kinds[kind] for kind in ('post', 'reply', 'repost')},
        'post_share': round(kinds['post'] / writes, 3) if writes else None,
        'reply_share': round(kinds['reply'] / writes, 3) if writes else None,
        'post_reply_ratio': post_reply_ratio(kinds['post'], kinds['reply']),
        'replies_to_citizens_share': round(replies_to_citizens / replies_known, 3) if replies_known else None,
        'stance': {key: round(stances[key] / sided, 3) if sided else None for key in ('yes', 'no', 'undecided')},
        'stance_posts': sided,
        'trending_share': round(originals_trending / originals, 3) if originals else None,
        'citizen_replies': citizen_actions['reply'], 'citizen_reposts': citizen_actions['repost'],
        'citizen_points_per_hour': round(points / WINDOW_HOURS, 1),
        'bot_flagged': len(bots), 'bot_share': round(len(bots) / len(members), 3) if members else None,
        'bot_reasons': dict(reasons.most_common()),
        # Blue uses its own 24 h text clusters; link through the shared members.
        'blue_candidates': len(blue_items) if blue is not None else None,
        'blue_cluster_ids': sorted({item['cluster_id'] for item in blue_items if item.get('cluster_id')}),
        'examples': examples}


def build(db_path, *, now=None, blue=None):
    """``blue`` is a cached /api/blue/candidates result (or None); it is never computed here."""
    now = datetime.fromisoformat(normalize_timestamp(now).replace('Z', '+00:00')) if now else datetime.now(timezone.utc)
    since, until = normalize_timestamp(now - timedelta(hours=WINDOW_HOURS)), normalize_timestamp(now)
    conn = connect(db_path)
    try:
        posts, authors, trending, own = _read(conn, since, until)
        groups, texts = _components(posts, authors, own)
        clustered = set().union(*groups.values()) if groups else set()
        involved = {post['author_id'] for post in posts} | set(authors.values())
        citizens = citizen_authors(db_path, involved, exclude=clustered | own)
        members = sorted(clustered | own)
        annotations, accounts = {}, {}
        for offset in range(0, len(members), 500):
            batch = members[offset:offset + 500]
            marks = ','.join('?' for _ in batch)
            for row in conn.execute(f'SELECT account_id,is_bot,bot_reasons_json FROM account_annotations WHERE account_id IN ({marks})', batch):
                annotations[row['account_id']] = {'is_bot': bool(row['is_bot']),
                                                  'reasons': json.loads(row['bot_reasons_json'] or '[]')}
            accounts.update((row['account_id'], row['username']) for row in conn.execute(
                f'SELECT account_id,username FROM accounts WHERE account_id IN ({marks})', batch))
    finally:
        conn.close()
    start = blue_reports._epoch(since)
    profile = lambda group: _profile(group, posts, authors, trending, citizens, annotations, accounts, start, texts, blue)
    clusters = [{'cluster_id': f'c{root}', 'is_own': False, **profile(group)} for root, group in groups.items()]
    clusters.sort(key=lambda row: (-row['citizen_points_per_hour'], -row['writes']))
    return {'generated_at': utc_now(), 'window': {'hours': WINDOW_HOURS, 'from': since, 'to': until},
            'note': NOTE, 'own': {'cluster_id': 'own', 'is_own': True, 'label': '我们自己（Agent Zero）', **profile(own)},
            'clusters': clusters[:TOP_CLUSTERS], 'clusters_total': len(clusters),
            'clustered_accounts': len(clustered), 'citizen_accounts': len(citizens),
            'method': {'link': ['同一段文字被 ≥3 个账号发布（≥40 字，不含接龙）',
                                f'对同一簇转发 ≥{BOOSTER_MIN_REPOSTS} 次的账号并入该簇'],
                       # The deep-analysis report also linked by activity correlation
                       # and creation time; those pairwise comparisons are not run here.
                       'not_used': ['30 分钟活跃度相关', '创建时间相近'],
                       'min_accounts': MIN_ACCOUNTS,
                       'citizen_points': '只计市民（citizens.py 口径）的回复 ×15、转发 ×10，平均到每小时；点赞和浏览无法归属，不计'}}


class Clusters:
    """One cached result, refreshed in the background when stale."""
    def __init__(self, db_path, *, blue=None):
        self.db_path, self.blue = db_path, blue
        self._lock = threading.Lock()
        self._refreshing = False
        self._cached = None

    def _build(self):
        return build(self.db_path, blue=self.blue.peek() if self.blue is not None else None)

    def _refresh(self):
        try:
            self._cached = (time.monotonic(), self._build())
        except Exception:
            # Keep serving the last result (still marked stale); the next request retries.
            LOGGER.exception('cluster refresh failed')
        finally:
            with self._lock:
                self._refreshing = False

    def get(self):
        cached = self._cached
        if cached is None:
            with self._lock:
                if self._cached is None:
                    self._cached = (time.monotonic(), self._build())
                return {**self._cached[1], 'stale': False}
        stale = time.monotonic() - cached[0] >= CACHE_SECONDS
        if stale:
            with self._lock:
                if not self._refreshing:
                    self._refreshing = True
                    threading.Thread(target=self._refresh, name='ctn-clusters', daemon=True).start()
        return {**cached[1], 'stale': stale}
