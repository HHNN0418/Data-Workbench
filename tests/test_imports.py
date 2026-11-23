from contextlib import closing
import csv
import io
from pathlib import Path
import tempfile
import unittest

from ctn_data.db import connect, initialize
from ctn_data.imports import add_score_snapshot, decimal_text, import_activity_csv, parse_activity


def export_csv(rows, bom=False):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["When (UTC)", "Type", "Message"])
    writer.writerows(rows)
    return ("\ufeff" if bom else "") + output.getvalue()


class ImportsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        initialize(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def rows(self):
        with closing(connect(self.path)) as conn, conn:
            return [dict(row) for row in conn.execute("SELECT * FROM official_activity ORDER BY id")]

    def test_bom_and_reimport_are_idempotent(self):
        text = export_csv([("2026-10-19 07:00:00", "MEMBERSHIP", "joined")], bom=True)
        self.assertEqual(import_activity_csv(self.path, text)["inserted"], 1)
        self.assertTrue(import_activity_csv(self.path, text)["already_imported"])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["parse_status"], "non_scoring")

    def test_overlap_preserves_max_duplicate_multiplicity(self):
        event = ("2026-10-19T07:00:00Z", "SCORE_RED", "Awarded 5 points")
        second = ("2026-10-19T08:00:00Z", "SCORE_BLUE", "Awarded 25 points")
        self.assertEqual(import_activity_csv(self.path, export_csv([event, event]))["inserted"], 2)
        result = import_activity_csv(self.path, export_csv([event, event, second]))
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(len(self.rows()), 3)
        result = import_activity_csv(self.path, export_csv([event, event, event, second]))
        self.assertEqual(result["inserted"], 1)
        self.assertEqual([row["occurrence"] for row in self.rows() if row["track"] == "red"], [1, 2, 3])

    def test_counts_and_multiplier_are_not_booked_points(self):
        for text in ("NPC engagement (×2 points): 8 views, 2 likes",
                     "Moderation (×0.1 points): 1 inauthentic report",
                     "Narrative: Includes your share for 312 changes of mind and 840 contributions"):
            parsed = parse_activity("SCORE_RED", text)
            self.assertIsNone(parsed["points_decimal"])
            self.assertEqual(parsed["parse_status"], "unparsed")

    def test_explicit_signed_decimal_only(self):
        self.assertEqual(parse_activity("SCORE_RED", "Points: -20.25")["points_decimal"], "-20.25")
        self.assertEqual(parse_activity("SCORE_RED", "Lost 20 points")["points_decimal"], "-20")
        self.assertEqual(parse_activity("SCORE_RED", "Points deducted: 20")["points_decimal"], "-20")
        self.assertEqual(parse_activity("SCORE_BLUE", "Awarded 1,250.50 points")["points_decimal"], "1250.5")
        self.assertIsNone(parse_activity("SCORE_RED", "Awarded 5 points and lost 20 points")["points_decimal"])
        self.assertIsNone(parse_activity("SCORE_RED", "If upheld, awarded 25 points")["points_decimal"])
        self.assertIsNone(parse_activity("SCORE_RED", "Total points: 1000")["points_decimal"])

    def test_legacy_track_uses_semantics_and_does_not_guess(self):
        parsed = parse_activity("SCORE_NARRATIVE", "+75 points: NPC engagement: 5 replies")
        self.assertEqual(parsed["track"], "red")
        self.assertEqual(parsed["points_decimal"], "75")
        self.assertIsNone(parse_activity("SCORE_NARRATIVE", "+75 points")["track"])

    def test_repeated_independent_equal_amounts_are_ambiguous(self):
        self.assertEqual(parse_activity("SCORE_RED", "Awarded +5 points")["points_decimal"], "5")
        self.assertIsNone(parse_activity("SCORE_RED", "+5 points for a like; +5 points for another like")["points_decimal"])

    def test_embedded_newline_and_sql_text_preserved(self):
        message = "'; DROP TABLE posts; --\nNPC engagement: 1 like"
        import_activity_csv(self.path, export_csv([("2026-10-19T07:00:00Z", "SCORE_RED", message)]))
        self.assertEqual(self.rows()[0]["message"], message)
        with closing(connect(self.path)) as conn, conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 0)
            self.assertIn(message.replace("\n", "\\n"), conn.execute("SELECT payload_json FROM raw_records").fetchone()[0])

    def test_invalid_time_preserved_without_false_points(self):
        import_activity_csv(self.path, export_csv([("unrecognized", "SCORE_RED", "Awarded 5 points")]))
        row = self.rows()[0]
        self.assertEqual(row["parse_status"], "invalid_time")
        self.assertIsNone(row["occurred_at"])
        self.assertIsNone(row["points_decimal"])

    def test_invalid_csv_is_atomic(self):
        with self.assertRaises(ValueError):
            import_activity_csv(self.path, "When (UTC),Type,Message\n2026-10-19,SCORE_RED,ok,extra\n")
        self.assertEqual(self.rows(), [])
        with self.assertRaises(ValueError):
            import_activity_csv(self.path, "Type,Message,When (UTC)\n")

    def test_decimal_validation_and_snapshot_idempotence(self):
        self.assertEqual(decimal_text("0.10000000000000001"), "0.10000000000000001")
        precise = "123456789012345678901234567890.123456789"
        self.assertEqual(decimal_text(precise), precise)
        for value in ("NaN", "Infinity", True, "abc"):
            with self.assertRaises(ValueError):
                add_score_snapshot(self.path, value)
        first = add_score_snapshot(self.path, "-0.25", "2026-10-19T18:00:00+11:00")
        second = add_score_snapshot(self.path, "-0.25", "2026-10-19T07:00:00Z")
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["already_exists"])


if __name__ == "__main__":
    unittest.main()

