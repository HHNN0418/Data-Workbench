from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from ctn_data.imports import add_score_snapshot, import_activity_csv
from ctn_data.scoring import calculate_scenario, rules_context, score_overview, score_pace, sydney_time
from tests.test_imports import export_csv


class ScoringTests(unittest.TestCase):
    def test_multiplier_at_sydney_midnight_boundaries(self):
        cases = [("2026-10-19T07:00:00Z", 0, 1), ("2026-10-25T12:59:59Z", 6, 1),
                 ("2026-10-25T13:00:00Z", 7, 2), ("2026-10-31T12:59:59Z", 12, 2),
                 ("2026-10-31T13:00:00Z", 13, 4), ("2026-11-01T13:00:00Z", 14, 4)]
        for stamp, day, multiplier in cases:
            with self.subTest(stamp=stamp):
                context = rules_context(stamp)
                self.assertEqual((context["campaign_day"], context["red_multiplier"]), (day, multiplier))

    def test_timezone_fallback_matches_2026_dst_transitions(self):
        cases = [("2026-04-04T15:59:59Z", 11), ("2026-04-04T16:00:00Z", 10),
                 ("2026-10-03T15:59:59Z", 10), ("2026-10-03T16:00:00Z", 11)]
        with patch("ctn_data.scoring.ZoneInfo", side_effect=ZoneInfoNotFoundError):
            for stamp, offset in cases:
                self.assertEqual(sydney_time(stamp).utcoffset().total_seconds(), offset * 3600)
            self.assertEqual(rules_context("2026-10-25T13:00:00Z")["red_multiplier"], 2)

    def test_red_cap_penalty_and_contribution_pool(self):
        result = calculate_scenario({"track": "red", "recorded_at": "2026-10-26T00:00:00+11:00",
                                     "engagement": {"views": 20, "views_already_paid_today": 490, "likes": 2, "unfollows": 1},
                                     "changes": [{"view": "vaccination", "direction": "towards", "decisive_count": 1,
                                                  "contributing_posts": 1, "total_contributing_posts": 4},
                                                 {"view": "vaccination", "direction": "away", "decisive_count": 1,
                                                  "contributing_posts": 2, "total_contributing_posts": 4}]})
        # engagement=(10+10-20)*2; towards=(160+20)*2; away=(-80-20)*2
        self.assertEqual(result["points_decimal"], "160")
        self.assertFalse(result["is_official"])
        self.assertEqual(result["components"][0]["paid_count"], 10)

    def test_red_missing_cap_or_pool_is_unknown(self):
        result = calculate_scenario({"track": "red", "recorded_at": "2026-11-01T00:00:00+11:00", "engagement": {"views": 50, "likes": 1}})
        self.assertIsNone(result["points_decimal"])
        self.assertEqual(result["partial_points_decimal"], "20")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(calculate_scenario({"track": "red", "public_counts": {"likes": 10}})["status"], "unknown")

    def test_non_vaccine_change_neutral_and_no_decisive_post(self):
        result = calculate_scenario({"track": "red", "recorded_at": "2026-10-19T18:00:00+11:00",
                                     "changes": [{"view": "other", "decisive_count": 0, "contributing_posts": 1, "total_contributing_posts": 4},
                                                 {"view": "vaccination", "direction": "neutral", "decisive_count": 1, "contributing_posts": 0}]})
        self.assertEqual(result["points_decimal"], "22.5")

    def test_blue_diminishing_tiers(self):
        for ordinal, expected in [(1, "25"), (5, "25"), (6, "12.5"), (10, "12.5"), (11, "2.5")]:
            result = calculate_scenario({"track": "blue", "type": "post", "ruling": "inauthentic", "report_rank": 1,
                                         "target_is_own": False, "upheld_against_target_today": ordinal})
            self.assertEqual(result["points_decimal"], expected)
        result = calculate_scenario({"track": "blue", "ruling": "inauthentic", "report_rank": 5,
                                     "target_is_own": False, "upheld_against_target_today": 11})
        self.assertEqual(result["points_decimal"], "0.5")

    def test_blue_delay_pending_and_genuine(self):
        for count, hours in [(1, "3"), (2, "1.5"), (3, "0.75"), (4, "0.375"), (5, "0.1875")]:
            result = calculate_scenario({"track": "blue", "ruling": "pending", "distinct_teams": count,
                                         "first_report_at": "2026-10-19T07:00:00Z"})
            self.assertEqual(result["ruling_after_hours"], hours)
            self.assertIsNone(result["points_decimal"])
        result = calculate_scenario({"track": "blue", "ruling": "genuine"})
        self.assertEqual((result["points_decimal"], result["report_suspension_hours"]), ("0", 6))

    def test_blue_self_and_late_reports_zero(self):
        for rank, own in [(1, True), (6, False)]:
            result = calculate_scenario({"track": "blue", "ruling": "inauthentic", "report_rank": rank, "target_is_own": own})
            self.assertEqual(result["points_decimal"], "0")

    def test_account_review_exact_threshold_and_no_retrospective_payment(self):
        payload = {"track": "blue", "type": "account", "reason": "bot_spam", "target_is_own": False,
                   "permanently_hidden_posts": 1, "actions": 100, "report_open_at_ruling": True}
        self.assertEqual(calculate_scenario(payload)["points_decimal"], "15")
        self.assertEqual(calculate_scenario({**payload, "actions": 99})["points_decimal"], "0")
        self.assertEqual(calculate_scenario({**payload, "report_open_at_ruling": False})["points_decimal"], "0")
        self.assertEqual(calculate_scenario({**payload, "permanently_hidden_posts": 0})["points_decimal"], "0")

    def test_poll_threshold_and_close_call_boundary(self):
        payload = {"track": "polling", "type": "poll", "poll_number": 1, "team_score": "1", "team_side": "no", "yes_votes": 52, "no_votes": 48}
        self.assertEqual(calculate_scenario(payload)["points_decimal"], "3000")
        self.assertEqual(calculate_scenario({**payload, "team_score": "0.999"})["points_decimal"], "0")
        self.assertEqual(calculate_scenario({**payload, "poll_number": 2})["points_decimal"], "0")
        self.assertEqual(calculate_scenario({**payload, "poll_number": 2, "team_score": "6999.99"})["points_decimal"], "0")
        self.assertEqual(calculate_scenario({**payload, "poll_number": 3, "team_score": "7000"})["points_decimal"], "3000")
        self.assertEqual(calculate_scenario({**payload, "yes_votes": 520001, "no_votes": 479999})["points_decimal"], "0")
        self.assertEqual(calculate_scenario({**payload, "team_side": "yes", "yes_votes": 53, "no_votes": 47})["points_decimal"], "6000")

    def test_rules_context_uses_revised_polling_and_ruling_schedule(self):
        context = rules_context("2026-10-21T08:00:00Z")
        polling = context["polling"]
        self.assertEqual([poll["threshold"] for poll in polling["polls"]], ["1", "7000", "7000"])
        self.assertEqual((polling["poll_win_award"], polling["close_call_award"]), ("6000", "3000"))
        self.assertEqual((polling["referendum_base"], polling["referendum_threshold"], polling["persuasion_pool"]),
                         ("20000", "7500", "100000"))
        self.assertEqual(context["blue"]["ruling_hours"], ["3", "1.5", "0.75", "0.375", "0.1875"])

    def test_referendum_weighted_pool_uses_share_not_contribution_count(self):
        result = calculate_scenario({"track": "polling", "type": "referendum", "team_side": "yes", "yes_votes": 60, "no_votes": 40,
                                     "qualifying_changes": 30, "contributions": [{"count": 40, "total_posts": 4}], "winning_side_total_weight": "50"})
        self.assertEqual(result["team_persuasion_weight"], "35")
        # Official example: 35 / 50 of the 100,000 pool, plus the 20,000 base award.
        self.assertEqual(result["points_decimal"], "90000")

    def test_referendum_tie_loser_and_unknown_pool(self):
        payload = {"track": "polling", "type": "referendum", "team_side": "yes", "yes_votes": 50, "no_votes": 50}
        self.assertEqual(calculate_scenario(payload)["points_decimal"], "0")
        self.assertEqual(calculate_scenario({**payload, "yes_votes": 49})["points_decimal"], "0")
        unknown = calculate_scenario({**payload, "yes_votes": 60, "team_persuasion_weight": "1"})
        self.assertIsNone(unknown["points_decimal"])
        self.assertEqual(unknown["partial_points_decimal"], "20000")
        qualified_by_score = calculate_scenario({**payload, "yes_votes": 60, "team_score": "7500", "team_persuasion_weight": "0"})
        self.assertEqual(qualified_by_score["points_decimal"], "20000")
        below_threshold = calculate_scenario({**payload, "yes_votes": 60, "team_score": "7499", "team_persuasion_weight": "0"})
        self.assertEqual(below_threshold["points_decimal"], "0")

    def test_invalid_inputs_do_not_silently_coerce(self):
        with self.assertRaises(ValueError):
            calculate_scenario({"track": "red", "recorded_at": "2026-10-19", "engagement": {"likes": -1}})
        with self.assertRaises(ValueError):
            calculate_scenario({"track": "blue", "ruling": "inauthentic", "report_rank": 1, "target_is_own": "false"})
        with self.assertRaises(ValueError):
            calculate_scenario({"track": "polling", "type": "referendum", "team_side": "yes", "yes_votes": 60, "no_votes": 40,
                                "qualifying_changes": 1, "contributions": [5], "winning_side_total_weight": "10"})


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def test_no_data_is_not_official_zero(self):
        result = score_overview(self.path)
        self.assertIsNone(result["official_total"])
        self.assertEqual(result["reconciliation"]["status"], "no_snapshot")

    def test_single_zero_snapshot_waits_for_interval_without_claiming_discrepancy(self):
        add_score_snapshot(self.path, "0", "2026-10-19T07:00:00Z")
        result = score_overview(self.path)
        self.assertEqual(result["official_total"], "0")
        self.assertEqual(result["reconciliation"]["status"], "single_snapshot")
        self.assertIsNone(result["reconciliation"]["unexplained_decimal"])
        self.assertEqual(result["reconciliation"]["absolute_balance_minus_parsed_decimal"], "0")
        self.assertEqual(result["reconciliation"]["intervals"], [])

    def test_interval_reconciliation_and_unknown_scope(self):
        add_score_snapshot(self.path, "100", "2026-10-19T07:00:00Z")
        add_score_snapshot(self.path, "105.25", "2026-10-19T08:00:00Z")
        import_activity_csv(self.path, export_csv([("2026-10-19T07:30:00Z", "SCORE_RED", "Awarded 5.25 points")]))
        result = score_overview(self.path)
        self.assertEqual(result["official_total"], "105.25")
        self.assertEqual(result["reconciliation"]["intervals"][0]["difference"], "0")
        self.assertEqual(result["reconciliation"]["status"], "matched_observed_scope")
        self.assertEqual(result["reconciliation"]["unexplained_decimal"], "0")
        self.assertEqual(result["reconciliation"]["absolute_balance_minus_parsed_decimal"], "100")
        import_activity_csv(self.path, export_csv([("2026-10-19T07:35:00Z", "SCORE_RED", "NPC engagement: 5 views")]))
        result = score_overview(self.path)
        self.assertEqual(result["reconciliation"]["status"], "unresolved")
        self.assertEqual(result["activity"]["unparsed"], 1)
        self.assertEqual(result["parsed_total"], "5.25")

    def test_pace_projects_poll_threshold_with_later_multipliers(self):
        add_score_snapshot(self.path, "4000", "2026-10-23T00:00:00Z")
        add_score_snapshot(self.path, "4600", "2026-10-23T06:00:00Z")
        result = score_pace(self.path, "2026-10-23T06:00:00Z")
        self.assertEqual(result["total"], "4600")
        self.assertFalse(result["stale"])
        self.assertEqual(result["rates"]["6h"]["points_per_hour"], 100)
        self.assertIsNone(result["rates"]["24h"])
        self.assertEqual(result["rate_basis"], "12h")
        self.assertEqual(result["rates"]["12h"]["points_per_hour"], 100)
        poll2, poll3 = result["polls"]
        self.assertEqual(poll2["estimated_count_at"], "2026-10-24T20:11:00.000000Z")
        self.assertEqual(poll2["gap"], "2400")
        self.assertEqual(poll2["eta"], "2026-10-24T06:00:00.000000Z")
        self.assertTrue(poll2["on_track"])
        self.assertAlmostEqual(poll2["projected_total"], 4600 + 100 * poll2["hours_left"], delta=10)
        # Day 7 (×2) starts 2026-10-25 00:00 Sydney = 13:00 UTC, before Poll 3.
        weighted = 55 + 2 * (poll3["hours_left"] - 55)
        self.assertAlmostEqual(poll3["projected_total"], 4600 + 100 * weighted, delta=10)
        self.assertAlmostEqual(poll3["required_base_points_per_hour"], 2400 / weighted, delta=0.2)

    def test_pace_excludes_poll_awards_and_reports_shortfall(self):
        add_score_snapshot(self.path, "1000", "2026-10-20T15:00:00Z")
        add_score_snapshot(self.path, "4060", "2026-10-20T21:00:00Z")
        import_activity_csv(self.path, export_csv([("2026-10-20T20:11:43Z", "SCORE_NARRATIVE",
                                                    "+3000 points: Narrative: the poll was too close to call (52–48), so both sides share it")]))
        result = score_pace(self.path, "2026-10-20T21:00:00Z")
        self.assertEqual(result["rate_basis"], "12h")
        self.assertEqual(result["rates"]["12h"]["points_per_hour"], 10)
        self.assertEqual(result["rates"]["6h"]["excluded_polling_points"], "3000")
        self.assertEqual(result["rates"]["6h"]["points_per_hour"], 10)
        poll2 = result["polls"][0]
        self.assertFalse(poll2["on_track"])
        self.assertIsNone(poll2["eta"])
        self.assertLess(poll2["projected_total"], 7000)
        self.assertGreater(poll2["required_points_per_hour_now"], 10)

    def test_pace_without_snapshots_is_unknown_not_zero(self):
        result = score_pace(self.path, "2026-10-23T06:00:00Z")
        self.assertIsNone(result["total"])
        self.assertTrue(result["stale"])
        self.assertIsNone(result["polls"][0]["gap"])
        self.assertIsNone(result["polls"][0]["on_track"])
        self.assertEqual([poll["poll"] for poll in score_pace(self.path, "2026-10-26T00:00:00Z")["polls"]], [3])

if __name__ == "__main__":
    unittest.main()
