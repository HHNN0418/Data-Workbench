"""Incremental analysis reuses unchanged posts and equals a full recomputation."""
from contextlib import closing
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ctn_data import analytics
from ctn_data.analytics import PostFactsCache, analyze
from ctn_data.db import connect, initialize

PARAMETER_SETS = (
    {"as_of": "2026-10-19T12:00:00Z"},
    {"as_of": "2026-10-19T12:00:00Z", "window_minutes": 30, "bucket": "day", "detail_limit": 2},
    {"as_of": "2026-10-19T12:00:00Z", "author_id": 1, "sample_status": "eligible"},
    {"as_of": "2026-10-20T12:00:00Z", "since": "2026-10-19T07:30:00Z", "focus_post_id": 4},
)


def comparable(result):
    return json.dumps({key: value for key, value in result.items() if key != "generated_at"}, sort_keys=True, default=str)


class IncrementalAnalysisTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "analysis.sqlite3"
        initialize(self.path)
        for post_id in range(1, 9):
            self.post(post_id, author=1 + post_id % 3, created=f"2026-10-19T07:0{post_id}:00Z")
            self.counters(post_id, f"2026-10-19T07:0{post_id}:30Z", likes=1, views=10)
            if post_id % 2:
                self.counters(post_id, f"2026-10-19T08:0{post_id}:30Z", likes=post_id, views=10 * post_id)
        self.counters(3, "2026-10-19T09:00:00Z", likes=0, views=5)  # a negative transition
        self.post(20, created=None)  # no snapshots, no date

    def post(self, post_id, author=1, body="real test text", created="2026-10-19T07:00:00Z"):
        with closing(connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen,kind) VALUES(?,?,?,?,?,?,?)",
                         (post_id, author, body, created, created or "2026-10-19T07:00:00Z", "2026-10-19T08:00:00Z", "post"))

    def counters(self, post_id, at, likes=0, views=0):
        with closing(connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO counter_snapshots(post_id,observed_at,like_count,reply_count,repost_count,view_count) VALUES(?,?,?,0,0,?)",
                         (post_id, at, likes, views))

    def check_all(self, cache):
        for params in PARAMETER_SETS:
            with self.subTest(params=params):
                self.assertEqual(comparable(analyze(self.path, dict(params), facts_cache=cache)),
                                 comparable(analyze(self.path, dict(params))))

    def test_matches_full_recomputation_before_and_after_changes(self):
        cache = PostFactsCache(max_parameter_sets=8)
        self.check_all(cache)
        self.check_all(cache)  # warm cache
        self.counters(2, "2026-10-19T08:02:30Z", likes=9, views=90)    # new snapshot
        with closing(connect(self.path)) as conn, conn:                # edited body
            conn.execute("UPDATE posts SET body='edited text with more words' WHERE post_id=5")
        self.post(9, created="2026-10-19T07:09:00Z")                     # new post
        self.counters(9, "2026-10-19T07:09:30Z", likes=2, views=20)
        self.check_all(cache)

    def test_only_changed_posts_are_recomputed(self):
        cache = PostFactsCache()
        params = PARAMETER_SETS[0]
        analyze(self.path, dict(params), facts_cache=cache)
        self.counters(2, "2026-10-19T08:02:30Z", likes=9, views=90)
        computed = []
        original = analytics._PostFacts.__init__
        def record(facts, post, records, **kwargs):
            computed.append(post["post_id"])
            original(facts, post, records, **kwargs)
        with patch.object(analytics._PostFacts, "__init__", record):
            analyze(self.path, dict(params), facts_cache=cache)
        self.assertEqual(computed, [2])


if __name__ == "__main__":
    unittest.main()
