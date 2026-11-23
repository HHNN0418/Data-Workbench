"""Read models used by the local dashboard and CSV exports."""
from __future__ import annotations

import json
from collections import defaultdict
import threading
import time
from datetime import datetime, timedelta, timezone
from .db import connect
from .coverage import coverage, changes


def rows(connection, sql, params=()):
    return [dict(row) for row in connection.execute(sql, params)]


def paging(filters):
    limit = max(1, min(200, int(filters.get('limit', 50))))
    offset = max(0, int(filters.get('offset', 0)))
    return limit, offset


def social_context(db_path, filters):
    """Collected platform tags and the two named news accounts, without a fetch."""
    from .social_topics import read_social_context
    view = filters.get('view', 'tags')
    if view not in ('tags', 'news_accounts'):
        raise ValueError('view 只支持 tags/news_accounts')
    popular_value = str(filters.get('popular', 'false')).lower()
    if popular_value not in ('true', 'false'):
        raise ValueError('popular 必须为 true/false')
    limit, offset = paging(filters)
    q = str(filters.get('q', '')).strip()[:200]
    connection = connect(db_path)
    try:
        connection.execute('BEGIN')
        result = read_social_context(connection, q=q, limit=limit, offset=offset,
                                     popular=popular_value == 'true')
        result['view'] = view
        if view == 'news_accounts':
            clauses = ["LOWER(a.username) IN ('dailykingstonia','kingstoniaherald')"]
            args = []
            if q:
                clauses.append('(LOWER(p.body) LIKE LOWER(?) OR LOWER(a.username) LIKE LOWER(?) OR CAST(p.post_id AS TEXT)=?)')
                args = ['%' + q + '%', '%' + q + '%', q]
            join = ' FROM posts p JOIN accounts a ON p.author_id=a.account_id WHERE ' + ' AND '.join(clauses)
            result['total'] = connection.execute('SELECT COUNT(*)' + join, args).fetchone()[0]
            result['items'] = rows(connection, 'SELECT p.*,a.username,a.display_name' + join +
                                   ' ORDER BY ctn_timestamp(p.created_at) DESC NULLS LAST,p.post_id DESC LIMIT ? OFFSET ?',
                                   args + [limit, offset])
        return result
    finally:
        connection.close()


REFERENCE_FIELDS = ('repost_of_post_id', 'reply_to_post_id', 'root_post_id')


def _reference_data(connection, posts):
    targets = sorted({post[key] for post in posts for key in REFERENCE_FIELDS if post[key] is not None})
    found, checkpoints = {}, {}
    keys = [f'relation:{target}' for target in targets] + [f"counter_attempt:{post['post_id']}" for post in posts]
    # Stay below SQLite parameter limits even with three distinct parents per post.
    for offset in range(0, len(targets), 400):
        batch = targets[offset:offset + 400]
        marks = ','.join('?' for _ in batch)
        found.update((row['post_id'], dict(row)) for row in connection.execute(
            f'SELECT p.*,a.username FROM posts p LEFT JOIN accounts a ON p.author_id=a.account_id WHERE p.post_id IN ({marks})', batch))
    for offset in range(0, len(keys), 400):
        batch = keys[offset:offset + 400]
        marks = ','.join('?' for _ in batch)
        checkpoints.update((row['key'], dict(row)) for row in connection.execute(
            f'SELECT key,value,updated_at FROM checkpoints WHERE key IN ({marks})', batch))
    return found, checkpoints


def references(connection, post, reference_data=None):
    found_posts, checkpoints = reference_data if reference_data is not None else _reference_data(connection, [post])
    result = {}
    for key in REFERENCE_FIELDS:
        target = post[key]
        if target is None:
            continue
        found = found_posts.get(target)
        checkpoint = checkpoints.get(f'relation:{target}')
        state = json.loads(checkpoint['value']) if checkpoint else {}
        result[key] = dict(post_id=target, status='available' if found else state.get('status','pending'), post=dict(found) if found else None)
    return result


# Stored by annotations.py; a post not yet annotated has stance null.
ANNOTATION_FIELDS = 'pa.stance,pa.concerns_json,pa.thread_depth,COALESCE(aa.is_bot,0) AS is_bot,aa.bot_reasons_json'
ANNOTATION_JOINS = (' LEFT JOIN post_annotations pa ON pa.post_id=p.post_id'
                    ' LEFT JOIN account_annotations aa ON aa.account_id=p.author_id')


def _annotation(item):
    item['concerns'] = json.loads(item.pop('concerns_json') or '[]')
    item['bot_reasons'] = json.loads(item.pop('bot_reasons_json') or '[]')
    # is_bot is the single agreed rule, stored by annotations.refresh_accounts.
    item['is_bot'] = bool(item['is_bot'])
    return item


def posts(db_path, filters):
    limit, offset = paging(filters)
    clauses, args = [], []
    if filters.get('q'):
        clauses.append('(LOWER(p.body) LIKE LOWER(?) OR CAST(p.post_id AS TEXT)=?)')
        args.extend(['%' + str(filters['q']) + '%', str(filters['q'])])
    if filters.get('author_id'):
        clauses.append('p.author_id=?')
        args.append(int(filters['author_id']))
    if filters.get('kind'):
        clauses.append('p.kind=?')
        args.append(filters['kind'])
    if filters.get('own') == 'own':
        clauses.append('t.is_own=1')
    elif filters.get('own') == 'unknown':
        clauses.append('t.account_id IS NULL')
    feed = filters.get('feed', 'all')
    if feed not in ('all', 'latest', 'trending'):
        raise ValueError('feed 只支持 all/latest/trending')
    connection = connect(db_path)
    try:
        connection.execute('BEGIN')
        observed_at = None
        batches = []
        if feed == 'latest':
            clauses.append("EXISTS(SELECT 1 FROM feed_observations f WHERE f.post_id=p.post_id AND f.source IN ('latest','browser_latest'))")
        if feed == 'trending':
            # Only one observed page: never combine stale ranks into a current leaderboard.
            latest = connection.execute("SELECT observed_at FROM feed_observations WHERE source='trending' AND rank_position=1 AND ctn_timestamp(observed_at) IS NOT NULL ORDER BY ctn_timestamp(observed_at) DESC,id DESC LIMIT 1").fetchone()
            observed_at = latest[0] if latest else None
            batches = rows(connection, "SELECT observed_at,COUNT(*) AS posts,MIN(rank_position) AS rank_from,MAX(rank_position) AS rank_to FROM feed_observations WHERE source='trending' GROUP BY observed_at ORDER BY ctn_timestamp(observed_at) DESC NULLS LAST LIMIT 30")
            if filters.get('snapshot'):
                observed_at = filters['snapshot']
                if not connection.execute("SELECT 1 FROM feed_observations WHERE source='trending' AND observed_at=? LIMIT 1", (observed_at,)).fetchone():
                    raise ValueError('Trending 观测页不存在')
            clauses.append("f.id IS NOT NULL")
        feed_join = " LEFT JOIN feed_observations f ON f.post_id=p.post_id AND f.source='trending' AND f.observed_at=?" if feed == 'trending' else ""
        join_args = [observed_at] if feed == 'trending' else []
        feed_fields = ',f.rank_position,f.global_hot_score,f.observed_at AS feed_observed_at' if feed == 'trending' else ''
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        order = 'f.rank_position IS NULL,f.rank_position ASC NULLS FIRST,p.post_id' if feed == 'trending' else 'ctn_timestamp(p.created_at) DESC NULLS LAST,p.post_id DESC'
        total = connection.execute('SELECT COUNT(*) FROM posts p LEFT JOIN team_accounts t ON p.author_id=t.account_id' + feed_join + where, join_args + args).fetchone()[0]
        # Apply every membership/feed filter and its ordering before looking up
        # counter history. Keep both reads in this one consistent transaction.
        items = rows(connection, '''SELECT p.*,a.username,a.display_name,t.team_label,t.is_own,''' + ANNOTATION_FIELDS + feed_fields + '''
            FROM posts p LEFT JOIN accounts a ON p.author_id=a.account_id
            LEFT JOIN team_accounts t ON p.author_id=t.account_id''' + ANNOTATION_JOINS + '''
            ''' + feed_join + where + ' ORDER BY ' + order + ' LIMIT ? OFFSET ?', join_args + args + [limit, offset])
        counters = {}
        if items:
            marks = ','.join('?' for _ in items)
            counters = {row['post_id']: dict(row) for row in connection.execute('''
                SELECT p.post_id,c.like_count,c.reply_count,c.repost_count,c.view_count,
                    c.observed_at AS counter_observed_at FROM posts p
                LEFT JOIN counter_snapshots c ON c.id=(SELECT id FROM counter_snapshots
                    WHERE post_id=p.post_id AND ctn_timestamp(observed_at) IS NOT NULL
                    ORDER BY ctn_timestamp(observed_at) DESC,id DESC LIMIT 1)
                WHERE p.post_id IN (''' + marks + ')', [item['post_id'] for item in items])}
        reference_data = _reference_data(connection, items)
        for item in items:
            item.update(counters[item['post_id']])
            item['references'] = references(connection, item, reference_data)
            item['latest_counts'] = {k: item.pop(k) for k in ('like_count','reply_count','repost_count','view_count')}
            item['latest_counts']['observed_at'] = item.pop('counter_observed_at')
            attempt = reference_data[1].get(f"counter_attempt:{item['post_id']}")
            _annotation(item)
            item['counter_state'] = ('partial' if any(item['latest_counts'][k] is None for k in ('like_count','reply_count','repost_count','view_count')) else 'sampled') if item['latest_counts']['observed_at'] else ('failed' if attempt and json.loads(attempt['value']).get('status')=='failed' else 'pending')
        coverage = {}
        for row in connection.execute("SELECT key,value,updated_at FROM checkpoints WHERE key IN ('feed:latest','feed:latest:live','feed:trending')"):
            state = json.loads(row['value'])
            coverage[row['key']] = dict(pending=bool(state.get('cursor')), updated_at=row['updated_at'])
        return dict(items=items, total=total, limit=limit, offset=offset, feed=feed,
                    feed_observed_at=observed_at, feed_batches=batches, coverage=coverage)
    finally:
        connection.close()


def post_detail(db_path, post_id):
    connection = connect(db_path)
    try:
        post = connection.execute('SELECT p.*,a.username,t.team_label,t.is_own,' + ANNOTATION_FIELDS + ' FROM posts p LEFT JOIN accounts a ON p.author_id=a.account_id LEFT JOIN team_accounts t ON p.author_id=t.account_id' + ANNOTATION_JOINS + ' WHERE p.post_id=?', (post_id,)).fetchone()
        if post is None:
            return None
        post = _annotation(dict(post))
        reply_state = connection.execute('SELECT value,updated_at FROM checkpoints WHERE key=?', (f'replies:{post_id}',)).fetchone()
        return dict(post=dict(post), references=references(connection, post),
                    reply_coverage={**json.loads(reply_state['value']), 'updated_at':reply_state['updated_at']} if reply_state else {'status':'pending'},
                    versions=rows(connection,'SELECT * FROM post_versions WHERE post_id=? ORDER BY ctn_timestamp(observed_at) ASC NULLS FIRST,id',(post_id,)),
                    counters=changes(rows(connection,'SELECT * FROM counter_snapshots WHERE post_id=? ORDER BY ctn_timestamp(observed_at) ASC NULLS FIRST,id',(post_id,))),
                    counter_conflicts=rows(connection,'SELECT * FROM counter_conflicts WHERE post_id=? ORDER BY ctn_timestamp(observed_at) ASC NULLS FIRST,id',(post_id,)),
                    feed_observations=rows(connection,'SELECT * FROM feed_observations WHERE post_id=? ORDER BY ctn_timestamp(observed_at) DESC NULLS LAST,id DESC',(post_id,)),
                    labels=rows(connection,'SELECT * FROM post_labels WHERE post_id=? ORDER BY created_at DESC',(post_id,)),
                    replies=rows(connection,'SELECT * FROM posts WHERE reply_to_post_id=? ORDER BY ctn_timestamp(created_at) ASC NULLS FIRST,post_id',(post_id,)),
                    reposts=rows(connection,'SELECT * FROM posts WHERE repost_of_post_id=? ORDER BY ctn_timestamp(created_at) ASC NULLS FIRST,post_id',(post_id,)))
    finally:
        connection.close()


ACCOUNT_SORTS = ('recent', 'posts', 'engagement')
BOT_RULE = "EXISTS(SELECT 1 FROM account_annotations aa WHERE aa.account_id=a.account_id AND aa.is_bot=1)"
# A lean needs a few posts that take a side; otherwise it stays undecided.
LEAN_MIN_SIDED = 3


def _stance_lean(counts):
    sided = counts.get('yes', 0) + counts.get('no', 0)
    if sided < LEAN_MIN_SIDED:
        return 'undecided' if counts else None
    share = counts.get('yes', 0) / sided
    return 'yes' if share >= 0.6 else 'no' if share <= 0.4 else 'mixed'


def accounts(db_path, filters, author_points=None):
    """Accounts with activity and annotation fields.

    ``author_points`` (author_id -> engagement points over the insights
    window) enables sort=engagement and the engagement_72h field.
    """
    limit, offset = paging(filters)
    sort = filters.get('sort') or 'recent'
    if sort not in ACCOUNT_SORTS:
        raise ValueError('sort 只支持 recent/posts/engagement')
    bots = filters.get('bots') or 'all'
    if bots not in ('all', 'only', 'exclude'):
        raise ValueError('bots 只支持 all/only/exclude')
    clauses, args = [], []
    if filters.get('q'):
        clauses.append('(LOWER(a.username) LIKE LOWER(?) OR LOWER(a.display_name) LIKE LOWER(?) OR CAST(a.account_id AS TEXT)=?)')
        args += ['%' + filters['q'] + '%', '%' + filters['q'] + '%', filters['q']]
    if bots != 'all':
        clauses.append(BOT_RULE if bots == 'only' else 'NOT ' + BOT_RULE)
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    engagement_ready = author_points is not None
    connection = connect(db_path)
    try:
        total = connection.execute('SELECT COUNT(*) FROM accounts a' + where, args).fetchone()[0]
        if sort == 'engagement' and engagement_ready:
            ids = [row[0] for row in connection.execute('SELECT a.account_id FROM accounts a' + where, args)]
            ids.sort(key=lambda account: (-author_points.get(account, 0), -account))
            page = ids[offset:offset + limit]
        else:
            # Choose the page before counting posts for the page's rows only.
            order = ('(SELECT COUNT(*) FROM posts p WHERE p.author_id=a.account_id) DESC,a.account_id DESC'
                     if sort == 'posts' else 'a.last_seen DESC,a.account_id DESC')
            page = [row[0] for row in connection.execute(
                'SELECT a.account_id FROM accounts a' + where + ' ORDER BY ' + order + ' LIMIT ? OFFSET ?', args + [limit, offset])]
        items = []
        if page:
            marks = ','.join('?' for _ in page)
            since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat().replace('+00:00', 'Z')
            found = {row['account_id']: row for row in rows(connection, '''SELECT a.*,t.team_label,t.is_own,t.evidence,
                (SELECT COUNT(*) FROM posts p WHERE p.author_id=a.account_id) AS post_count,
                (SELECT COUNT(*) FROM posts p WHERE p.author_id=a.account_id AND ctn_timestamp(p.created_at)>=?) AS posts_24h,
                COALESCE(aa.is_bot,0) AS is_bot,aa.bot_reasons_json
                FROM accounts a LEFT JOIN team_accounts t ON a.account_id=t.account_id
                LEFT JOIN account_annotations aa ON aa.account_id=a.account_id
                WHERE a.account_id IN (''' + marks + ')', [since] + page)}
            stances = defaultdict(dict)
            for row in connection.execute('SELECT author_id,stance,COUNT(*) AS n FROM post_annotations WHERE author_id IN ('
                                          + marks + ') GROUP BY author_id,stance', page):
                stances[row['author_id']][row['stance']] = row['n']
            for account in page:
                item = found[account]
                item['bot_reasons'] = json.loads(item.pop('bot_reasons_json') or '[]')
                item['is_bot'] = bool(item['is_bot'])
                item['stance_counts'] = stances.get(account, {})
                item['stance_lean'] = _stance_lean(item['stance_counts'])
                item['engagement_72h'] = author_points.get(account, 0) if engagement_ready else None
                items.append(item)
        return dict(items=items, total=total, limit=limit, offset=offset, sort=sort, bots=bots,
                    engagement_ready=engagement_ready)
    finally:
        connection.close()


def news(db_path, filters):
    limit, offset = paging(filters)
    where, args = '', []
    if filters.get('q'):
        where = ' WHERE LOWER(title) LIKE LOWER(?) OR LOWER(body) LIKE LOWER(?)'
        args = ['%' + filters['q'] + '%'] * 2
    connection = connect(db_path)
    try:
        return dict(items=rows(connection,'SELECT * FROM news_articles' + where + ' ORDER BY published_at DESC NULLS LAST LIMIT ? OFFSET ?',args+[limit,offset]),
                    total=connection.execute('SELECT COUNT(*) FROM news_articles'+where,args).fetchone()[0],limit=limit,offset=offset)
    finally:
        connection.close()


def activity(db_path, filters):
    limit, offset = paging(filters)
    clauses, args = [], []
    if filters.get('track'):
        clauses.append('track=?')
        args.append(filters['track'])
    if filters.get('q'):
        clauses.append('LOWER(message) LIKE LOWER(?)')
        args.append('%'+filters['q']+'%')
    where = (' WHERE '+' AND '.join(clauses)) if clauses else ''
    connection = connect(db_path)
    try:
        # Live-table rows the CSV export has not reached lead the first page.
        from .official_feed import pending_rows
        live = [row for row in reversed(pending_rows(connection))
                if (not filters.get('track') or row['track'] == filters['track'])
                and (not filters.get('q') or filters['q'].lower() in row['message'].lower())]
        items = rows(connection,'SELECT * FROM official_activity'+where+' ORDER BY occurred_at DESC NULLS LAST,id DESC LIMIT ? OFFSET ?',args+[limit,offset])
        return dict(items=(live+items)[:limit] if offset == 0 else items,
                    total=connection.execute('SELECT COUNT(*) FROM official_activity'+where,args).fetchone()[0]+len(live),
                    imports=rows(connection,'SELECT * FROM activity_imports ORDER BY imported_at DESC'),
                    score_snapshots=rows(connection,'SELECT * FROM score_snapshots ORDER BY ctn_timestamp(observed_at) DESC NULLS LAST,id DESC'),limit=limit,offset=offset)
    finally:
        connection.close()


def request_traffic(connection, *, now=None, minutes=5):
    """Actual outbound HTTP logs across direct and proxy workers, not job runs."""
    now=now or datetime.now(timezone.utc)
    boundary=now-timedelta(minutes=minutes)
    start=now-timedelta(minutes=2*minutes)
    # The indexed prefix bounds the scan; julianday handles fractional seconds
    # and the exact half-open comparison boundary consistently on both engines.
    stats=rows(connection,'''SELECT
        CASE WHEN julianday(occurred_at)>=julianday(?) THEN 'current' ELSE 'previous' END AS period,
        COALESCE(service,'unknown') AS source,COUNT(*) AS requests,
        SUM(CASE WHEN status>=200 AND status<300 THEN 1 ELSE 0 END) AS success,
        SUM(CASE WHEN status=429 THEN 1 ELSE 0 END) AS rate_limited,
        SUM(CASE WHEN status IS NULL OR status<200 OR status>=300 THEN 1 ELSE 0 END) AS failed,
        MAX(occurred_at) AS last_request_at
        FROM api_call_logs WHERE occurred_at>=? AND julianday(occurred_at)>=julianday(?)
        AND julianday(occurred_at)<=julianday(?) GROUP BY period,source ORDER BY period,source''',
        (boundary.isoformat(),start.strftime('%Y-%m-%dT%H:%M:%S'),start.isoformat(),now.isoformat()))
    fields=('requests','success','rate_limited','failed')
    totals={period:{key:0 for key in fields} for period in ('current','previous')}
    for row in stats:
        for key in fields:totals[row['period']][key]+=row[key]
    for value in totals.values():
        value['per_minute']=round(value['requests']/minutes,1)
        value['rate_limited_percent']=round(value['rate_limited']/value['requests']*100,2) if value['requests'] else None
    return {**totals,'window_minutes':minutes,'updated_at':now.isoformat(),
            'last_request_at':max((row['last_request_at'] for row in stats),default=None),
            'by_source':[{**row,'per_minute':round(row['requests']/minutes,1)} for row in stats if row['period']=='current']}


# Whole-history coverage and status totals take seconds on the live database,
# while the page polls every few seconds. Share one result per minute across
# viewers; while one request refreshes it, others keep the previous result.
SLOW_OPERATIONS_SECONDS = 60
_slow_operations = {}
_slow_operations_lock = threading.Lock()


def _operations_totals(db_path, connection, config, runs):
    key = str(db_path)
    cached = _slow_operations.get(key)
    if cached and time.monotonic() - cached[0] < SLOW_OPERATIONS_SECONDS:
        return cached[1]
    if not _slow_operations_lock.acquire(blocking=cached is None):
        return cached[1]
    try:
        cached = _slow_operations.get(key)
        if cached and time.monotonic() - cached[0] < SLOW_OPERATIONS_SECONDS:
            return cached[1]
        totals = dict(coverage=coverage(connection, config, runs),
                      status_codes=rows(connection, 'SELECT status,COUNT(*) AS count FROM api_call_logs GROUP BY status'))
        _slow_operations[key] = (time.monotonic(), totals)
        return totals
    finally:
        _slow_operations_lock.release()


def operations(db_path, config, collector_running=False):
    from .collector import is_collecting
    connection = connect(db_path)
    try:
        runs = rows(connection,'SELECT * FROM collection_runs ORDER BY id DESC LIMIT 30')
        for run in runs:
            try:
                run['metrics'] = json.loads(run.get('metrics_json') or '{}')
            except ValueError:
                run['metrics'] = {}
        return dict(runs=runs,traffic=request_traffic(connection),logs=rows(connection,'SELECT * FROM api_call_logs ORDER BY id DESC LIMIT 200'),
                    checkpoints=rows(connection,"SELECT * FROM checkpoints WHERE (key>='feed:' AND key<'feed;') OR key='counter_scheduler' ORDER BY key"),
                    **_operations_totals(db_path, connection, config, runs),
                    collector_running=collector_running or is_collecting(db_path),config=config)
    finally:
        connection.close()


OVERVIEW_SCORE_DETAIL = frozenset(('reconciliation', 'snapshots'))
# score_overview re-reads every official activity row (~0.5 s live); the
# overview polls, so viewers share one summary per half minute.
OVERVIEW_SCORE_SECONDS = 30
_overview_score = {}
_overview_score_lock = threading.Lock()


def _overview_score_summary(db_path):
    from .scoring import score_overview, score_pace
    key = str(db_path)
    cached = _overview_score.get(key)
    if cached and time.monotonic() - cached[0] < OVERVIEW_SCORE_SECONDS:
        return cached[1]
    if not _overview_score_lock.acquire(blocking=cached is None):
        return cached[1]
    try:
        cached = _overview_score.get(key)
        if cached and time.monotonic() - cached[0] < OVERVIEW_SCORE_SECONDS:
            return cached[1]
        summary = ({k: v for k, v in score_overview(db_path).items() if k not in OVERVIEW_SCORE_DETAIL},
                   # Distance to the 7,000-point poll threshold for the cockpit.
                   score_pace(db_path))
        _overview_score[key] = (time.monotonic(), summary)
        return summary
    finally:
        _overview_score_lock.release()


def overview(db_path):
    connection = connect(db_path)
    try:
        counts = {name:connection.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in
                  ('posts','accounts','counter_snapshots','news_articles','api_call_logs','official_activity')}
        counts['api_calls'] = counts.pop('api_call_logs')
        counts['news_full_bodies'] = connection.execute("SELECT COUNT(*) FROM news_articles WHERE body IS NOT NULL AND body<>''").fetchone()[0]
        latest = connection.execute('SELECT * FROM collection_runs ORDER BY id DESC LIMIT 1').fetchone()
        latest = dict(latest) if latest else None
        if latest:
            try: latest['metrics']=json.loads(latest.get('metrics_json') or '{}')
            except ValueError: latest['metrics']={}
        mode = connection.execute("SELECT value FROM meta WHERE key='mode'").fetchone()[0]
        score, threshold = _overview_score_summary(db_path)
        # The full reconciliation and snapshot history (~650 KB live) belong to
        # /api/score; the overview polls and only needs the summary fields.
        return dict(counts=counts,latest_collection=latest,traffic=request_traffic(connection),
                    score=score,threshold=threshold,mode=mode,
                    last_observed=connection.execute('SELECT MAX(ctn_timestamp(observed_at)) FROM counter_snapshots').fetchone()[0],
                    quality=dict(counter_conflicts=connection.execute('SELECT COUNT(*) FROM counter_conflicts').fetchone()[0],
                                 # counter_sampling is kept by triggers; has_sample matches
                                 # EXISTS(counter_snapshots) without scanning every post.
                                 posts_without_counters=connection.execute('SELECT COUNT(*) FROM counter_sampling WHERE has_sample=0').fetchone()[0],
                                 unknown_accounts=connection.execute('SELECT COUNT(*) FROM accounts a LEFT JOIN team_accounts t ON a.account_id=t.account_id WHERE t.account_id IS NULL').fetchone()[0]))
    finally:
        connection.close()


EXPORTS = {
 'posts':'SELECT * FROM posts ORDER BY post_id',
 'counters':'SELECT * FROM counter_snapshots ORDER BY post_id,ctn_timestamp(observed_at),id',
 'accounts':'SELECT a.*,t.team_label,t.is_own,t.evidence FROM accounts a LEFT JOIN team_accounts t ON a.account_id=t.account_id',
 'activity':'SELECT * FROM official_activity ORDER BY occurred_at ASC NULLS FIRST,id',
 'api_logs':'SELECT * FROM api_call_logs ORDER BY id',
 'news':'SELECT * FROM news_articles ORDER BY published_at ASC NULLS FIRST',
 'analysis':'SELECT * FROM analysis_runs ORDER BY id',
}
