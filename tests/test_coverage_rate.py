from datetime import datetime, timezone
import unittest

from ctn_data.coverage import coverage
from ctn_data.ingest import save_posts, save_counters
from tests.test_collection import BaseDatabase


class CoverageRateTests(BaseDatabase):
    def result(self, runs=()):
        return coverage(self.conn,{},list(runs),
            now=datetime(2026,10,19,12,0,tzinfo=timezone.utc))['counters']

    def test_new_coverage_uses_first_observation_across_all_sources(self):
        save_posts(self.conn,[{'post_id':i,'body':'sample'} for i in range(1,6)],'latest')
        save_counters(self.conn,1,{'like_count':1},'2026-10-19T11:50:00Z')
        save_counters(self.conn,1,{'like_count':2},'2026-10-19T11:55:00Z','browser:counters')
        save_counters(self.conn,2,{'like_count':1},'2026-10-19T11:58:00Z','proxy:one')
        save_counters(self.conn,3,{'like_count':1},'2026-10-19T10:00:00Z')
        save_counters(self.conn,3,{'like_count':2},'2026-10-19T11:59:00Z')
        result=self.result()
        self.assertEqual(result['first_samples_in_window'],2)
        self.assertEqual(result['first_sample_window_minutes'],15)
        self.assertEqual(result['first_samples_per_minute'],.1)
        self.assertEqual(result['missing'],2)
        self.assertEqual(result['backlog_minutes'],15)

    def test_request_run_counts_cannot_invent_a_new_coverage_rate(self):
        save_posts(self.conn,[{'post_id':1,'body':'old'},{'post_id':2,'body':'missing'}],'latest')
        save_counters(self.conn,1,{'like_count':1},'2026-10-19T10:00:00Z')
        save_counters(self.conn,1,{'like_count':2},'2026-10-19T11:59:00Z')
        result=self.result([{'status':'completed','started_at':'2026-10-19T11:59:00Z',
            'finished_at':'2026-10-19T11:59:30Z','metrics':{'requests':300,
            'counter_sampling':{'new':{'saved':300}}}}])
        self.assertEqual(result['first_samples_in_window'],0)
        self.assertIsNone(result['first_samples_per_minute'])
        self.assertIsNone(result['backlog_minutes'])

    def test_future_observations_are_excluded_and_zero_backlog_is_zero(self):
        save_posts(self.conn,[{'post_id':1,'body':'future'},{'post_id':2,'body':'now'}],'latest')
        save_counters(self.conn,1,{'like_count':1},'2026-10-19T13:00:00Z')
        save_counters(self.conn,2,{'like_count':1},'2026-10-19T12:00:00Z')
        result=self.result()
        self.assertEqual(result['first_samples_in_window'],1)
        self.assertEqual(result['backlog_minutes'],0)
