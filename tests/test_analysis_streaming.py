"""Memory regression for long ranking histories, independent of HTTP or services."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import gc
from pathlib import Path
import tempfile
import tracemalloc
import unittest

from ctn_data.analysis_explore import trending_analysis
from ctn_data.db import connect, initialize


class TrendingStreamingTests(unittest.TestCase):
    def test_one_long_history_retains_only_requested_detail_points(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "streaming.sqlite3"
            initialize(path)
            start = datetime(2026, 10, 1, tzinfo=timezone.utc)
            posts = [{"post_id": 1, "author_id": 1, "username": "synthetic",
                      "body": "memory regression fixture", "created_at": start.isoformat()}]
            count = 20000
            with closing(connect(path)) as conn:
                with conn:
                    conn.execute("INSERT INTO posts(post_id,first_seen,last_seen) VALUES(1,?,?)",
                                 (start.isoformat(), start.isoformat()))
                    conn.executemany("""INSERT INTO feed_observations
                        (post_id,source,observed_at,rank_position) VALUES(?,?,?,?)""",
                        ((1, "trending", (start + timedelta(seconds=index)).isoformat(), index % 100 + 1)
                         for index in range(count)))
                gc.collect()
                tracemalloc.start()
                try:
                    result = trending_analysis(conn, posts, 10)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
            row = result["trajectories"][0]
            self.assertEqual(result["summary"]["observations"], count)
            self.assertEqual(row["points_total"], count)
            self.assertEqual(len(row["points"]), 10)
            self.assertEqual(row["best_rank"], 1)
            self.assertEqual(row["last_rank"], 100)
            self.assertTrue(row["points_truncated"])
            # The old full-history dictionaries used about 10 MiB here. Leave
            # ample interpreter variation without allowing O(history) storage.
            self.assertLess(peak, 4 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
