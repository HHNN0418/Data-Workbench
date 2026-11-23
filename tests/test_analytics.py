from contextlib import closing, contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import nullcontext
from zoneinfo import ZoneInfoNotFoundError

from ctn_data.analytics import analyze
from ctn_data.db import connect, initialize


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        initialize(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def post(self, post_id, author=1, body="real test text", created="2026-10-19T07:00:00Z", reply=None, repost=None):
        with closing(connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen,kind,reply_to_post_id,repost_of_post_id) VALUES(?,?,?,?,?,?,?,?,?)",
                         (post_id, author, body, created, created or "2026-10-19T07:00:00Z", "2026-10-19T08:00:00Z", "post", reply, repost))

    def counters(self, post_id, at, likes=0, replies=0, reposts=0, views=0):
        with closing(connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO counter_snapshots(post_id,observed_at,like_count,reply_count,repost_count,view_count) VALUES(?,?,?,?,?,?)",
                         (post_id, at, likes, replies, reposts, views))

    def test_empty_returns_empty_data_and_unknown_correlations(self):
        result = analyze(self.path)
        for key in ("timeseries", "cohorts", "growth", "keywords"):
            self.assertEqual(result[key], [])
        self.assertEqual(result["summary"]["posts"], 0)
        self.assertIsNone(result["correlation"][0]["pearson_r"])
        self.assertIn("没有真实帖子", result["caveats"][0])

    def test_cohorts_are_limited_but_totals_cover_every_author(self):
        for post_id, author in ((1, 7), (2, 7), (3, 8), (4, 9)):
            self.post(post_id, author=author)
        result = analyze(self.path, {"cohort_limit": 2})
        self.assertEqual([row["author_id"] for row in result["cohorts"]], [7, 8])
        self.assertEqual(result["cohorts_total"], 3)
        self.assertTrue(result["cohorts_truncated"])
        self.assertEqual(result["summary"]["accounts_in_scope"], 3)
        full = analyze(self.path, {"include_exportable": True, "cohort_limit": 2})
        self.assertEqual(len(full["exportable"]["cohorts"]), 3)
        self.assertFalse(analyze(self.path)["cohorts_truncated"])
        with self.assertRaises(ValueError):
            analyze(self.path, {"cohort_limit": 1.5})

    def test_same_window_requires_early_baseline_and_endpoint(self):
        for post_id in (1, 2, 3):
            self.post(post_id)
        self.counters(1, "2026-10-19T07:05:00Z", likes=1, views=10)
        self.counters(1, "2026-10-19T08:05:00Z", likes=4, views=50)
        self.counters(2, "2026-10-19T08:00:00Z")
        self.counters(2, "2026-10-19T09:00:00Z", likes=50)
        self.counters(3, "2026-10-19T07:05:00Z")
        result = analyze(self.path)
        self.assertEqual(len(result["growth"]), 1)
        self.assertEqual(result["growth"][0]["deltas"]["like_count"], 3)
        self.assertEqual(result["growth"][0]["elapsed_minutes"], 60)
        self.assertEqual(result["quality"]["growth_exclusions"], {"no_early_baseline": 1, "no_endpoint_in_tolerance": 1})

    def test_missing_values_are_not_zero(self):
        self.post(1)
        self.counters(1, "2026-10-19T07:00:00Z", likes=None, views=None)
        self.counters(1, "2026-10-19T08:00:00Z", likes=3, views=None)
        result = analyze(self.path)
        self.assertIsNone(result["growth"][0]["deltas"]["like_count"])
        self.assertIsNone(result["growth"][0]["interaction_delta"])
        self.assertIsNone(result["cohorts"][0]["latest_observed_totals"]["view_count"])
        self.assertEqual(result["cohorts"][0]["metric_coverage_posts"]["view_count"], 0)
        self.assertEqual(result["quality"]["missing_metric_values"]["view_count"], 2)

    def test_correlation_n_and_direction_use_window_growth(self):
        for post_id in range(1, 4):
            self.post(post_id, body="x" * (post_id * 10))
            self.counters(post_id, "2026-10-19T07:00:00Z")
            self.counters(post_id, "2026-10-19T08:00:00Z", likes=post_id, views=post_id * 100)
        result = analyze(self.path)
        self.assertEqual(result["correlation"][0]["n"], 3)
        self.assertEqual(result["correlation"][0]["pearson_r"], 1.0)
        self.assertEqual(result["correlation"][1]["pearson_r"], 1.0)

    def test_decreases_preserved_and_excluded_from_correlation(self):
        self.post(1)
        self.counters(1, "2026-10-19T07:00:00Z", likes=5)
        self.counters(1, "2026-10-19T08:00:00Z", likes=2)
        result = analyze(self.path)
        self.assertEqual(result["growth"][0]["deltas"]["like_count"], -3)
        self.assertTrue(result["growth"][0]["has_negative_delta"])
        self.assertEqual(result["quality"]["negative_counter_transitions"], 1)
        self.assertEqual(result["correlation"][0]["n"], 0)
        self.assertEqual(result["timeseries"][1]["counter_deltas"]["like_count"], -3)

    def test_relations_keep_unsampled_parent_unknown(self):
        self.post(1, author=10)
        self.post(2, author=20, reply=1, repost=999)
        result = analyze(self.path)
        self.assertEqual(result["network"]["total_edges"], 2)
        self.assertEqual(result["network"]["account_edges"][0]["target_author_id"], "10")
        self.assertIsNone(result["network"]["edges"][1]["target_author_id"])
        self.assertEqual(result["quality"]["relations_outside_sample"], 1)

    def test_own_filter_requires_explicit_evidence(self):
        self.post(1, author=1)
        self.post(2, author=2)
        with closing(connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO team_accounts VALUES(?,?,?,?,?)", (1, "Agent Zero", 1, "member supplied ID", "2026-10-19T07:00:00Z"))
        result = analyze(self.path, {"own": "own"})
        self.assertEqual(result["summary"]["posts"], 1)
        self.assertEqual(result["quality"]["unknown_team_posts"], 0)
        self.assertEqual(analyze(self.path, {"own": "unknown"})["summary"]["posts"], 1)

    def test_keyword_counts_and_document_frequency(self):
        self.post(1, body="The apples apples and pears https://example.com/tracking")
        self.post(2, body="APPLES pears")
        words = {row["term"]: row for row in analyze(self.path)["keywords"]}
        self.assertEqual(words["apples"], {"term": "apples", "count": 3, "post_count": 2})
        self.assertNotIn("the", words)
        self.assertNotIn("tracking", words)

    def test_time_series_uses_deltas_not_sum_of_cumulative_counts(self):
        self.post(1)
        self.counters(1, "2026-10-19T07:00:00Z", likes=10)
        self.counters(1, "2026-10-19T07:30:00Z", likes=20)
        self.counters(1, "2026-10-19T08:00:00Z", likes=25)
        result = analyze(self.path, {"as_of": "2026-10-21T08:00:00Z"})
        self.assertEqual(sum(row["counter_deltas"]["like_count"] or 0 for row in result["timeseries"]), 15)
        self.assertEqual(result["quality"]["stale_counter_posts_24h"], 1)

    def test_invalid_parameters_are_rejected(self):
        for params in ({"window_minutes": 0}, {"window_minutes": "NaN"}, {"bucket": "minute"},
                       {"own": "npc"}, {"tolerance_minutes": 60}, {"since": "bad"}, {"bucket_timezone": "Europe/Paris"}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                analyze(self.path, params)

    def test_daily_buckets_default_to_sydney_midnight_and_allow_utc(self):
        self.post(1, created="2026-10-19T12:59:59Z")
        self.post(2, created="2026-10-19T13:00:00Z")
        result = analyze(self.path, {"bucket": "day"})
        self.assertEqual([row["bucket_at"] for row in result["timeseries"]],
                         ["2026-10-18T13:00:00.000000Z", "2026-10-19T13:00:00.000000Z"])
        self.assertTrue(all(row["timezone"] == "Australia/Sydney" for row in result["timeseries"]))
        result = analyze(self.path, {"bucket": "day", "bucket_timezone": "UTC"})
        self.assertEqual(len(result["timeseries"]), 1)
        self.assertEqual(result["timeseries"][0]["bucket_at"], "2026-10-19T00:00:00.000000Z")
        self.assertEqual(result["timeseries"][0]["timezone"], "UTC")

    def test_spring_dst_day_uses_midnight_offset_and_23_hour_interval(self):
        self.post(1, created="2026-10-03T14:30:00Z")  # Oct 4, 00:30 AEST
        self.post(2, created="2026-10-04T12:30:00Z")  # Oct 4, 23:30 AEDT
        self.post(3, created="2026-10-04T13:00:00Z")  # Oct 5, 00:00 AEDT
        for fallback in (False, True):
            context = patch("ctn_data.scoring.ZoneInfo", side_effect=ZoneInfoNotFoundError) if fallback else nullcontext()
            with context:
                result = analyze(self.path, {"bucket": "day"})
            self.assertEqual([row["bucket_at"] for row in result["timeseries"]],
                             ["2026-10-03T14:00:00.000000Z", "2026-10-04T13:00:00.000000Z"])
            self.assertEqual([row["posts_published"] for row in result["timeseries"]], [2, 1])

    def test_autumn_dst_day_is_25_hours_and_repeated_hour_stays_distinct(self):
        self.post(1, created="2026-04-04T13:30:00Z")  # Apr 5, 00:30 AEDT
        self.post(2, created="2026-04-05T13:30:00Z")  # Apr 5, 23:30 AEST
        self.post(3, created="2026-04-05T14:00:00Z")  # Apr 6, 00:00 AEST
        self.post(4, created="2026-04-04T15:30:00Z")  # First 02:30, AEDT
        self.post(5, created="2026-04-04T16:30:00Z")  # Second 02:30, AEST
        for fallback in (False, True):
            context = patch("ctn_data.scoring.ZoneInfo", side_effect=ZoneInfoNotFoundError) if fallback else nullcontext()
            with context:
                daily = analyze(self.path, {"bucket": "day"})
                hourly = analyze(self.path, {"bucket": "hour"})
            self.assertEqual([row["bucket_at"] for row in daily["timeseries"]],
                             ["2026-04-04T13:00:00.000000Z", "2026-04-05T14:00:00.000000Z"])
            hours = {row["bucket_at"] for row in hourly["timeseries"]}
            self.assertIn("2026-04-04T15:00:00.000000Z", hours)
            self.assertIn("2026-04-04T16:00:00.000000Z", hours)

    def test_detail_limit_does_not_change_full_sample_statistics(self):
        for post_id in range(1, 6):
            self.post(post_id, body="x" * (post_id * 10), reply=999)
            self.counters(post_id, "2026-10-19T07:00:00Z")
            self.counters(post_id, "2026-10-19T08:00:00Z", likes=post_id, views=post_id * 100)
        result = analyze(self.path, {"detail_limit": 1})
        self.assertEqual(len(result["growth"]), 1)
        self.assertTrue(result["growth_truncated"])
        self.assertEqual(result["growth_total"], 5)
        self.assertEqual(result["summary"]["counter_snapshots"], 10)
        self.assertEqual(result["cohorts"][0]["window_sample_count"], 5)
        self.assertEqual(result["cohorts"][0]["median_window_interactions"], 3)
        self.assertEqual(result["correlation"][0]["n"], 5)
        self.assertEqual(result["network"]["total_edges"], 5)
        self.assertEqual(len(result["network"]["edges"]), 1)
        self.assertTrue(result["network"]["truncated"])
        self.assertEqual(result["quality"]["max_post_snapshots_buffered"], 2)

    def test_filtered_snapshot_query_never_materializes_other_accounts(self):
        self.post(1, author=1)
        self.post(2, author=2)
        self.counters(1, "2026-10-19T07:00:00Z")
        self.counters(2, "2026-10-19T07:00:00Z")
        original_number = __import__("ctn_data.analytics", fromlist=["_number"])._number
        seen_values = []
        def record_number(value):
            seen_values.append(value)
            return original_number(value)
        # Make the other account identifiable without injecting mock rows.
        self.counters(2, "2026-10-19T08:00:00Z", likes=777)
        with patch("ctn_data.analytics._number", side_effect=record_number):
            result = analyze(self.path, {"author_id": 1})
        self.assertEqual(result["summary"]["counter_snapshots"], 1)
        self.assertNotIn(777, seen_values)
        self.assertEqual(result["quality"]["snapshot_processing"], "stream_by_post")

    def test_snapshot_stream_is_closed_before_other_analysis_sections(self):
        self.post(1)
        self.counters(1, "2026-10-19T07:00:00Z")
        self.counters(1, "2026-10-19T08:00:00Z", likes=4)
        from ctn_data.db import iter_rows
        events = []

        @contextmanager
        def observed_stream(conn, sql, parameters):
            self.assertIn("counter_snapshots", sql)
            events.append("open")
            try:
                with iter_rows(conn, sql, parameters) as cursor:
                    yield cursor
            finally:
                events.append("close")

        def after_snapshots(*args, **kwargs):
            self.assertEqual(events, ["open", "close"])
            return {}

        with (patch("ctn_data.analytics.iter_rows", side_effect=observed_stream),
              patch("ctn_data.analytics.trending_analysis", side_effect=after_snapshots)):
            result = analyze(self.path)
        self.assertEqual(result["summary"]["counter_snapshots"], 2)
        self.assertEqual(result["growth"][0]["interaction_delta"], 4)

    def test_selective_details_preserve_all_aggregate_values(self):
        for post_id in range(1, 6):
            self.post(post_id, body="x" * (post_id * 10), reply=999)
            self.counters(post_id, "2026-10-19T07:00:00Z")
            self.counters(post_id, "2026-10-19T08:00:00Z", likes=post_id, views=post_id * 100)
        params = {"as_of": "2026-10-19T12:00:00Z", "detail_limit": 1}
        complete = analyze(self.path, params)
        with (patch("ctn_data.analytics.SamplingAnalysis", side_effect=AssertionError("unrequested sampling")),
              patch("ctn_data.analytics.trending_analysis", side_effect=AssertionError("unrequested trending")),
              patch("ctn_data.analytics.reply_analysis", side_effect=AssertionError("unrequested replies"))):
            compact = analyze(self.path, {**params, "sections": "core", "include_exportable": "false"})
        for field in ("summary", "timeseries", "cohorts", "growth_total", "growth_truncated",
                      "correlation", "keywords", "quality", "caveats"):
            self.assertEqual(compact[field], complete[field], field)
        for field in ("exportable", "growth", "network", "trending", "replies", "sampling"):
            self.assertNotIn(field, compact)
        growth = analyze(self.path, {**params, "sections": "growth", "include_exportable": False})
        self.assertEqual(growth["growth"], complete["growth"])
        self.assertEqual(growth["included_sections"], ["core", "growth"])

    def test_time_bucket_cache_reuses_hours_without_changing_dst_semantics(self):
        for post_id in range(1, 20):
            self.post(post_id, created=f"2026-10-19T07:{post_id:02d}:00Z")
            self.counters(post_id, f"2026-10-19T07:{post_id:02d}:01Z")
        from ctn_data.analytics import _bucket_start
        with patch("ctn_data.analytics._bucket_start", wraps=_bucket_start) as bucket:
            result = analyze(self.path, {"as_of": "2026-10-19T12:00:00Z"})
        self.assertEqual(bucket.call_count, 1)
        self.assertEqual(result["timeseries"][0]["posts_published"], 19)
        self.assertEqual(result["timeseries"][0]["snapshots"], 19)

    def test_selective_payload_validates_options_and_retains_legacy_export(self):
        self.assertIn("exportable", analyze(self.path))
        for params in ({"sections": ""}, {"sections": "all,core"}, {"sections": []},
                       {"sections": "unknown"}, {"include_exportable": "maybe"}, {"include_exportable": 7}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                analyze(self.path, params)


if __name__ == "__main__":
    unittest.main()

