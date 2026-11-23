import hashlib
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from ctn_data.browser_capture import capture
from ctn_data.browser_status import collection_status, latest_portal
from ctn_data.db import connect, initialize, utc_now
from ctn_data.server import create_app


class BrowserFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='browser-freshness-', dir=Path(__file__).parent)
        self.db = Path(self.tmp.name) / 'test.sqlite3'
        initialize(self.db)
        conn = connect(self.db)
        with conn:
            conn.executemany('INSERT INTO meta(key,value) VALUES(?,?)',
                             [('browser_team_id', '46'), ('browser_extension_id', 'a' * 32)])
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def portal(self, rid='portal-freshness-001', at='2026-10-19T03:00:00Z', total='30', csv=None):
        return dict(id=rid, kind='portal', observed_at=at, data=dict(team_id=46, total=total,
                    activity_csv=csv if csv is not None else 'When (UTC),Type,Message\n2026-10-19T01:00:00Z,SCORE_RED,Awarded 5 points\n'))

    def endpoint(self, app, path, method='GET'):
        return next(route.endpoint for route in app.routes
                    if getattr(route, 'path', None) == path and method in route.methods)

    def test_delayed_old_upload_cannot_regress_latest_portal(self):
        records = [self.portal(), self.portal('portal-delayed-001', '2026-10-19T02:00:00Z', '20'),
                   self.portal('portal-subsecond-001', '2026-10-19T03:00:00.000001Z', '31')]
        for record in records:
            self.assertFalse(capture(self.db, [record])['errors'])
        # The un-fractioned spelling sorts after the fractioned one as raw text.
        self.assertFalse(capture(self.db, [self.portal('portal-late-retry-001')])['errors'])
        conn = connect(self.db)
        try:
            portal, ingested = latest_portal(conn)
        finally:
            conn.close()
        self.assertEqual(portal['total'], '31')
        self.assertIsNotNone(ingested)
        app = create_app(self.db)
        self.assertEqual(self.endpoint(app, '/api/extension-status')()['last_portal']['total'], '31')
        self.assertEqual(self.endpoint(app, '/api/score')()['official_total'], '31')

    def test_freshness_requires_committed_collection_and_has_exact_boundaries(self):
        now = datetime(2026, 10, 19, 3, 0, tzinfo=timezone.utc)
        heartbeat = dict(at=now.isoformat(), auto_enabled=True, queue_count=0,
                         pages=[dict(role='portal', enabled=True, last_collected_at=now.isoformat())])
        status = collection_status('a' * 32, heartbeat, None, None, now=now)
        self.assertEqual(status['state'], 'starting')
        self.assertFalse(status['healthy'])
        for seconds, expected in ((299, 'healthy'), (300, 'stale')):
            portal = dict(observed_at=(now - timedelta(seconds=seconds)).isoformat())
            status = collection_status('a' * 32, heartbeat, portal, now.isoformat(), now=now)
            self.assertEqual(status['state'], expected)
            self.assertEqual(status['age_seconds'], seconds)
            self.assertEqual(status['freshness_seconds'], 300)
        heartbeat['at'] = (now - timedelta(seconds=90)).isoformat()
        self.assertEqual(collection_status('a' * 32, heartbeat, dict(observed_at=now.isoformat()), None, now=now)['state'], 'offline')

    def test_diagnostic_states_follow_priority(self):
        now = datetime(2026, 10, 19, 3, 0, tzinfo=timezone.utc)
        heartbeat = dict(at=now.isoformat(), auto_enabled=True, queue_count=0,
                         pages=[dict(role='portal', enabled=True)])
        portal = dict(observed_at=now.isoformat())
        cases = [
            ('unpaired', None, {'buffer_full': True}),
            ('offline', 'paired', {'at': '2026-10-19T01:00:00Z', 'buffer_full': True}),
            ('buffer_full', 'paired', {'buffer_full': True, 'auto_enabled': False}),
            ('paused', 'paired', {'auto_enabled': False}),
            ('paused', 'paired', {'pages': [dict(role='portal', enabled=False, code='success')]}),
            ('waiting_login', 'paired', {'pages': [dict(role='portal', enabled=False, code='auth', http_status=401, pause_until=int((now.timestamp() + 300) * 1000))]}),
            ('cooldown', 'paired', {'pages': [dict(role='portal', enabled=False, code='rate_limit', http_status=429, pause_until=int((now.timestamp() + 300) * 1000))]}),
            ('error', 'paired', {'pages': [dict(role='portal', enabled=False, code='schema', error='页面格式变化')]}),
            ('pending', 'paired', {'queue_count': 1}),
            ('healthy', 'paired', {}),
        ]
        for expected, extension, changes in cases:
            with self.subTest(expected):
                status = collection_status(extension, {**heartbeat, **changes}, portal, None, now=now)
                self.assertEqual(status['state'], expected)
                self.assertEqual(status['healthy'], expected == 'healthy')

    def test_score_and_extension_use_same_status_and_validate_heartbeat_diagnostics(self):
        app = create_app(self.db)
        at = utc_now()
        self.assertFalse(capture(self.db, [self.portal(at=at)])['errors'])
        heartbeat = self.endpoint(app, '/api/extension-status', 'POST')
        page = dict(role='portal', tab_id=42, enabled=True, last_attempt_at=at,
                    last_collected_at=at, http_status=200, activity_rows=1,
                    activity_digest='a' * 64, error='', code='success', html='<html>private</html>', csv='private')
        payload = dict(extension_id='a' * 32, version='1.3.1', auto_enabled=True, pages=[page])
        self.assertEqual(heartbeat(payload), {'ok': True})
        extension = self.endpoint(app, '/api/extension-status')()
        score = self.endpoint(app, '/api/score')()['automatic_collection']
        for key in ('state', 'healthy', 'reason', 'last_collected_at', 'last_ingested_at', 'heartbeat_at', 'last_attempt_at', 'http_status'):
            self.assertEqual(score[key], extension['collection_status'][key])
        self.assertTrue(score['enabled'])
        self.assertNotIn('html', extension['heartbeat']['pages'][0])
        self.assertNotIn('csv', extension['heartbeat']['pages'][0])
        invalid = [('last_attempt_at', '2026-10-19T03:00:00'), ('last_collected_at', 'not-time'),
                   ('last_attempt_at', (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
                   ('http_status', True), ('http_status', 600), ('activity_rows', -1),
                   ('activity_digest', 'x' * 64), ('error', 'x' * 181),
                   ('error', '<html>source</html>'), ('code', 'raw data')]
        for key, value in invalid:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                heartbeat({**payload, 'pages': [{**page, key: value}]})

    def test_pending_page_becomes_healthy_when_its_observation_is_committed(self):
        app = create_app(self.db)
        at = utc_now()
        heartbeat = self.endpoint(app, '/api/extension-status', 'POST')
        payload = dict(extension_id='a' * 32, version='1.3.1', auto_enabled=True, queue_count=0,
                       pages=[dict(role='portal', tab_id=42, enabled=True, code='pending',
                                   last_attempt_at=at, last_collected_at=at)])
        heartbeat(payload)
        read = self.endpoint(app, '/api/extension-status')
        self.assertEqual(read()['collection_status']['state'], 'pending')
        self.assertFalse(capture(self.db, [self.portal(at=at)])['errors'])
        # The page retains pending until its next request; database confirmation
        # of that same observation already proves this collection succeeded.
        self.assertEqual(read()['collection_status']['state'], 'healthy')
        later = (datetime.fromisoformat(at.replace('Z', '+00:00')) + timedelta(seconds=1)).isoformat()
        payload['pages'][0]['last_collected_at'] = later
        heartbeat(payload)
        self.assertEqual(read()['collection_status']['state'], 'pending')
        self.assertFalse(capture(self.db, [self.portal(rid='portal-next-pending-001', at=later)])['errors'])
        self.assertEqual(read()['collection_status']['state'], 'healthy')
        heartbeat({**payload, 'queue_count': 1})
        self.assertEqual(read()['collection_status']['state'], 'pending')

    def test_malformed_csv_is_rejected_atomically_before_filtering(self):
        texts = ['2026-10-19T01:00:00Z,SCORE_RED',
                 '2026-10-19T01:00:00Z',
                 '2026-10-19T01:00:00Z,SCORE_RED,"Awarded 13 points',
                 '2026-10-19T01:00:00Z,SCORE_RED,ok,extra',
                 '2026-10-19T01:00:00Z,TEAMCODE']
        for index, row in enumerate(texts):
            with self.subTest(row=row):
                result = capture(self.db, [self.portal(rid=f'portal-bad-csv-{index}', csv='When (UTC),Type,Message\n' + row + '\n')])
                self.assertEqual(result['accepted'], [])
                self.assertFalse(result['errors'][0]['retryable'])
        conn = connect(self.db)
        try:
            for table in ('official_activity', 'activity_imports', 'score_snapshots', 'checkpoints'):
                self.assertEqual(conn.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0, table)
        finally:
            conn.close()

    def test_receipt_has_event_time_digest_and_lag_without_rewriting_event_time(self):
        csv = ('When (UTC),Type,Message\n2026-10-19T01:00:00Z,SCORE_RED,Awarded 5 points\n'
               '2026-10-19T02:00:00Z,TEAMCODE,private-code\n')
        self.assertFalse(capture(self.db, [self.portal(csv=csv)])['errors'])
        conn = connect(self.db)
        try:
            receipt, _ = latest_portal(conn)
            event = conn.execute('SELECT occurred_at FROM official_activity').fetchone()[0]
            raw = ''.join(row[0] for row in conn.execute('SELECT payload_json FROM raw_records'))
        finally:
            conn.close()
        expected_csv = '"When (UTC)","Type","Message"\r\n"2026-10-19T01:00:00Z","SCORE_RED","Awarded 5 points"'
        self.assertEqual(receipt['activity_digest'], hashlib.sha256(expected_csv.encode()).hexdigest())
        self.assertEqual(receipt['team_id'], 46)
        self.assertEqual(receipt['activity_latest_at'], '2026-10-19T01:00:00.000000Z')
        self.assertEqual(receipt['activity_lag_seconds'], 7200)
        self.assertEqual(event, receipt['activity_latest_at'])
        self.assertNotIn('private-code', raw)


if __name__ == '__main__':
    unittest.main()
