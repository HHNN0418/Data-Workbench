"""Synthetic planner benchmark; never opens a configured or production database.

Run: python -m tests.benchmark_counter_planner --posts 1000 10000 --samples 100
The retained pre-change SQL is a comparison fixture, not a production fallback.
"""
import argparse
import json
import time

from ctn_data.collector import _config, _counter_windows, _standard_counter_schedule
from ctn_data.db import SCHEMA, connect


def previous_queue_queries(conn, at, limit):
    rows = []
    for condition in ('=0', '>0'):
        rows.extend(conn.execute('''SELECT p.post_id,
            CASE WHEN COALESCE(q.updated_at,p.first_seen)>COALESCE(MAX(c.observed_at),p.first_seen)
                THEN COALESCE(q.updated_at,p.first_seen) ELSE COALESCE(MAX(c.observed_at),p.first_seen) END AS last_attempt
            FROM posts p LEFT JOIN counter_snapshots c ON c.post_id=p.post_id
            LEFT JOIN checkpoints q ON q.key='counter_attempt:' || p.post_id
            WHERE NOT (COALESCE(json_valid(q.value),FALSE) AND
              COALESCE(CASE WHEN json_valid(q.value) THEN json_extract(q.value,'$.status') ELSE NULL END='failed',FALSE)
              AND COALESCE(julianday(q.updated_at)>julianday(?)-5.0/1440,FALSE))
            AND NOT EXISTS(SELECT 1 FROM counter_snapshots fresh JOIN raw_records raw ON raw.id=fresh.raw_id
                WHERE fresh.post_id=p.post_id AND raw.source='browser:counters' AND julianday(fresh.observed_at)>julianday(?)-1.0/1440)
            AND NOT EXISTS(SELECT 1 FROM checkpoints failed WHERE failed.key='counter_attempt:' || p.post_id
                AND CAST(CASE WHEN json_valid(failed.value) THEN json_extract(failed.value,'$.http_status') ELSE NULL END AS INTEGER) IN (403,404,410)
                AND julianday(failed.updated_at)>julianday(?)-15.0/1440)
            GROUP BY p.post_id,p.first_seen,q.updated_at HAVING COUNT(c.id)''' + condition + '''
            ORDER BY last_attempt,p.post_id DESC LIMIT ?''', (at, at, at, limit)).fetchall())
    return rows


def benchmark(posts, samples):
    conn = connect(':memory:')
    try:
        conn.executescript(SCHEMA)
        at, created = '2026-10-20T01:00:00Z', '2026-10-19T23:00:00Z'
        with conn:
            conn.executemany('INSERT INTO posts(post_id,created_at,first_seen,last_seen) VALUES(?,?,?,?)',
                             ((pid, created, created, created) for pid in range(1, posts + 1)))
            conn.executemany('INSERT INTO counter_snapshots(post_id,observed_at,like_count) VALUES(?,?,1)',
                             ((pid, f'2026-10-20T00:{sample // 60:02d}:{sample % 60:02d}Z')
                              for pid in range(1, posts + 1) for sample in range(samples)))
        started = time.perf_counter()
        previous = previous_queue_queries(conn, at, 30)
        before = time.perf_counter() - started
        started = time.perf_counter()
        current = _standard_counter_schedule(conn, _config({'counter_limit': 30}), now=at)
        after = time.perf_counter() - started
        assert [row['post_id'] for row in previous] == [row['post_id'] for row in current]
        started = time.perf_counter()
        _counter_windows(conn, 30, at)
        windows = time.perf_counter() - started
        return {'posts': posts, 'snapshots': posts * samples, 'before_seconds': round(before, 4),
                'after_seconds': round(after, 4), 'speedup': round(before / after, 1),
                'window_seconds': round(windows, 4), 'same_selection': True}
    finally:
        conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--posts', type=int, nargs='+', default=[1000, 10000])
    parser.add_argument('--samples', type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.samples <= 3600 or any(not 1 <= count <= 100000 for count in args.posts):
        parser.error('samples must be 1..3600; posts must be 1..100000')
    for count in args.posts:
        print(json.dumps(benchmark(count, args.samples)), flush=True)
