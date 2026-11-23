"""Blue-team report candidates: ranking only, read-only, never reports."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ctn_data import agent_targeting, blue_reports
from ctn_data.db import SCHEMA, SQLITE_DERIVED, configure_sqlite, connect, initialize

NOW = datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)
TEXT_A = 'Keeping thirty ICU beds free is crucial for everyone on the island. Vote yes!'
TEXT_B = 'Sensible precautions now stop Dolphin Flu before it spreads across Kingstonia.'
TEXT_D = 'Free vaccines and clear exemptions protect elders while shops stay open safely.'
CHAIN = "Copy-paste this: let's save our friends and family from H2D2, vote yes on November 2!"


def stamp(at):
    return at.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def memory_db():
    """In-memory SQLite database built from the project's schema helpers."""
    conn = configure_sqlite(sqlite3.connect(':memory:'))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executescript(SQLITE_DERIVED)
    return conn


class Builder:
    def __init__(self, conn, now):
        self.conn, self.now, self.next_id = conn, now, 1000

    def post(self, author, minutes_ago, body='text', parent=None, seconds=0, repost_of=None, post_id=None):
        if post_id is None:
            self.next_id += 1
            post_id = self.next_id
        at = stamp(self.now - timedelta(minutes=minutes_ago) + timedelta(seconds=seconds))
        kind = 'repost' if repost_of else 'reply' if parent else 'post'
        self.conn.execute('INSERT INTO posts(post_id,author_id,kind,body,reply_to_post_id,repost_of_post_id,'
                          'visibility,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (post_id, author, kind, body, parent, repost_of, 'public', at, at, at))
        return post_id

    def account(self, account_id, verified=0):
        at = stamp(self.now)
        self.conn.execute('INSERT INTO accounts(account_id,username,verified,private,first_seen,last_seen) '
                          'VALUES(?,?,?,0,?,?)', (account_id, f'user{account_id}', verified, at, at))

    def bot_replies(self, bot, minutes_ago, answered=8, latency=20):
        """Ten citizens reply to the bot's root post; the bot answers ``answered`` of them."""
        root = self.post(bot, minutes_ago + 10, 'Our clinics need this, here is why.')
        for index in range(10):
            citizen = 2001 + index
            reply = self.post(citizen, minutes_ago, f'Why do you think so? ({index})', parent=root)
            if index < answered:
                self.post(bot, minutes_ago, f'Because it keeps clinics open ({index}).', parent=reply, seconds=latency)
        return root


def scenario(conn, now=NOW):
    """A bot cluster, a citizen chain message and every excluded kind of post."""
    b = Builder(conn, now)
    with conn:
        for account in (801, 802, 803, 901, 902, 903, 5554, 7001, 7002, *range(2001, 2011)):
            b.account(account)
        b.account(8, verified=1)
        conn.execute("INSERT INTO team_accounts VALUES(7001,'Agent Zero',1,'test',?)", (stamp(now),))
        conn.execute('INSERT INTO meta(key,value) VALUES(?,?)',
                     ('agent_control_v1:agents', json.dumps({'x': {'id': 'x', 'account_id': 7002}})))
        ids = {}
        # Same text within a minute (co-timed) by 801-803 and 850 (no profile).
        ids['a801'] = b.post(801, 30, TEXT_A)
        ids['a802'] = b.post(802, 30, TEXT_A, seconds=20)
        ids['a803'] = b.post(803, 30, TEXT_A, seconds=40)
        ids['a850'] = b.post(850, 30, TEXT_A, seconds=50)
        # Excluded authors posting the same text: verified, own (constant, team_accounts, Agent store).
        for author in (8, 5554, 7001, 7002):
            ids[f'a{author}'] = b.post(author, 30, TEXT_A, seconds=30)
        # Shared but hours apart: not co-timed.
        ids['b801'] = b.post(801, 200, TEXT_B)
        b.post(802, 400, TEXT_B)
        b.post(803, 600, TEXT_B)
        for minutes in range(5):
            b.post(802, 60 + minutes * 90, TEXT_D)
        ids['root801'] = b.bot_replies(801, 120)
        # Chain message by citizens: weak, never clustered.
        for offset, author in enumerate((901, 902, 903)):
            ids[f'chain{author}'] = b.post(author, 50, CHAIN, seconds=offset * 10)
        # Exclusions: older than 24 h, repost, hidden (counter request answered 404).
        ids['old801'] = b.post(801, 30 * 60, TEXT_A)
        ids['repost801'] = b.post(801, 20, '', repost_of=ids['a802'])
        conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)',
                     (f"counter_attempt:{ids['a803']}", json.dumps({'status': 'failed', 'http_status': 404}),
                      stamp(now - timedelta(minutes=5))))
    return ids


class CandidateTest(unittest.TestCase):
    def setUp(self):
        self.conn = memory_db()
        self.ids = scenario(self.conn)
        self.items = blue_reports.candidates(self.conn, now=NOW, limit=50)
        self.by_post = {item['post_id']: item for item in self.items}

    def tearDown(self):
        self.conn.close()

    def test_top_candidate_has_corroborated_evidence(self):
        top = self.items[0]
        self.assertEqual(top['post_id'], self.ids['a801'])
        rules = {e['rule'] for e in top['evidence']}
        self.assertTrue({'behaviour_bot_rule', 'fast_answers', 'shared_text_post', 'cotimed_post'} <= rules)
        self.assertGreaterEqual(top['p'], 0.95)
        self.assertGreaterEqual(top['support'], 3)
        self.assertAlmostEqual(top['ev'], blue_reports.expected_value(top['p'], 40), delta=0.01)
        self.assertLessEqual(len(top['excerpt']), blue_reports.EXCERPT_CHARS)
        self.assertIs(top['verified'], False)

    def test_exclusions(self):
        excluded = {self.ids[key] for key in ('a8', 'a5554', 'a7001', 'a7002', 'old801', 'repost801', 'a803')}
        self.assertFalse(excluded & set(self.by_post))
        authors = {item['author_id'] for item in self.items}
        self.assertFalse(authors & {8, 5554, 7001, 7002, 6464, *range(2001, 2011)})

    def test_shared_text_counts_exclude_own_and_verified(self):
        shared = next(e for e in self.by_post[self.ids['a802']]['evidence'] if e['rule'] == 'shared_text_post')
        self.assertEqual(shared['accounts'], 4)  # 801, 802, 803, 850

    def test_clusters_group_shared_texts_but_not_chain_messages(self):
        cluster = {self.by_post[self.ids[key]]['cluster_id'] for key in ('a801', 'a802', 'a850')}
        self.assertEqual(len(cluster), 1)
        self.assertEqual(self.by_post[self.ids['a801']]['cluster_size'], 4)
        chains = [self.by_post[self.ids[f'chain{a}']] for a in (901, 902, 903)]
        self.assertEqual({c['cluster_id'] for c in chains}, {'a901', 'a902', 'a903'})
        for chain in chains:
            self.assertEqual([e['rule'] for e in chain['evidence']], ['chain_text'])
            self.assertLess(chain['p'], 0.2)

    def test_single_family_never_reaches_threshold(self):
        p, support = blue_reports.precision(['shared_text_post', 'shared_text_author'])
        self.assertEqual(support, 1)
        self.assertLessEqual(p, blue_reports.SINGLE_FAMILY_CAP)
        p, _ = blue_reports.precision(['behaviour_bot_rule', 'deep_threads'])
        self.assertLessEqual(p, blue_reports.SINGLE_FAMILY_CAP)

    def test_per_author_limit_and_ordering(self):
        per_author = {}
        for item in self.items:
            per_author[item['author_id']] = per_author.get(item['author_id'], 0) + 1
        self.assertLessEqual(max(per_author.values()), 2)
        self.assertEqual([i['p'] for i in self.items], sorted((i['p'] for i in self.items), reverse=True))
        self.assertEqual(self.by_post[self.ids['b801']]['author_id'], 801)

    def test_side_comes_from_the_whole_cluster(self):
        # 801-803 and 850 posted "Vote yes!" four times; each chain author posted one Yes text.
        cluster = self.by_post[self.ids['a850']]
        self.assertEqual((cluster['side'], cluster['side_counts']), ('yes', {'yes': 4, 'no': 0}))
        self.assertEqual(self.by_post[self.ids['b801']]['side'], 'yes')
        self.assertEqual(self.by_post[self.ids['chain901']]['side'], 'unknown')

    def test_side_rule(self):
        side = lambda yes, no: blue_reports._side({'yes': yes, 'no': no})
        self.assertEqual([side(0, 1), side(0, 2), side(1, 2), side(2, 1), side(2, 3), side(3, 0)],
                         ['unknown', 'no', 'no', 'yes', 'mixed', 'yes'])

    def test_unknown_profile_is_marked(self):
        self.assertIsNone(self.by_post[self.ids['a850']]['verified'])

    def test_expected_value_formula(self):
        # 25*(0.9*0.9 + 0.1*0.1) - 40*(0.9*0.1 + 0.1*0.9) = 20.5 - 7.2
        self.assertAlmostEqual(blue_reports.expected_value(0.9, 40), 13.3)
        self.assertAlmostEqual(blue_reports.expected_value(0.5, 0), 12.5)

    def test_candidates_rejects_bad_limit(self):
        with self.assertRaises(ValueError):
            blue_reports.candidates(self.conn, now=NOW, limit=0)


class ReadOnlyTest(unittest.TestCase):
    def test_borrowed_sqlite_connection_cannot_write_and_is_restored(self):
        conn = memory_db()
        try:
            with blue_reports.read_only(conn) as ro:
                with self.assertRaises(sqlite3.OperationalError):
                    ro.execute("INSERT INTO meta(key,value) VALUES('x','y')")
            self.assertEqual(conn.execute('PRAGMA query_only').fetchone()[0], 0)
            self.assertFalse(conn.in_transaction)
            with conn:
                conn.execute("INSERT INTO meta(key,value) VALUES('x','y')")
            conn.execute('BEGIN')
            with self.assertRaises(ValueError):
                with blue_reports.read_only(conn):
                    pass
        finally:
            conn.close()

    def test_candidates_leave_database_unchanged(self):
        conn = memory_db()
        try:
            scenario(conn)
            before = conn.total_changes
            blue_reports.candidates(conn, now=NOW)
            self.assertEqual(conn.total_changes, before)
        finally:
            conn.close()


class DailyPlanTest(unittest.TestCase):
    @staticmethod
    def item(post_id, p, cluster='c1', author=None, verified=False):
        return {'post_id': post_id, 'author_id': author or post_id, 'cluster_id': cluster, 'p': p,
                'ev': round(blue_reports.expected_value(p), 2), 'verified': verified}

    def test_cluster_cap_counts_reports_already_made_today(self):
        items = [self.item(i, 0.95) for i in range(1, 8)] + [self.item(20, 0.9, 'c2')]
        plan = blue_reports.daily_plan(items, {'c1': 3}, now=NOW)
        planned = [i['post_id'] for i in plan['items']]
        self.assertEqual(planned, [1, 2, 20])
        self.assertEqual([s['reason'] for s in plan['skipped']], ['cluster_daily_cap'] * 5)
        self.assertEqual(plan['day'], '2026-10-22')
        self.assertEqual(plan['stop_reason'], 'exhausted')

    def test_record_list_and_stop_at_threshold(self):
        items = [self.item(1, 0.99), self.item(2, 0.97, author=1), self.item(3, 0.96, author=1),
                 self.item(4, 0.95, verified=None), self.item(5, 0.84), self.item(6, 0.99, 'c9')]
        already = [{'post_id': 6, 'cluster_id': 'c9'}]
        plan = blue_reports.daily_plan(items, already)
        self.assertEqual([i['post_id'] for i in plan['items']], [1, 2])
        self.assertEqual({s['post_id']: s['reason'] for s in plan['skipped']},
                         {6: 'already_reported', 3: 'author_daily_cap', 4: 'verified_or_unknown'})
        self.assertEqual(plan['stop_reason'], 'below_threshold')
        self.assertNotIn(5, [s['post_id'] for s in plan['skipped']])

    def test_no_side_comes_first_above_the_threshold(self):
        items = [dict(self.item(1, 0.99, 'c1'), side='yes'), dict(self.item(2, 0.9, 'c2'), side='no'),
                 dict(self.item(3, 0.95, 'c3'), side='mixed'), self.item(4, 0.97, 'c4'),
                 dict(self.item(5, 0.8, 'c5'), side='no')]
        plan = blue_reports.daily_plan(items, None)
        self.assertEqual([i['post_id'] for i in plan['items']], [2, 4, 3, 1])
        self.assertEqual(plan['stop_reason'], 'below_threshold')

    def test_rate_limit_slots_and_negative_ev(self):
        items = [self.item(i, 0.99, f'c{i}') for i in range(1, 8)]
        plan = blue_reports.daily_plan(items, None)
        self.assertEqual([i['slot_minute'] for i in plan['items']], [0, 0, 0, 1, 1, 1, 2])
        losing = dict(self.item(9, 0.9, 'c9'), ev=-1.0)
        self.assertEqual(blue_reports.daily_plan([losing], {})['skipped'][0]['reason'], 'non_positive_ev')

    def test_plan_from_scenario_reports_only_corroborated_posts(self):
        conn = memory_db()
        try:
            ids = scenario(conn)
            plan = blue_reports.daily_plan(blue_reports.candidates(conn, now=NOW), {}, now=NOW)
        finally:
            conn.close()
        self.assertEqual({i['post_id'] for i in plan['items']}, {ids['a801'], ids['b801']})
        self.assertTrue(all(i['p'] >= 0.85 for i in plan['items']))

    def test_sydney_day(self):
        self.assertEqual(blue_reports.sydney_day(datetime(2026, 10, 21, 12, 59, tzinfo=timezone.utc)), '2026-10-21')
        self.assertEqual(blue_reports.sydney_day('2026-10-21T13:00:00Z'), '2026-10-22')


class AgentTargetingConsistencyTest(unittest.TestCase):
    """The bot rule and duplicate texts agree with agent_targeting on a file database."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'ctn.sqlite3')
        initialize(self.db)
        self.now = datetime.now(timezone.utc)
        conn = connect(self.db)
        try:
            scenario(conn, self.now)
            b = Builder(conn, self.now)
            b.next_id = 5000
            with conn:
                b.bot_replies(804, 300, answered=2, latency=600)  # citizen-like: 2 of 10 answered
        finally:
            conn.close()
        agent_targeting._cache.clear()

    def tearDown(self):
        agent_targeting._cache.clear()
        self.tmp.cleanup()

    def test_matches_agent_targeting(self):
        until = blue_reports._key(self.now + timedelta(seconds=5))
        since = blue_reports._key(self.now - timedelta(hours=agent_targeting.BEHAVIOR_WINDOW_HOURS))
        with blue_reports.read_only(self.db) as conn:
            stats, _, _ = blue_reports._behaviour(conn, since, until)
            posts = blue_reports._recent_posts(conn, blue_reports._key(self.now - timedelta(hours=24)), until)
        bots = {author for author, item in stats.items() if item['bot_rule']}
        self.assertEqual(bots, set(agent_targeting.behavior_bots(self.db)))
        self.assertIn(801, bots)
        self.assertNotIn(804, bots)
        texts = blue_reports._texts(posts)
        shared = {author for author, signatures in texts['shared_authors'].items() if signatures}
        self.assertEqual(shared, set(agent_targeting.duplicate_authors(self.db)))

    def test_path_mode(self):
        items = blue_reports.candidates(self.db, now=self.now + timedelta(seconds=5))
        self.assertEqual(items[0]['author_id'], 801)


if __name__ == '__main__':
    unittest.main()
