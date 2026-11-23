"""Risk assessment: hidden posts found from the recent feed, confirmed by a second 404 after 20 minutes."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from ctn_data import copy_patterns, hidden_posts, risk_assessment as risk
from ctn_data.db import connect, initialize

NOW = datetime(2026, 10, 24, 3, 0, tzinfo=timezone.utc)


def iso(hours_ago=0, minutes=0):
    return risk._key(NOW - timedelta(hours=hours_ago) + timedelta(minutes=minutes))


class NotFound(Exception):
    status = 404


class Unavailable(Exception):
    status = 503


class Page(list):
    def __init__(self, items, next_cursor=None):
        super().__init__(items)
        self.next_cursor = next_cursor


class FakeSocial:
    """The recent feed lists every post it is given; get_post answers 404 for gone posts."""

    def __init__(self, listed, gone=(), errors=()):
        self.listed = sorted(listed, key=lambda item: (item['created_at'], item['post_id']), reverse=True)
        self.gone, self.errors, self.calls = set(gone), set(errors), []
        self.rate_limit = None

    def feed_latest(self, limit=50, cursor=None):
        raw = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
        self.calls.append(('feed', raw['t']))
        rows = [item for item in self.listed if (item['created_at'], item['post_id']) < (raw['t'], raw['i'])][:limit]
        following = risk.cursor(rows[-1]['created_at'], rows[-1]['post_id']) if len(rows) == limit else None
        return Page(rows, following)

    def get_post(self, post_id):
        self.calls.append(('get', post_id))
        if post_id in self.errors:
            raise Unavailable()
        if post_id in self.gone:
            raise NotFound()
        return {'post_id': post_id}

    def gets(self):
        return [call[1] for call in self.calls if call[0] == 'get']


class RiskAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'ctn.sqlite3')
        initialize(self.db)
        # Post id -> (author, hours ago, visibility). 6464 is our role, 7000 a teammate, 8000/8001 others.
        self.posts = {1: (8000, 1, 'public'), 2: (8000, 2, 'public'), 3: (8001, 3, 'public'), 4: (6464, 4, 'public'),
                      5: (7000, 5, 'public'), 6: (8001, 2.5, 'followers'), 7: (8000, 10, 'public')}
        conn = connect(self.db)
        with conn:
            conn.execute('INSERT INTO meta(key,value) VALUES(?,?)', (hidden_posts.AGENTS_KEY, json.dumps(
                {'a': {'id': 'a', 'name': 'Leah', 'account_id': 6464}})))
            conn.execute("INSERT INTO team_accounts(account_id,team_label,is_own,evidence,updated_at) VALUES(7000,'Agent Zero',0,'',?)",
                         (iso(),))
            for account_id, name in ((8000, 'bot_a'), (8001, 'bot_b'), (6464, 'leah'), (7000, 'mate')):
                conn.execute('INSERT INTO accounts(account_id,username,first_seen,last_seen) VALUES(?,?,?,?)',
                             (account_id, name, iso(), iso()))
            for post_id, (author, hours, visibility) in self.posts.items():
                conn.execute('INSERT INTO posts(post_id,author_id,body,visibility,created_at,first_seen,last_seen) '
                             'VALUES(?,?,?,?,?,?,?)', (post_id, author, f'text {post_id}', visibility, iso(hours),
                                                       iso(hours), iso(hours)))
            for index in range(100):  # 8000 has 100+ collected actions
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                             (1000 + index, 8000, 'old', iso(100), iso(100), iso(100)))
        conn.close()

    def listed(self, *missing):
        return [{'post_id': post_id, 'created_at': iso(hours)} for post_id, (_, hours, visibility) in self.posts.items()
                if post_id not in missing and visibility == 'public']

    def rows(self, sql, *args):
        conn = connect(self.db)
        try:
            return [tuple(row) for row in conn.execute(sql, args)]
        finally:
            conn.close()

    def observe(self, post_id, answer, at):
        author, hours, _ = self.posts[post_id]
        conn = connect(self.db)
        try:
            with conn:
                return risk.observe(conn, post_id, author, iso(hours), answer, at)
        finally:
            conn.close()

    def test_cursor_is_the_feed_s_time_and_id(self):
        raw = json.loads(base64.urlsafe_b64decode(risk.cursor('2026-10-23T15:26:27.138867Z', 310905) + '=='))
        self.assertEqual(raw, {'t': '2026-10-23T15:26:27.138867Z', 'i': 310905})

    def test_a_404_is_confirmed_only_ten_minutes_after_the_first(self):
        self.assertEqual(risk.CONFIRM_MINUTES, 10)  # phase 2 (「改吧」), down from 20
        self.assertEqual(self.observe(3, 404, iso()), 'suspect')
        self.assertEqual(self.rows('SELECT post_id FROM hidden_posts'), [])
        self.assertEqual(self.observe(3, 404, iso(minutes=9)), 'suspect')
        self.assertEqual(self.observe(3, 404, iso(minutes=10)), 'hidden')
        self.assertEqual(self.rows('SELECT post_id,account_id,detected_at FROM hidden_posts'), [(3, 8001, iso(minutes=10))])
        self.assertEqual(self.rows('SELECT status,first_404_at,confirmed_at,checks FROM post_visibility'),
                         [('hidden', iso(), iso(minutes=10), 3)])

    def test_a_post_served_again_within_ten_minutes_is_never_confirmed(self):
        self.observe(3, 404, iso())
        self.assertEqual(self.observe(3, 200, iso(minutes=7)), 'visible')
        social = FakeSocial([], gone={3})
        self.assertEqual(risk.recheck(self.db, social, at=NOW + timedelta(minutes=10), pause=0)['due'], 0)
        self.assertEqual(social.gets(), [])
        self.assertEqual(self.rows('SELECT status,first_404_at FROM post_visibility'), [('visible', None)])
        self.assertEqual(self.rows('SELECT post_id FROM hidden_posts'), [])

    def test_a_served_post_clears_the_suspicion_and_leaves_hidden_posts(self):
        self.observe(3, 404, iso())
        self.assertEqual(self.observe(3, 200, iso(minutes=10)), 'visible')
        self.assertEqual(self.observe(3, 404, iso(minutes=25)), 'suspect')  # a new first 404
        self.assertEqual(self.observe(3, 404, iso(minutes=45)), 'hidden')
        self.assertEqual(self.observe(3, 200, iso(minutes=60)), 'visible')
        self.assertEqual(self.rows('SELECT post_id FROM hidden_posts'), [])

    def test_other_answers_change_nothing(self):
        self.assertEqual(self.observe(3, None, iso()), 'unknown')
        self.assertEqual(self.rows('SELECT post_id FROM post_visibility'), [])
        self.observe(3, 404, iso())
        self.assertEqual(self.observe(3, 503, iso(minutes=25)), 'suspect')

    def test_posts_our_roles_deleted_are_skipped(self):
        conn = connect(self.db)
        with conn:
            conn.execute("INSERT INTO agent_audit_events(id,agent_id,at,kind,message,details_json) VALUES('e1','a',?,"
                         "'published','',?)", (iso(), json.dumps({'action': 'delete_post', 'target_post_id': 4,
                                                                  'already_applied': False})))
        conn.close()
        self.assertEqual(self.observe(4, 404, iso()), 'skipped')
        self.assertEqual(self.rows('SELECT post_id FROM post_visibility'), [])

    def test_a_scan_asks_only_about_posts_missing_from_the_feed(self):
        social = FakeSocial(self.listed(3, 5), gone={3})
        with mock.patch.object(risk, 'PAGE_LIMIT', 2):
            result = risk.scan(self.db, social, iso(6), iso(minutes=-1), at=NOW, pause=0)
        # Post 6 is not public and post 7 is outside the window; post 5 is served (feed lag).
        self.assertEqual(sorted(social.gets()), [3, 5])
        self.assertEqual((result['listed'], result['collected'], result['missing'], result['pages']), (3, 5, 2, 2))
        self.assertEqual((result['suspect'], result['visible'], result['requests']), (1, 1, 4))
        self.assertEqual(self.rows('SELECT post_id,status FROM post_visibility ORDER BY post_id'),
                         [(3, 'suspect'), (5, 'visible')])
        # Again: the suspect waits for its re-check and the served post for VISIBLE_RECHECK_HOURS.
        social.calls.clear()
        risk.scan(self.db, social, iso(6), iso(minutes=-1), at=NOW + timedelta(minutes=10), pause=0)
        self.assertEqual(social.gets(), [])
        risk.scan(self.db, social, iso(6), iso(minutes=-1), at=NOW + timedelta(hours=7), pause=0)
        self.assertEqual(social.gets(), [5])

    def test_due_suspects_are_rechecked_and_confirmed(self):
        social = FakeSocial(self.listed(3), gone={3})
        risk.scan(self.db, social, iso(6), iso(minutes=-1), at=NOW, pause=0)
        social.calls.clear()
        self.assertEqual(risk.recheck(self.db, social, at=NOW + timedelta(minutes=9), pause=0)['due'], 0)
        self.assertEqual(social.gets(), [])
        result = risk.recheck(self.db, social, at=NOW + timedelta(minutes=10), pause=0)
        self.assertEqual((result['due'], result.get('hidden')), (1, 1))
        self.assertEqual(self.rows('SELECT post_id FROM hidden_posts'), [(3,)])

    def test_a_check_without_answer_waits_before_the_next(self):
        self.observe(3, 404, iso())
        self.observe(3, 503, iso(minutes=21))
        conn = connect(self.db)
        try:
            self.assertEqual(risk.due(conn, iso(minutes=24)), [])
            self.assertEqual(risk.due(conn, iso(minutes=26)), [(3, 8001, iso(3))])
        finally:
            conn.close()

    def test_rows_recorded_on_a_first_404_are_adopted(self):
        conn = connect(self.db)
        with conn:
            conn.executemany('INSERT INTO hidden_posts(post_id,account_id,created_at,detected_at) VALUES(?,?,?,?)',
                             [(1, 8000, iso(1), iso(minutes=-60)), (2, 8000, iso(2), iso(minutes=-60)),
                              (3, 8001, iso(3), iso(minutes=-60)), (4, 6464, iso(4), iso(minutes=-5))])
        conn.close()
        social = FakeSocial([], gone={1, 4}, errors={3})
        result = risk.adopt(self.db, social, at=NOW, pause=0)
        # Post 4 was recorded 5 minutes ago: it waits for its 20 minutes.
        self.assertEqual(sorted(social.gets()), [1, 2, 3])
        self.assertEqual((result['hidden'], result['visible'], result['suspect']), (1, 1, 1))
        self.assertEqual(self.rows('SELECT post_id FROM hidden_posts ORDER BY post_id'), [(1,), (4,)])
        self.assertEqual(self.rows('SELECT post_id,status,first_404_at FROM post_visibility ORDER BY post_id'),
                         [(1, 'hidden', iso(minutes=-60)), (2, 'visible', None), (3, 'suspect', iso(minutes=-60))])

    def test_the_monitor_does_rechecks_then_the_newest_segment_then_the_full_cycle(self):
        social = FakeSocial(self.listed(3), gone={3})
        monitor = risk.RiskMonitor(self.db, client=social, pause=0)
        units = [monitor.step(NOW + timedelta(minutes=minute)) for minute in range(10)]
        # The suspect found at minute 0 gets its 5-minute shadow check before the next segment.
        self.assertEqual(units, ['fast', 'full', 'full', 'full', 'full', 'shadow', 'full', 'full', 'full', None])
        self.assertEqual(monitor.step(NOW + timedelta(minutes=10)), 'recheck')  # confirmed at 10 minutes
        self.assertIsNone(monitor.step(NOW + timedelta(minutes=10)))
        self.assertEqual(monitor.step(NOW + timedelta(minutes=20)), 'shadow')  # the 20-minute check of a confirmed post
        self.assertEqual(monitor.step(NOW + timedelta(minutes=20)), 'fast')
        self.assertIsNone(monitor.step(NOW + timedelta(minutes=21)))
        state = json.loads(self.rows('SELECT value FROM meta WHERE key=?', risk.STATE_KEY)[0][0])
        self.assertEqual(len(state['full']['segments']), 7)
        self.assertEqual(state['full']['segments'][-1]['end'], iso(42, minutes=1))
        self.assertIsNotNone(state['full']['finished_at'])
        self.assertEqual(state['recheck']['hidden'], 1)
        self.assertEqual(state['shadow']['20m_gone'], 1)
        self.assertEqual(monitor.step(NOW + timedelta(hours=3, minutes=2)), 'fast')
        self.assertEqual(monitor.step(NOW + timedelta(hours=3, minutes=2)), 'full')

    def test_the_overview_groups_accounts_and_applies_the_account_report_rule(self):
        for post_id in (1, 3, 4, 5):
            self.observe(post_id, 404, iso())
            self.observe(post_id, 404, iso(minutes=20))
        self.observe(2, 404, iso(minutes=30))
        view = risk.overview(self.db, NOW + timedelta(minutes=40))
        self.assertEqual(view['totals']['hidden'], 4)
        self.assertEqual(view['totals']['suspect'], 1)
        self.assertEqual([row['account_id'] for row in view['ours']], [6464])
        self.assertEqual([row['account_id'] for row in view['teammates']], [7000])
        others = {row['account_id']: row for row in view['others']}
        self.assertEqual((others[8000]['hidden'], others[8000]['suspect']), (1, 1))
        self.assertEqual(others[8000]['account_report'], {'ready': True, 'missing': []})
        self.assertEqual(others[8001]['account_report'], {'ready': False, 'missing': ['actions']})
        self.assertTrue(others[8000]['active'])
        self.assertEqual(view['totals']['others_report_ready'], 1)
        self.assertEqual(view['recent'][0]['post_id'], 2)
        self.assertEqual(view['recent'][0]['excerpt'], 'text 2')
        self.assertEqual(view['ours'][0]['role_state'], 'active')

    def test_suspended_and_disabled_roles_are_roles_not_teammates(self):
        # Nora is suspended by the official log, Maya's role is disabled; both are also team accounts.
        conn = connect(self.db)
        with conn:
            conn.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps({
                'a': {'id': 'a', 'name': 'Leah', 'account_id': 6464},
                'b': {'id': 'b', 'name': 'Nora', 'account_id': 6494, 'enabled': True},
                'c': {'id': 'c', 'name': 'Maya', 'account_id': 5554, 'enabled': False}}), hidden_posts.AGENTS_KEY))
            conn.execute("INSERT INTO official_activity(occurred_at,event_type,message,fingerprint,parse_status) "
                         "VALUES(?,?,?,?,?)", (iso(2), 'MODERATION', 'Moderation: your account @nora (id 6494) has '
                         'been suspended after it was reported.', 'f1', 'unparsed'))
            for post_id, author in ((20, 6494), (21, 5554)):
                self.posts[post_id] = (author, 1, 'public')
                conn.execute("INSERT INTO team_accounts(account_id,team_label,is_own,evidence,updated_at) "
                             "VALUES(?,'Agent Zero',1,'',?)", (author, iso()))
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                             (post_id, author, 'x', iso(1), iso(1), iso(1)))
        conn.close()
        for post_id in (4, 5, 20, 21):
            self.observe(post_id, 404, iso())
            self.observe(post_id, 404, iso(minutes=20))
        view = risk.overview(self.db, NOW + timedelta(minutes=30))
        states = {row['account_id']: (row['name'], row['role_state']) for row in view['ours']}
        self.assertEqual(states, {6464: ('Leah', 'active'), 6494: ('Nora', 'suspended'), 5554: ('Maya', 'disabled')})
        self.assertEqual([row['account_id'] for row in view['teammates']], [7000])
        # The execution stage's alerts still cover only the roles in use.
        conn = connect(self.db)
        try:
            self.assertEqual(set(hidden_posts.roles(conn)), {6464, 5554})
        finally:
            conn.close()

    def test_the_overview_carries_the_copy_pattern_cache(self):
        self.addCleanup(copy_patterns._CACHE.pop, self.db, None)
        self.assertEqual(risk.overview(self.db, NOW)['copy_patterns']['status'], 'pending')
        copy_patterns.refresh(self.db, NOW, npc=set())
        view = risk.overview(self.db, NOW)['copy_patterns']
        self.assertEqual((view['status'], view['posts'], view['ours']['totals']['accounts']), ('ready', 7, 1))

    def test_the_monitor_runs_the_copy_pattern_thread_alongside(self):
        monitor = risk.RiskMonitor(self.db, client=FakeSocial([]), first_run_seconds=3600)
        monitor.copy_patterns.first_run_seconds = 3600
        monitor.start()
        self.addCleanup(monitor.stop)
        self.assertTrue(monitor._thread.is_alive() and monitor.copy_patterns._thread.is_alive())
        monitor.stop()
        self.assertFalse(monitor.copy_patterns._thread.is_alive())


if __name__ == '__main__':
    unittest.main()
