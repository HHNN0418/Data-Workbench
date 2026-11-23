"""Coverage is relative to known local records, never an invented platform total."""
import json
import math
from datetime import datetime, timezone

METRICS = ('like_count', 'reply_count', 'repost_count', 'view_count')


def changes(samples):
    previous = None
    result = []
    for sample in samples:
        item = dict(sample)
        delta = {k: None for k in METRICS}
        elapsed = None
        if previous:
            elapsed = (datetime.fromisoformat(item['observed_at'].replace('Z','+00:00')) -
                       datetime.fromisoformat(previous['observed_at'].replace('Z','+00:00'))).total_seconds()/60
            if elapsed > 0:
                delta = {k: item[k]-previous[k] if item[k] is not None and previous[k] is not None else None for k in METRICS}
        item.update(deltas=delta, elapsed_minutes=round(elapsed, 2) if elapsed is not None else None,
                    has_negative_delta=any(v is not None and v < 0 for v in delta.values()))
        result.append(item)
        previous = item
    return result


def coverage(conn, config, runs, *, now=None):
    now = now or datetime.now(timezone.utc)
    counters = dict(conn.execute('''SELECT COUNT(*) total,
        SUM(CASE WHEN c.id IS NOT NULL THEN 1 ELSE 0 END) measured,
        SUM(CASE WHEN c.id IS NOT NULL AND c.like_count IS NOT NULL AND c.reply_count IS NOT NULL AND c.repost_count IS NOT NULL AND c.view_count IS NOT NULL THEN 1 ELSE 0 END) complete,
        SUM(CASE WHEN c.id IS NOT NULL AND julianday(c.observed_at)<julianday('now','-15 minutes') THEN 1 ELSE 0 END) stale,
        SUM(CASE WHEN c.id IS NULL AND json_extract(q.value,'$.status')='failed' THEN 1 ELSE 0 END) failed,
        SUM(CASE WHEN EXISTS(SELECT 1 FROM counter_snapshots s WHERE s.post_id=p.post_id LIMIT 1 OFFSET 1) THEN 1 ELSE 0 END) repeated
        FROM posts p LEFT JOIN counter_snapshots c ON c.id=(SELECT id FROM counter_snapshots WHERE post_id=p.post_id AND ctn_timestamp(observed_at) IS NOT NULL ORDER BY ctn_timestamp(observed_at) DESC,id DESC LIMIT 1)
        LEFT JOIN checkpoints q ON q.key='counter_attempt:' || p.post_id''').fetchone())
    counters = {k: v or 0 for k,v in counters.items()}
    counters.update(missing=counters['total']-counters['measured'], partial=counters['measured']-counters['complete'])
    refs = dict(conn.execute('''WITH targets AS (
        SELECT repost_of_post_id target FROM posts WHERE repost_of_post_id IS NOT NULL
        UNION SELECT reply_to_post_id FROM posts WHERE reply_to_post_id IS NOT NULL
        UNION SELECT root_post_id FROM posts WHERE root_post_id IS NOT NULL)
        SELECT COUNT(*) total,SUM(CASE WHEN p.post_id IS NULL THEN 1 ELSE 0 END) missing,
        SUM(CASE WHEN p.post_id IS NULL AND json_extract(q.value,'$.status')='unavailable' THEN 1 ELSE 0 END) unavailable
        FROM targets LEFT JOIN posts p ON p.post_id=target LEFT JOIN checkpoints q ON q.key='relation:' || target''').fetchone())
    refs = {k:v or 0 for k,v in refs.items()}
    feeds = {}
    for key in ('feed:latest','feed:latest:live','feed:trending'):
        row = conn.execute('SELECT value,updated_at FROM checkpoints WHERE key=?',(key,)).fetchone()
        feeds[key] = {**json.loads(row['value']), 'updated_at':row['updated_at']} if row else {}
        feeds[key].pop('cursor', None)
        feeds[key]['pending'] = bool(json.loads(row['value']).get('cursor')) if row else False
    bounds = dict(conn.execute("SELECT MIN(p.created_at) oldest,MAX(p.created_at) newest,COUNT(*) posts FROM posts p WHERE EXISTS(SELECT 1 FROM feed_observations f WHERE f.post_id=p.post_id AND f.source IN ('latest','browser_latest'))").fetchone())
    # Count newly covered post IDs, not requests or legacy whole-run metrics.
    # A repeated observation of an already measured post never advances this
    # rate. The fixed wall-clock window includes cooldowns and paused time.
    window_minutes = 15
    at = now.isoformat()
    first_samples = conn.execute('''WITH firsts AS MATERIALIZED (
        SELECT (SELECT julianday(observed_at) FROM counter_snapshots s WHERE s.post_id=p.post_id
          AND ctn_timestamp(observed_at) IS NOT NULL
          ORDER BY ctn_timestamp(observed_at),id LIMIT 1) AS first_observed FROM posts p)
        SELECT COUNT(*) FROM firsts
        WHERE first_observed>julianday(?)-?/1440.0 AND first_observed<=julianday(?)''',
        (at,window_minutes,at)).fetchone()[0]
    rate = first_samples/window_minutes
    counters['first_sample_window_minutes'] = window_minutes
    counters['first_samples_in_window'] = first_samples
    counters['first_samples_per_minute'] = round(rate,1) if rate else None
    counters['backlog_minutes'] = math.ceil(counters['missing']/rate) if rate else None
    authors = conn.execute('SELECT COUNT(DISTINCT p.author_id) FROM posts p LEFT JOIN accounts a ON a.account_id=p.author_id WHERE p.author_id IS NOT NULL AND a.account_id IS NULL').fetchone()[0]
    active = next((r for r in runs if r['status']=='running'), None)
    live = None
    if active:
        live = dict(conn.execute('SELECT COUNT(*) requests,SUM(CASE WHEN status=429 THEN 1 ELSE 0 END) throttled,SUM(CASE WHEN status>=400 THEN 1 ELSE 0 END) errors FROM api_call_logs WHERE occurred_at>=?',(active['started_at'],)).fetchone())
    return dict(counters=counters,references=refs,feeds=feeds,latest=bounds,missing_authors=authors,
                history_eta_minutes=None,history_total=None,live=live,updated_at=now.isoformat())
