"""Copy patterns: shared phrases, openings, fact copies and our hidden rates over 48 hours of posts."""
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ctn_data import copy_patterns as cp, hidden_posts
from ctn_data.db import connect, initialize

NOW = datetime(2026, 10, 24, 3, 0, tzinfo=timezone.utc)
TEMPLATE = 'The government says every eligible Kingstonian will have had a fair chance at the free jab before anything starts.'
TEMPLATE_WORDS = cp.words(TEMPLATE)
FERRY = 'The ferry ran late again this morning so the market opened without the fish.'
FACT = ('If Yes wins, the government says the rule is phased in only after every eligible Kingstonian has had a '
        'reasonable chance to be vaccinated.')


def iso(hours_ago=0.0):
    return cp._key(NOW - timedelta(hours=hours_ago))


class CopyPatternTests(unittest.TestCase):
    """6464 and 6465 are our roles, 7000 a teammate, 1500 a pre-competition NPC, 9000 a calibrated NPC."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'ctn.sqlite3')
        initialize(self.db)
        self.addCleanup(cp._CACHE.pop, self.db, None)
        self.next_id = 100
        conn = connect(self.db)
        with conn:
            conn.execute('INSERT INTO meta(key,value) VALUES(?,?)', (hidden_posts.AGENTS_KEY, json.dumps({
                'a': {'id': 'a', 'name': 'Leah', 'account_id': 6464},
                'b': {'id': 'b', 'name': 'Ben', 'account_id': 6465, 'enabled': False}})))
            for account_id in (6464, 6465, 7000):
                conn.execute("INSERT INTO team_accounts(account_id,team_label,is_own,evidence,updated_at) "
                             "VALUES(?,'Agent Zero',1,'',?)", (account_id, iso()))
            conn.execute("INSERT INTO facts(id,layer,status,claim,claim_hash,concerns_json,source_label,created_at,"
                         "updated_at) VALUES('phased','core','active',?,'h1','[]','seed',?,?)", (FACT, iso(), iso()))
            conn.execute("INSERT INTO facts(id,layer,status,claim,claim_hash,concerns_json,source_label,created_at,"
                         "updated_at) VALUES('pending_one','news','pending',?,'h2','[]','news',?,?)",
                         ('Purple elephants dance under the harbour lights every single night of the year.', iso(), iso()))
        conn.close()
        self.conn = connect(self.db)
        self.addCleanup(self.conn.close)
        # The template: 3 posts from Leah (one hidden), 2 from Ben, 1 teammate post, 1 from another team.
        self.leah = [self.post(6464, f'{TEMPLATE} {self.tail()}', hours) for hours in (1, 2, 3)]
        self.ben = [self.post(6465, f'{TEMPLATE} {self.tail()}', 4), self.post(6465, f'{TEMPLATE} {self.tail()}', 5)]
        self.mate = self.post(7000, f'{TEMPLATE} {self.tail()}', 6)
        self.other = self.post(8001, f'{TEMPLATE} {self.tail()}', 7)
        # Outside the window: does not count.
        self.post(8005, f'{TEMPLATE} {self.tail()}', 60)
        # Leah's plain post and Ben's fact copy.
        self.plain = self.post(6464, 'Morning swim was cold but worth it.', 8)
        self.ben_fact = self.post(6465, 'Worth knowing: the rule is phased in only after every eligible Kingstonian is '
                                        'covered.', 9)
        # Another team copies the fact too; 8004 matches only 6 words.
        self.fact_copy = self.post(8003, 'Read it again: the government says the rule is phased in only after people.', 10)
        self.post(8004, 'So the government says the rule is phasing in and that is that.', 11)
        # A line two NPCs and another account use.
        self.post(1500, f'{FERRY} {self.tail()}', 12)
        self.post(9000, f'@leah_patel {FERRY} {self.tail()}', 13)
        self.post(8002, f'{FERRY} https://example.com/x {self.tail()}', 14)
        # Two accounts only: not shared.
        self.post(8006, 'Purple elephants dance under the harbour lights tonight, mate.', 15)
        self.post(8007, 'purple elephants dance under the harbour lights tonight!!', 16)
        with self.conn:
            self.conn.execute("INSERT INTO post_visibility(post_id,account_id,created_at,status,first_404_at,confirmed_at,"
                              "last_checked_at,last_http,checks) VALUES(?,6464,?,'hidden',?,?,?,404,2)",
                              (self.leah[0], iso(1), iso(0.5), iso(0.2), iso(0.2)))

    def tail(self):
        """A different closing word for each post, so no phrase runs into a shared tail."""
        self.tails = getattr(self, 'tails', 0) + 1
        return f'Bye{self.tails}'

    def post(self, author, body, hours, kind='post'):
        self.next_id += 1
        with self.conn:
            if not self.conn.execute('SELECT 1 FROM accounts WHERE account_id=?', (author,)).fetchone():
                self.conn.execute('INSERT INTO accounts(account_id,username,first_seen,last_seen) VALUES(?,?,?,?)',
                                  (author, f'user{author}', iso(), iso()))
            self.conn.execute('INSERT INTO posts(post_id,author_id,kind,body,created_at,first_seen,last_seen) '
                              'VALUES(?,?,?,?,?,?,?)', (self.next_id, author, kind, body, iso(hours), iso(hours), iso(hours)))
        return self.next_id

    def view(self):
        return cp.refresh(self.db, NOW, npc={9000})

    def test_text_is_normalised_before_phrases_are_cut(self):
        self.assertEqual(cp.words("Don't @leah_p check https://x.io/a?b=1 — the Government’s plan, Tomás!"),
                         ['dont', 'check', 'the', 'governments', 'plan', 'tomas'])
        # Clauses end at punctuation and at a spaced dash; one shorter than 3 words has no frame.
        self.assertEqual(cp.frames(cp.normal('Honestly, the government says yes. And then - we vote; ok')),
                         ['the government says'])
        self.assertEqual(cp.frames(cp.normal('“Every eligible Kingstonian” — that is the line')),
                         ['every eligible kingstonian', 'that is the'])

    def test_a_phrase_three_accounts_use_is_shared_and_shown_whole(self):
        view = self.view()
        top = view['phrases'][0]
        # The 14 overlapping 6-word phrases of the template have the same accounts: one phrase.
        self.assertEqual(top['phrase'], ' '.join(TEMPLATE_WORDS))
        self.assertEqual((top['grams'], top['posts'], top['accounts'], top['members_total']), (14, 7, 4, 4))
        self.assertTrue(top['npc_free'])
        self.assertEqual({group: value['accounts'] for group, value in top['groups'].items()},
                         {'ours': 2, 'teammates': 1, 'others': 1, 'npc': 0})
        self.assertEqual(top['groups']['ours']['posts'], 5)
        # The example comes from our roles first; members are listed by their uses.
        self.assertEqual(top['examples'][0]['group'], 'ours')
        self.assertEqual([member['account_id'] for member in top['members']], [6464, 6465, 7000, 8001])
        self.assertEqual(top['members'][0]['posts'], 3)
        phrases = {row['phrase']: row for row in view['phrases']}
        ferry = next(row for text, row in phrases.items() if text.startswith('the ferry ran late'))
        self.assertEqual((ferry['accounts'], ferry['groups']['npc']['accounts'], ferry['npc_free']), (3, 2, False))
        # Two accounts are not enough, and nothing outside 48 hours counts.
        self.assertFalse(any('purple elephants' in text for text in phrases))
        self.assertEqual(view['posts'], 16)

    def test_overlapping_phrases_are_variants_and_accounts_are_never_merged(self):
        for author in (8008, 8009, 8010):
            self.post(author, f'Every eligible Kingstonian will have had a fair chance to get jabbed. {self.tail()}', 20)
        view = self.view()
        top = view['phrases'][0]
        # The part both wordings share has all 7 accounts; the template's own parts (4 accounts) and the
        # new wording's own part (3 accounts) overlap it by 4 words and show as its variants.
        self.assertEqual(top['phrase'], 'every eligible kingstonian will have had a fair chance')
        self.assertEqual((top['accounts'], top['variants']), (7, 3))
        self.assertTrue(all(row['accounts'] <= 7 for row in view['phrases']))
        # The 7-account part, the template's two own runs (one account set, not adjacent), the new
        # wording's own part, and the ferry line.
        self.assertEqual(view['shared']['phrases'], 5)

    def test_openings_frames_and_fact_copies(self):
        view = self.view()
        opening = view['openings'][0]
        self.assertEqual((opening['text'], opening['posts'], opening['accounts']), ('the government says every', 7, 4))
        frames = {row['text']: row for row in view['frames']}
        # The template posts and 8003 ('Read it again: the government says'); 8004 says 'So the government'.
        self.assertEqual(frames['the government says']['posts'], 8)
        self.assertEqual(frames['the government says']['groups']['ours']['posts'], 5)
        facts = view['facts']
        # Pending facts are never offered, so they are not compared.
        self.assertEqual((facts['facts'], facts['posts'], facts['facts_copied']), (1, 2, 1))
        row = facts['top'][0]
        self.assertEqual((row['fact_id'], row['posts'], row['accounts']), ('phased', 2, 2))
        self.assertEqual((row['groups']['ours']['posts'], row['groups']['others']['posts']), (1, 1))
        self.assertEqual(row['example']['post_id'], self.ben_fact)

    def test_accounts_and_groups(self):
        view = self.view()
        groups = view['by_group']
        self.assertEqual((groups['ours']['accounts'], groups['ours']['posts'], groups['ours']['shared_posts']), (2, 7, 5))
        self.assertEqual(groups['ours']['fact_posts'], 1)
        self.assertEqual((groups['npc']['accounts'], groups['npc']['shared_posts']), (2, 2))
        self.assertEqual(groups['npc']['shared_no_npc_posts'], 0)
        teammates = view['accounts_view']['teammates']
        self.assertEqual([(row['account_id'], row['share']) for row in teammates], [(7000, 1.0)])
        others = {row['account_id']: row for row in view['accounts_view']['others']['top']}
        self.assertEqual(others, {})  # nobody else has MIN_ACCOUNT_POSTS posts
        self.assertEqual(view['accounts_view']['others']['accounts'], 6)

    def test_our_template_posts_against_hidden_posts(self):
        ours = self.view()['ours']
        roles = {row['account_id']: row for row in ours['roles']}
        self.assertEqual((roles[6464]['name'], roles[6464]['role_state'], roles[6464]['posts']), ('Leah', 'active', 4))
        self.assertEqual((roles[6464]['template_posts'], roles[6464]['hidden']), (3, 1))
        self.assertEqual((roles[6465]['role_state'], roles[6465]['template_posts'], roles[6465]['fact_posts']),
                         ('disabled', 3, 1))
        rates = {row['key']: row for row in ours['hidden_rates']}
        self.assertEqual((rates['template']['posts'], rates['template']['hidden'], rates['template']['hidden_rate']),
                         (6, 1, round(1 / 6, 4)))
        self.assertEqual((rates['plain']['posts'], rates['plain']['hidden'], rates['plain']['hidden_rate']), (1, 0, 0.0))
        self.assertEqual(rates['c75']['posts'] + rates['c50']['posts'] + rates['c25']['posts'] + rates['c0']['posts'], 7)
        self.assertEqual(rates['c75']['hidden'], 1)
        self.assertEqual((rates['fact']['posts'], rates['shared_opening']['posts']), (1, 5))
        # Hidden ones first, then the newest.
        self.assertEqual(ours['posts'][0]['post_id'], self.leah[0])
        self.assertEqual(ours['posts'][0]['status'], 'hidden')
        self.assertEqual(ours['posts'][0]['phrase'], ' '.join(TEMPLATE_WORDS))
        self.assertEqual(ours['posts_total'], 6)
        fact_post = next(row for row in ours['posts'] if row['post_id'] == self.ben_fact)
        self.assertEqual((fact_post['fact_ids'], fact_post['phrase']), (['phased'], None))
        self.assertEqual(ours['phrases'][0]['phrase'], ' '.join(TEMPLATE_WORDS))
        self.assertEqual(ours['openings'][0]['text'], 'the government says every')

    def test_account_evidence_reads_the_cache_only(self):
        self.assertEqual(cp.account_evidence(self.db, [8001]), {})
        self.view()
        with mock.patch.object(cp, 'connect', side_effect=AssertionError('no database access')):
            found = cp.account_evidence(self.db, [8001, 8003, 424242, 'x'])
        self.assertEqual(set(found), {8001, 8003})
        evidence = found[8001]
        self.assertEqual(evidence['template_share'], 1.0)
        self.assertEqual(evidence['phrases'][0], {'phrase': ' '.join(TEMPLATE_WORDS), 'accounts': 4, 'npc_free': True,
                                                  'posts': 1})
        self.assertEqual(evidence['peers'], [6464, 6465, 7000])
        self.assertEqual(evidence['peer_phrases'], {6464: 1, 6465: 1, 7000: 1})
        self.assertEqual((evidence['copied_facts'], found[8003]['copied_facts'], found[8003]['fact_ids']), (0, 1, ['phased']))
        self.assertEqual((found[8003]['template_share'], found[8003]['phrases'], found[8003]['peers']), (0.0, [], []))
        # Phrases NPCs also use are no evidence of a group.
        ferry = cp.account_evidence(self.db, [8002])[8002]
        self.assertEqual((ferry['peers'], ferry['phrases'][0]['npc_free']), ([], False))

    def test_each_round_opens_and_closes_its_own_connection(self):
        opened = []

        def tracked(path):
            conn = connect(path)
            opened.append(conn)
            return conn

        with mock.patch.object(cp, 'connect', side_effect=tracked):
            self.view()
            self.view()
        self.assertEqual(len(opened), 2)
        for conn in opened:
            with self.assertRaises(Exception):
                conn.execute('SELECT 1')

    def test_the_cache_and_the_monitor(self):
        self.assertEqual(cp.cached(self.db)['status'], 'pending')
        monitor = cp.CopyPatternMonitor(self.db, first_run_seconds=0, interval_seconds=3600, isolated=False)
        monitor.start()
        self.addCleanup(monitor.stop)
        deadline = time.monotonic() + 30
        while cp.cached(self.db)['status'] != 'ready' and time.monotonic() < deadline:
            time.sleep(0.05)
        view = cp.cached(self.db)
        self.assertEqual(view['status'], 'ready')
        self.assertIsNone(view['last_error'])
        self.assertGreaterEqual(view['run']['cpu_seconds'], 0)
        monitor.stop()
        self.assertFalse(monitor._thread.is_alive())

    def test_a_failed_round_keeps_the_last_view_and_reports_the_error(self):
        self.view()
        monitor = cp.CopyPatternMonitor(self.db, first_run_seconds=0, interval_seconds=3600, retry_seconds=3600,
                                        isolated=False)
        with mock.patch.object(cp, '_load', side_effect=RuntimeError('database is down')):
            monitor.start()
            deadline = time.monotonic() + 30
            while not cp.cached(self.db)['last_error'] and time.monotonic() < deadline:
                time.sleep(0.05)
            monitor.stop()
        view = cp.cached(self.db)
        self.assertEqual(view['status'], 'ready')
        self.assertIn('database is down', view['last_error']['error'])

    def test_an_isolated_round_runs_in_a_child_process_and_keeps_account_ids(self):
        # The child reads the window up to its own clock: shift the posts so they are recent.
        shift = cp._now() - NOW
        with self.conn:
            for post_id, created_at in self.conn.execute('SELECT post_id,created_at FROM posts').fetchall():
                self.conn.execute('UPDATE posts SET created_at=? WHERE post_id=?',
                                  (cp._key(cp._now(created_at) + shift), post_id))
        started = []
        view = cp.refresh_isolated(self.db, started=started.append)
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].returncode, 0)
        self.assertEqual((view['status'], view['posts'], view['run']['isolated']), ('ready', 16, True))
        self.assertGreaterEqual(view['run']['cpu_seconds'], 0)
        self.assertEqual(view['phrases'][0]['phrase'], ' '.join(TEMPLATE_WORDS))
        self.assertEqual(cp.cached(self.db)['phrases'][0]['accounts'], 4)
        evidence = cp.account_evidence(self.db, [8001])[8001]
        self.assertEqual((evidence['peers'], evidence['phrases'][0]['accounts']), ([6464, 6465, 7000], 4))

    def test_a_failed_child_process_reports_its_last_line(self):
        with self.assertRaises(RuntimeError) as caught:
            cp.refresh_isolated(str(Path(self.tmp.name) / 'missing' / 'nothing.sqlite3'))
        self.assertIn('文案识别子进程失败', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
