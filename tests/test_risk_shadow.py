"""Shadow re-checks: a new suspect is asked again 5 and 10 minutes after its first 404, recorded only (phase 1)."""
import unittest
from datetime import timedelta
from unittest.mock import patch

from ctn_data import risk_assessment as risk
from ctn_data.db import connect
from tests import test_risk_assessment as base
from tests.test_risk_assessment import NOW, FakeSocial, iso


class ShadowTests(unittest.TestCase):
    # The risk assessment fixture: posts of our role, a teammate and two other accounts.
    setUp = base.RiskAssessmentTests.setUp
    observe = base.RiskAssessmentTests.observe
    rows = base.RiskAssessmentTests.rows
    listed = base.RiskAssessmentTests.listed

    def shadow(self, social, minutes):
        return risk.shadow(self.db, social, at=NOW + timedelta(minutes=minutes), pause=0)

    def shadows(self):
        return self.rows('SELECT post_id,check_label,http_status,minutes_after_first_404 FROM post_visibility_shadow '
                         'ORDER BY post_id,check_label')

    def test_each_check_runs_once_at_its_minute_and_never_changes_the_status(self):
        self.assertEqual(risk.SHADOW_CHECKS, (('5m', 5), ('20m', 20)))
        self.observe(3, 404, iso())
        social = FakeSocial([], gone={3})
        self.assertEqual(self.shadow(social, 4)['due'], 0)
        self.assertEqual(self.shadow(social, 5)['5m_gone'], 1)
        self.assertEqual(self.shadow(social, 6)['due'], 0)
        self.assertEqual(self.rows('SELECT status,first_404_at,checks FROM post_visibility'), [('suspect', iso(), 1)])
        # The rule: a 404 again at 10 minutes confirms.
        self.assertEqual(risk.recheck(self.db, social, at=NOW + timedelta(minutes=10), pause=0).get('hidden'), 1)
        # Served again at 20 minutes: the 10-minute rule was wrong here; recorded, not acted on.
        social.gone.clear()
        self.assertEqual(self.shadow(social, 20)['20m_back'], 1)
        self.assertEqual(self.shadow(social, 21)['due'], 0)
        self.assertEqual(social.gets(), [3, 3, 3])
        self.assertEqual(self.shadows(), [(3, '20m', 200, 20.0), (3, '5m', 404, 5.0)])
        self.assertEqual(self.rows('SELECT status FROM post_visibility'), [('hidden',)])
        self.assertEqual(self.rows('SELECT post_id FROM hidden_posts'), [(3,)])

    def test_a_suspect_too_old_for_its_check_is_skipped(self):
        self.observe(3, 404, iso(minutes=-30))
        conn = connect(self.db)
        try:
            self.assertEqual(risk.shadow_due(conn, NOW + timedelta(minutes=1)), [])
            # 14 minutes after the first 404 the 5-minute check is 9 minutes late but due;
            # at 25 it is 20 minutes late and skipped, and the 20-minute check is due.
            self.assertEqual([row[2] for row in risk.shadow_due(conn, NOW - timedelta(minutes=16))], ['5m'])
            self.assertEqual([row[2] for row in risk.shadow_due(conn, NOW - timedelta(minutes=5))], ['20m'])
        finally:
            conn.close()

    def test_an_unanswered_shadow_check_waits_retry_minutes(self):
        self.observe(3, 404, iso())
        social = FakeSocial([], gone={3}, errors={3})
        self.assertEqual(self.shadow(social, 5)['5m_unanswered'], 1)
        self.assertEqual(self.shadow(social, 8)['due'], 0)
        social.errors.clear()
        self.assertEqual(self.shadow(social, 10)['5m_gone'], 1)
        self.assertEqual(self.shadows(), [(3, '5m', 404, 10.0)])

    def test_a_suspect_settled_before_its_check_is_not_asked(self):
        self.observe(3, 404, iso())
        self.observe(3, 200, iso(minutes=3))  # served again: no first 404 any more
        social = FakeSocial([], gone={3})
        self.assertEqual(self.shadow(social, 5)['due'], 0)
        self.assertEqual(social.gets(), [])

    def test_the_summary_counts_answers_and_what_became_of_the_posts(self):
        for post_id in (1, 3, 4, 5):
            self.observe(post_id, 404, iso())
        social = FakeSocial([], gone={3, 4}, errors={5})
        self.shadow(social, 5)  # 1 back, 3 and 4 gone, 5 unanswered
        social.errors.clear()
        self.shadow(social, 10)  # 5's check retried (RETRY_MINUTES later) and now answered (back)
        # Then, as in the monitor, the due re-checks: 3 and 4 confirmed hidden; 1 and 5 served, become visible.
        risk.recheck(self.db, social, at=NOW + timedelta(minutes=10), pause=0)
        social.gone.discard(4)
        self.shadow(social, 20)  # the confirmed 3 still gone, 4 back: one wrong 10-minute confirmation
        conn = connect(self.db)
        try:
            summary = risk.shadow_summary(conn)
        finally:
            conn.close()
        self.assertEqual(summary['posts'], 4)
        self.assertEqual(summary['confirm_minutes'], 10)
        five, twenty = summary['labels']['5m'], summary['labels']['20m']
        self.assertEqual((five['checked'], five['gone'], five['back'], five['unanswered']), (4, 2, 2, 0))
        self.assertEqual(five['gone_now'], {'hidden': 2, 'visible': 0, 'suspect': 0})
        self.assertEqual(five['back_now'], {'hidden': 0, 'visible': 2, 'suspect': 0})
        self.assertEqual((twenty['checked'], twenty['gone'], twenty['back']), (2, 1, 1))
        self.assertEqual(twenty['back_now'], {'hidden': 1, 'visible': 0, 'suspect': 0})
        self.assertEqual(twenty['median_minutes'], 20.0)

    def test_the_overview_carries_the_summary(self):
        self.observe(3, 404, iso())
        self.shadow(FakeSocial([], gone={3}), 5)
        view = risk.overview(self.db, NOW + timedelta(minutes=6))
        self.assertEqual(view['shadow']['labels']['5m']['gone'], 1)
        self.assertEqual(view['rules']['shadow_checks'], [5, 20])


class InterleaveTests(unittest.TestCase):
    setUp = base.RiskAssessmentTests.setUp
    observe = base.RiskAssessmentTests.observe
    rows = base.RiskAssessmentTests.rows
    listed = base.RiskAssessmentTests.listed

    def test_due_checks_run_inside_a_long_scan_at_most_every_due_check_seconds(self):
        self.observe(4, 404, iso(minutes=-5))  # its 5-minute shadow check is due now
        social = FakeSocial(self.listed(3), gone={3, 4})
        monitor = risk.RiskMonitor(self.db, client=social, pause=0)
        calls = []
        between = lambda: (calls.append(1), monitor._interleave(NOW))
        with patch.object(risk, 'PAGE_LIMIT', 2):
            risk.scan(self.db, social, iso(6), iso(minutes=-1), at=NOW, pause=0, between=between)
        self.assertGreater(len(calls), 1)
        # One shadow GET for post 4 (the first call), none more within DUE_CHECK_SECONDS.
        self.assertEqual(social.gets().count(4), 1)
        self.assertEqual(self.rows('SELECT post_id,check_label FROM post_visibility_shadow'), [(4, '5m')])
        self.assertEqual(monitor.state()['shadow']['5m_gone'], 1)


if __name__ == '__main__':
    unittest.main()
