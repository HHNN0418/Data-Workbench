from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from ctn_data.analytics import analyze
from ctn_data.db import connect, initialize


class AnalysisExploreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "explore.sqlite3"
        initialize(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def post(self, post_id, *, author=1, parent=None, created="2026-10-19T07:00:00Z"):
        with closing(connect(self.path)) as conn, conn:
            conn.execute("""INSERT INTO posts(post_id,author_id,reply_to_post_id,body,kind,created_at,first_seen,last_seen)
                            VALUES(?,?,?,?,?,?,?,?)""",
                         (post_id, author, parent, f"Observed post {post_id}", "reply" if parent else "post",
                          created, "2026-10-19T07:00:00Z", "2026-10-19T09:00:00Z"))

    def counters(self, post_id, at, *, likes=0, replies=0, reposts=0, views=10):
        with closing(connect(self.path)) as conn, conn:
            conn.execute("""INSERT INTO counter_snapshots(post_id,observed_at,like_count,reply_count,repost_count,view_count)
                            VALUES(?,?,?,?,?,?)""", (post_id, at, likes, replies, reposts, views))

    def feed(self, post_id, at, rank, *, source="trending", hot_score=10):
        with closing(connect(self.path)) as conn, conn:
            conn.execute("""INSERT INTO feed_observations(post_id,source,observed_at,rank_position,global_hot_score)
                            VALUES(?,?,?,?,?)""", (post_id, source, at, rank, hot_score))

    def test_empty_exploration_has_explicit_zero_counts(self):
        result = analyze(self.path)
        self.assertEqual(result["trending"]["summary"]["posts"], 0)
        self.assertEqual(result["trending"]["trajectories"], [])
        self.assertEqual(result["replies"]["nodes"], [])
        self.assertIsNone(result["replies"]["selected_root_id"])
        self.assertEqual(result["sampling"]["posts_total"], 0)
        self.assertTrue(all(row["count"] == 0 for row in result["sampling"]["statuses"]))

    def test_coverage_uses_mature_opportunities_and_keeps_aggregates_under_filters(self):
        self.post(1)
        self.counters(1, '2026-10-19T07:02:00Z')
        self.counters(1, '2026-10-19T07:33:00Z', likes=3)
        self.counters(1, '2026-10-19T08:03:00Z', likes=6)
        self.post(2, created='2026-10-19T08:00:00Z')
        self.counters(2, '2026-10-19T08:01:00Z')
        self.post(3)  # Mature missing baseline belongs in the denominator.
        params = {'as_of': '2026-10-19T08:25:00Z', 'bucket_timezone': 'UTC'}
        full = analyze(self.path, params)['sampling']
        cells = {(r['batch'], r['minutes']): r for r in full['coverage']['cells']}
        early = cells[('2026-10-19T07:00:00.000000Z', 60)]
        self.assertEqual((early['eligible'], early['mature'], early['pending']), (1, 2, 0))
        self.assertEqual(early['missing_baseline'], 1)
        self.assertEqual(early['rate'], .5)
        later = cells[('2026-10-19T08:00:00.000000Z', 30)]
        self.assertEqual((later['mature'], later['pending']), (0, 1))
        self.assertIsNone(later['rate'])
        self.assertEqual(cells[('2026-10-19T07:00:00.000000Z', 180)]['mature'], 0)
        filtered = analyze(self.path, {**params, 'sample_batch': '2026-10-19T08:00:00Z', 'detail_limit': 1})['sampling']
        self.assertEqual(filtered['coverage'], full['coverage'])
        self.assertEqual(filtered['stages'], full['stages'])
        self.assertEqual([p['post_id'] for p in filtered['posts']], [2])
        self.assertEqual(full['coverage']['endpoint_offsets'], [{'minutes': 1, 'count': 1}])

    def test_coverage_excludes_incomplete_and_negative_windows(self):
        for post_id in (1, 2):
            self.post(post_id)
            self.counters(post_id, '2026-10-19T07:00:00Z', likes=10)
        self.counters(1, '2026-10-19T07:30:00Z', likes=9)
        self.counters(2, '2026-10-19T07:30:00Z', likes=11, replies=None)
        result = analyze(self.path, {'as_of': '2026-10-19T09:00:00Z'})['sampling']
        cell = next(c for c in result['coverage']['cells'] if c['minutes'] == 30)
        self.assertEqual((cell['eligible'], cell['mature'], cell['invalid_metrics']), (0, 2, 2))
        with self.assertRaises(ValueError):
            analyze(self.path, {'sample_batch': 'bad'})

    def test_trending_preserves_observed_gaps_normalizes_and_deduplicates(self):
        self.post(1)
        self.feed(1, "2026-10-19T08:00:00Z", 8)
        self.feed(1, "2026-10-19T19:00:00+11:00", 7)  # same instant and source: later record wins
        self.feed(1, "2026-10-19T08:00:00Z", 6, source="browser_trending")
        self.feed(1, "2026-10-19T11:00:00Z", 2, hot_score=float("inf"))
        self.feed(1, "2026-10-19T09:00:00Z", 0)
        self.feed(1, "2026-10-19T09:01:00Z", 1.5)
        self.feed(1, "bad timestamp", 1)
        self.feed(1, "2026-10-19T09:02:00Z", 1, source="latest")
        result = analyze(self.path)["trending"]
        self.assertEqual(result["summary"]["observations"], 3)
        self.assertEqual(result["summary"]["repeated_posts"], 1)
        row = result["trajectories"][0]
        self.assertEqual(row["best_rank"], 2)
        self.assertEqual(row["first_rank"], 6)
        self.assertEqual(row["last_rank"], 2)
        self.assertEqual(row["rank_change"], 4)
        self.assertEqual(row["observed_span_minutes"], 180)
        self.assertEqual([point["observed_at"] for point in row["points"]],
                         ["2026-10-19T08:00:00.000000Z", "2026-10-19T08:00:00.000000Z", "2026-10-19T11:00:00.000000Z"])
        self.assertEqual(row["points"][1]["rank"], 7)
        self.assertIsNone(row["points"][-1]["hot_score"])

    def test_focus_and_detail_limit_preserve_full_trending_aggregates(self):
        for post_id in (1, 2, 3):
            self.post(post_id, author=post_id)
            self.feed(post_id, "2026-10-19T08:00:00Z", post_id)
        self.feed(1, "2026-10-19T09:00:00Z", 1)
        full = analyze(self.path)
        focused = analyze(self.path, {"detail_limit": 1, "focus_post_id": "3"})
        self.assertEqual(focused["trending"]["summary"], full["trending"]["summary"])
        self.assertEqual(focused["trending"]["total_posts"], 3)
        self.assertTrue(focused["trending"]["truncated"])
        self.assertEqual([row["post_id"] for row in focused["trending"]["trajectories"]], [3])
        self.assertEqual([row["post_id"] for row in focused["sampling"]["posts"]], [3])
        self.assertEqual(analyze(self.path, {"author_id": 2})["trending"]["summary"]["posts"], 1)
        small = analyze(self.path, {"detail_limit": 1, "focus_post_id": 1})["trending"]["trajectories"][0]
        self.assertEqual(small["observations"], 2)
        self.assertTrue(small["points_truncated"])
        self.assertEqual(len(small["points"]), 1)

    def test_trending_uses_exact_instants_and_last_valid_id_per_source(self):
        self.post(1)
        # IDs deliberately disagree with chronological order. SQLite julianday
        # rounds these adjacent microseconds together and cannot be the sort key.
        self.feed(1, "2026-10-19T08:00:00.000002Z", 2)
        self.feed(1, "2026-10-19T08:00:00Z", 9)
        self.feed(1, "2026-10-19T08:00:00.000001Z", 4)
        self.feed(1, "2026-10-19T19:00:00.000001+11:00", 3)
        self.feed(1, "2026-10-19T08:00:00.000001+00:00", 0)  # invalid rank is ignored
        self.feed(1, "2026-10-19T08:00:00.000001Z", 5, source="browser_trending")
        result = analyze(self.path, {"sections": "trending", "include_exportable": False})["trending"]
        row = result["trajectories"][0]
        self.assertEqual(result["summary"]["observations"], 4)
        self.assertEqual([point["rank"] for point in row["points"]], [9, 5, 3, 2])
        self.assertEqual((row["first_rank"], row["last_rank"], row["best_rank"]), (9, 2, 2))
        self.assertEqual(result["summary"]["repeated_posts"], 1)

    def test_trending_uniform_points_and_summary_are_independent_of_detail_limit(self):
        self.post(1)
        for minute in reversed(range(11)):
            self.feed(1, f"2026-10-19T08:{minute:02d}:00Z", 20 - minute)
        full = analyze(self.path, {"sections": "trending", "detail_limit": 20})["trending"]
        small = analyze(self.path, {"sections": "trending", "detail_limit": 4})["trending"]
        latest = analyze(self.path, {"sections": "trending", "detail_limit": 1})["trending"]
        self.assertEqual(small["summary"], full["summary"])
        self.assertEqual(small["trajectories"][0]["points"],
                         [full["trajectories"][0]["points"][index] for index in (0, 3, 7, 10)])
        self.assertEqual(latest["trajectories"][0]["points"], [full["trajectories"][0]["points"][-1]])
        self.assertTrue(small["trajectories"][0]["points_truncated"])
        self.assertEqual(small["trajectories"][0]["points_total"], 11)

    def test_trending_legacy_timestamp_formats_keep_chronology_and_deduplication(self):
        self.post(1)
        self.feed(1, "19/10/2026 09:00:00", 2)
        self.feed(1, "2026-10-19T08:00:00Z", 5)
        self.feed(1, "Mon, 19 Oct 2026 08:00:00 +0000", 4)
        self.feed(1, "2026-10-19 10:00:00 UTC", 1)
        row = analyze(self.path, {"sections": "trending"})["trending"]["trajectories"][0]
        self.assertEqual(row["observations"], 3)
        self.assertEqual([point["rank"] for point in row["points"]], [4, 2, 1])
        self.assertEqual((row["first_rank"], row["last_rank"]), (4, 1))

    def test_trending_keeps_focus_across_query_batches_and_summary_ties(self):
        with closing(connect(self.path)) as conn, conn:
            conn.executemany("INSERT INTO posts(post_id,author_id,first_seen,last_seen) VALUES(?,?,?,?)",
                             ((i, 1, "2026-10-19T08:00:00Z", "2026-10-19T08:00:00Z") for i in range(1, 403)))
            conn.executemany("INSERT INTO feed_observations(post_id,source,observed_at,rank_position) VALUES(?,?,?,?)",
                             ((i, "trending", "2026-10-19T08:00:00Z", 1) for i in range(1, 403)))
        small = analyze(self.path, {"sections": "trending", "detail_limit": 2, "focus_post_id": 402})["trending"]
        self.assertEqual(small["summary"]["observations"], 402)
        self.assertEqual(small["summary"]["repeated_posts"], 0)
        self.assertEqual(small["total_posts"], 402)
        self.assertTrue(small["truncated"])
        self.assertEqual([row["post_id"] for row in small["trajectories"]], [1, 402])
        self.assertTrue(all(len(row["points"]) == 1 for row in small["trajectories"]))

    def test_reply_ancestors_are_context_and_missing_parents_are_explicit(self):
        self.post(1, author=10)
        self.post(2, author=20, parent=1)
        self.post(3, author=20, parent=2)
        self.post(4, author=20, parent=999)
        result = analyze(self.path, {"author_id": 20})["replies"]
        self.assertEqual(result["summary"]["reply_posts"], 3)
        self.assertEqual(result["summary"]["participants"], 1)
        self.assertEqual(result["summary"]["threads"], 2)
        self.assertEqual(result["summary"]["missing_parents"], 1)
        self.assertEqual(result["summary"]["max_depth"], 2)
        self.assertEqual(result["threads"][0]["posts"], 2)
        self.assertEqual(result["selected_root_id"], 1)
        self.assertEqual(result["total_nodes"], 3)
        self.assertFalse(result["nodes"][0]["in_scope"])
        self.assertFalse(result["nodes"][0]["missing"])
        focused = analyze(self.path, {"author_id": 20, "focus_post_id": 4})["replies"]
        self.assertEqual(focused["selected_root_id"], 999)
        placeholder = next(row for row in focused["nodes"] if row["post_id"] == 999)
        self.assertTrue(placeholder["missing"])
        self.assertFalse(placeholder["in_scope"])
        self.assertEqual(focused["summary"], result["summary"])

    def test_reply_focus_path_is_prioritized_and_overlong_paths_expose_gaps(self):
        self.post(1)
        self.post(2, parent=1)
        self.post(3, parent=1)
        self.post(4, parent=3)
        result = analyze(self.path, {"detail_limit": 3, "focus_post_id": 4})["replies"]
        self.assertEqual([row["post_id"] for row in result["nodes"]], [1, 3, 4])
        self.assertFalse(result["path_truncated"])
        self.assertTrue(result["nodes_truncated"])
        small = analyze(self.path, {"detail_limit": 2, "focus_post_id": 4})["replies"]
        self.assertEqual([row["post_id"] for row in small["nodes"]], [1, 4])
        self.assertTrue(small["path_truncated"])
        self.assertEqual(small["omitted_ancestor_count"], 1)
        self.assertTrue(small["nodes"][-1]["context_gap"])
        self.assertEqual(small["summary"], result["summary"])

    def test_reply_cycles_and_deep_chains_do_not_recurse(self):
        for post_id, parent in ((10, 11), (11, 12), (12, 10), (20, 20)):
            self.post(post_id, parent=parent)
        cycle = analyze(self.path, {"focus_post_id": 11})["replies"]
        self.assertEqual(cycle["selected_root_id"], 10)
        self.assertEqual(cycle["summary"]["cycle_breaks"], 2)
        root = next(row for row in cycle["nodes"] if row["post_id"] == 10)
        self.assertIsNone(root["parent_id"])
        self.assertEqual(root["original_parent_id"], 11)
        self.assertTrue(root["cycle_broken"])
        with closing(connect(self.path)) as conn, conn:
            conn.executemany("""INSERT INTO posts(post_id,author_id,reply_to_post_id,body,created_at,first_seen,last_seen)
                                VALUES(?,?,?,?,?,?,?)""",
                             [(post_id, 2, post_id - 1 if post_id > 1000 else None, "deep",
                               "2026-10-19T07:00:00Z", "2026-10-19T07:00:00Z", "2026-10-19T07:00:00Z")
                              for post_id in range(1000, 2200)])
        deep = analyze(self.path, {"focus_post_id": 2199, "detail_limit": 2})["replies"]
        self.assertEqual(deep["selected_root_id"], 1000)
        self.assertEqual(deep["summary"]["max_depth"], 1199)
        self.assertEqual(deep["total_nodes"], 1200)
        self.assertEqual([row["post_id"] for row in deep["nodes"]], [1000, 2199])
        with closing(connect(self.path)) as conn, conn:
            conn.execute("UPDATE posts SET author_id=3 WHERE post_id=2199")
        context = analyze(self.path, {"author_id": 3, "focus_post_id": 2199, "detail_limit": 2})["replies"]
        self.assertEqual(context["total_nodes"], 1200)
        self.assertEqual(context["summary"]["reply_posts"], 1)
        self.assertEqual(context["summary"]["participants"], 1)
        self.assertEqual(context["threads"][0]["posts"], 1)
        self.assertFalse(context["nodes"][0]["in_scope"])

    def sampling_fixture(self):
        for post_id in range(1, 9):
            self.post(post_id, created=None if post_id == 2 else (
                "2026-10-19T08:20:00Z" if post_id == 4 else "2026-10-19T07:00:00Z"))
        self.counters(2, "2026-10-19T07:00:00Z")
        self.counters(3, "2026-10-19T07:30:00Z")
        self.counters(4, "2026-10-19T08:20:00Z")
        self.counters(5, "2026-10-19T07:00:00Z")
        for post_id in (6, 7, 8):
            self.counters(post_id, "2026-10-19T07:00:00Z", likes=None if post_id == 6 else 0)
            self.counters(post_id, "2026-10-19T08:00:00Z", likes=2, views=5 if post_id == 7 else 15)

    def test_sampling_mutually_exclusive_statuses_and_monotonic_stages(self):
        self.sampling_fixture()
        result = analyze(self.path, {"as_of": "2026-10-19T09:00:00Z"})["sampling"]
        self.assertEqual({row["key"]: row["count"] for row in result["statuses"]}, {
            "no_counters": 1, "missing_created_at": 1, "no_early_baseline": 1,
            "waiting_endpoint": 1, "missed_endpoint": 1, "missing_metrics": 1, "negative_delta": 1, "eligible": 1,
        })
        self.assertEqual([row["count"] for row in result["stages"]], [8, 7, 5, 3, 2, 1])
        self.assertEqual(result["posts_total"], 8)
        self.assertEqual(len({row["post_id"] for row in result["posts"]}), 8)
        missing = next(row for row in result["posts"] if row["post_id"] == 6)
        self.assertEqual(missing["endpoint_at"], "2026-10-19T08:00:00.000000Z")

    def test_sampling_filters_change_only_details_and_focus_is_retained(self):
        self.sampling_fixture()
        params = {"as_of": "2026-10-19T09:00:00Z"}
        full = analyze(self.path, params)
        filtered = analyze(self.path, {**params, "sample_status": "eligible", "detail_limit": 1})
        self.assertEqual(filtered["sampling"]["posts_total"], 1)
        self.assertEqual(filtered["sampling"]["posts"][0]["post_id"], 8)
        for key in ("stages", "statuses"):
            self.assertEqual(filtered["sampling"][key], full["sampling"][key])
        self.assertEqual(filtered["summary"], full["summary"])
        self.assertEqual(filtered["quality"], full["quality"])
        early = analyze(self.path, {**params, "sample_stage": "early_baseline", "focus_post_id": 8, "detail_limit": 2})["sampling"]
        self.assertEqual(early["posts_total"], 5)
        self.assertEqual(early["selected_stage"], "early_baseline")
        self.assertTrue(early["truncated"])
        self.assertIn(8, [row["post_id"] for row in early["posts"]])
        self.assertTrue(all(row["baseline_at"] is not None for row in early["posts"]))
        complete = analyze(self.path, {**params, "sample_stage": "complete_metrics"})["sampling"]
        self.assertEqual({row["post_id"] for row in complete["posts"]}, {7, 8})
        wrong_focus = analyze(self.path, {**params, "sample_status": "eligible", "focus_post_id": 1})["sampling"]
        self.assertEqual([row["post_id"] for row in wrong_focus["posts"]], [8])

    def test_endpoint_deadline_distinguishes_waiting_and_missed(self):
        self.post(1)
        self.counters(1, "2026-10-19T07:00:00Z")
        waiting = analyze(self.path, {"as_of": "2026-10-19T08:12:00Z"})["sampling"]
        missed = analyze(self.path, {"as_of": "2026-10-19T08:12:00.000001Z"})["sampling"]
        self.assertEqual(waiting["posts"][0]["status"], "waiting_endpoint")
        self.assertEqual(missed["posts"][0]["status"], "missed_endpoint")

    def test_exploration_parameters_are_strictly_validated(self):
        for params in ({"focus_post_id": True}, {"focus_post_id": 0}, {"focus_post_id": -1},
                       {"focus_post_id": 1.0}, {"focus_post_id": "1.5"}, {"focus_post_id": "1 OR 1=1"},
                       {"focus_post_id": 9223372036854775808}, {"sample_status": "bad"},
                       {"sample_status": []}, {"sample_stage": "bad"}, {"sample_stage": False},
                       {"sample_status": "eligible", "sample_stage": "total"}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                analyze(self.path, params)


if __name__ == "__main__":
    unittest.main()
