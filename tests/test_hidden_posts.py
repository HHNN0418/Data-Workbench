"""D90: hidden posts of our roles are found from the public listing and shown as suspension risk."""
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from ctn_data import hidden_posts, pipeline, risk_assessment
from ctn_data.db import connect, initialize

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')


class NotFound(Exception):
    status = 404


class Page(list):
    def __init__(self, items, next_cursor=None):
        super().__init__(items)
        self.next_cursor = next_cursor


class FakeSocial:
    def __init__(self, served, hidden):
        self.served, self.hidden, self.calls = served, set(hidden), []

    def account_posts(self, account_id, limit=50, cursor=None):
        self.calls.append(('list', account_id, cursor))
        rows = [{'post_id': pid, 'created_at': at} for pid, at in self.served.get(account_id, [])]
        start = int(cursor or 0)
        page = rows[start:start + 2]
        return Page(page, str(start + 2) if start + 2 < len(rows) else None)

    def get_post(self, post_id):
        self.calls.append(('get', post_id))
        if post_id in self.hidden:
            raise NotFound()
        return {'post_id': post_id}


class HiddenPostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'ctn.sqlite3')
        initialize(self.db)
        agents = {'a': {'id': 'a', 'name': 'Leah', 'account_id': 6464},
                  'b': {'id': 'b', 'name': 'Tomás', 'account_id': 10243},
                  'c': {'id': 'c', 'name': 'Maya', 'account_id': 5554, 'account_suspended': {'at': iso(30)}}}
        conn = connect(self.db)
        with conn:
            conn.execute('INSERT INTO meta(key,value) VALUES(?,?)', (hidden_posts.AGENTS_KEY, json.dumps(agents)))
            for post_id, author, hours in ((1, 6464, 5), (2, 6464, 6), (3, 6464, 7), (4, 6464, 0.1), (5, 6464, 60),
                                           (6, 10243, 3)):
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                             (post_id, author, 'x', iso(hours), iso(hours), iso(hours)))
            for index in range(120):  # Leah has more than 100 actions
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                             (1000 + index, 6464, 'old', iso(100), iso(100), iso(100)))
        conn.close()

    def visibility(self):
        conn = connect(self.db)
        try:
            return {row['post_id']: (row['status'], row['first_404_at']) for row in conn.execute(
                'SELECT post_id,status,first_404_at FROM post_visibility')}, {
                row['post_id'] for row in conn.execute('SELECT post_id FROM hidden_posts')}
        finally:
            conn.close()

    def test_a_first_404_is_a_suspect_until_the_recheck_confirms_it(self):
        # Post 2 is missing from the listing but still served (listing lag): visible.
        # Post 4 is too new to judge; post 5 is outside the window.
        social = FakeSocial({6464: [(1, iso(5)), (3, iso(7))], 10243: [(6, iso(3))]}, hidden={3})
        social.served[6464] = [(1, iso(5))]
        found = hidden_posts.run_once(self.db, social, now=NOW, pause=0)
        self.assertEqual(found, {6464: [3]})
        self.assertNotIn(('list', 5554, None), social.calls)  # suspended roles are skipped
        self.assertNotIn(('get', 4), social.calls)
        self.assertNotIn(('get', 5), social.calls)
        states, hidden = self.visibility()
        self.assertEqual({post: state for post, (state, _) in states.items()}, {2: 'visible', 3: 'suspect'})
        self.assertEqual(hidden, set())  # not counted before the 20-minute recheck
        again = hidden_posts.run_once(self.db, social, now=NOW, pause=0)
        self.assertEqual(again, {})
        self.assertNotIn(('get', 3), social.calls[len(social.calls) - 3:])  # left to risk_assessment's recheck
        later = (datetime.fromisoformat(states[3][1].replace('Z', '+00:00')) + timedelta(minutes=21)
                 ).isoformat().replace('+00:00', 'Z')
        conn = connect(self.db)
        try:
            with conn:
                self.assertEqual(risk_assessment.observe(conn, 3, 6464, iso(7), 404, later), 'hidden')
        finally:
            conn.close()
        self.assertEqual(self.visibility()[1], {3})

    def test_posts_our_roles_deleted_are_not_suspects(self):
        conn = connect(self.db)
        with conn:
            self.audit(conn, 'b', 1, 'delete_post', target_post_id=6)
        conn.close()
        social = FakeSocial({6464: [(1, iso(5)), (2, iso(6)), (3, iso(7))], 10243: []}, hidden={6})
        self.assertEqual(hidden_posts.run_once(self.db, social, now=NOW, pause=0), {})
        self.assertNotIn(6, self.visibility()[0])

    def test_risks_name_accounts_near_suspension(self):
        conn = connect(self.db)
        with conn:
            conn.executemany('INSERT INTO hidden_posts(post_id,account_id,created_at,detected_at) VALUES(?,?,?,?)',
                             [(1, 6464, iso(5), iso(1)), (6, 10243, iso(3), iso(1)), (2, 10243, iso(4), iso(1)),
                              (7, 9001, iso(5), iso(1)), (8, 9001, iso(5), iso(1)), (9, 9001, iso(5), iso(1))])
            # A banned role: the role row does not say so, the official log does.
            agents = json.loads(conn.execute('SELECT value FROM meta WHERE key=?', (hidden_posts.AGENTS_KEY,))
                                .fetchone()['value'])
            agents['d'] = {'id': 'd', 'name': 'Peg', 'account_id': 9001}
            conn.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(agents), hidden_posts.AGENTS_KEY))
            conn.execute("INSERT INTO official_activity(occurred_at,event_type,message,fingerprint,parse_status) "
                         "VALUES(?,?,?,?,?)", (iso(2), 'MODERATION', 'Moderation: your account @peg (id 9001) has '
                         'been suspended after 3 of its posts were hidden as inauthentic.', 'f1', 'unparsed'))
        conn.close()
        conn = connect(self.db)
        try:
            alerts, counts = hidden_posts.risks(conn)
        finally:
            conn.close()
        self.assertEqual(counts, {'6464': 1, '10243': 2})
        self.assertEqual(alerts[0], 'Tomás（10243）已有 2 条帖子被隐藏，再被隐藏 1 条就可能封号')
        self.assertIn('Leah（6464）已有 1 条帖子被隐藏、操作 125 次', alerts[1])
        conn = connect(self.db)
        try:
            found, _ = hidden_posts.risk_accounts(conn)
        finally:
            conn.close()
        self.assertEqual([(r['account_id'], r['name'], r['hidden'], r['level']) for r in found],
                         [(10243, 'Tomás', 2, 'suspend_next'), (6464, 'Leah', 1, 'report')])
        self.assertEqual([r['text'] for r in found], alerts)
        # The sidebar's Agent 执行 dot carries the risks; the outcome stage no longer does.
        stages = {stage['key']: stage for stage in pipeline.build(self.db, now=NOW)['stages']}
        execute, outcome = stages['execute'], stages['outcome']
        self.assertEqual(execute['metrics']['alerts'], alerts)
        self.assertEqual(execute['metrics']['risk_accounts'], found)
        self.assertTrue(execute['headline'].startswith('⚠ Tomás'))
        self.assertIn(execute['status'], ('warn', 'bad'))
        self.assertNotIn('alerts', outcome['metrics'])
        self.assertNotIn('⚠', outcome['headline'])

    def audit(self, conn, agent_id, hours_ago, action, **details):
        conn.execute('INSERT INTO agent_audit_events(id,agent_id,at,kind,message,draft_id,details_json) '
                     'VALUES(?,?,?,?,?,?,?)', (str(uuid.uuid4()), agent_id, iso(hours_ago), 'published', 'ok', None,
                                               json.dumps({'action': action, **details})))

    def test_actions_come_from_our_records_and_self_deleted_posts_are_left_out(self):
        conn = connect(self.db)
        with conn:
            # Tomás: one collected post, but 110 likes and follows in our own records.
            for index in range(110):
                self.audit(conn, 'b', 1 + index / 1000, 'like' if index % 2 else 'follow')
            self.audit(conn, 'b', 2, 'like', already_applied=True)   # only confirmed an existing like
            self.audit(conn, 'b', 50, 'like')                        # before the account's first post
            # Leah deleted post 1 herself: the 404 is not a hide.
            self.audit(conn, 'a', 4, 'delete_post', target_post_id=1)
            conn.executemany('INSERT INTO hidden_posts(post_id,account_id,created_at,detected_at) VALUES(?,?,?,?)',
                             [(6, 10243, iso(3), iso(1)), (1, 6464, iso(5), iso(1))])
        conn.close()
        conn = connect(self.db)
        try:
            found, counts = hidden_posts.risk_accounts(conn)
        finally:
            conn.close()
        self.assertEqual(counts, {'10243': 1})
        self.assertEqual([(r['account_id'], r['actions'], r['level']) for r in found], [(10243, 110, 'report')])

    def test_two_hides_and_a_hundred_actions_name_both_risks(self):
        conn = connect(self.db)
        with conn:
            conn.executemany('INSERT INTO hidden_posts(post_id,account_id,created_at,detected_at) VALUES(?,?,?,?)',
                             [(1, 6464, iso(5), iso(1)), (2, 6464, iso(6), iso(1)), (3, 6464, iso(7), iso(1))])
        conn.close()
        conn = connect(self.db)
        try:
            alerts, _ = hidden_posts.risks(conn)
            conn.execute('DELETE FROM hidden_posts WHERE post_id=3')
            two, _ = hidden_posts.risks(conn)
        finally:
            conn.close()
        self.assertIn('已到 3 条封号线', alerts[0])
        self.assertEqual(two, ['Leah（6464）已有 2 条帖子被隐藏，再被隐藏 1 条就可能封号；操作 125 次，账号也可能被直接举报'])

    def test_no_risk_leaves_the_execute_stage_alone(self):
        stage = next(s for s in pipeline.build(self.db, now=NOW)['stages'] if s['key'] == 'execute')
        self.assertEqual(stage['metrics']['risk_accounts'], [])
        self.assertNotIn('⚠', stage['headline'])


if __name__ == '__main__':
    unittest.main()
