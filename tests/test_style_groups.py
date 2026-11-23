"""Style groups (d1L): grouping, threshold, exclusions, the possible-teammate rule, evidence and the cache."""
import json
import random
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ctn_data import hidden_posts, style_groups as sg
from ctn_data.db import connect, initialize

# Real time: the child-process round reads the window before the current time.
NOW = datetime.now(timezone.utc).replace(microsecond=0)
ROLES = (6464, 6465, 6466)
TEAM = (7000, 7001)
UNREGISTERED = (3925, 7083)
OTHER_TEAM = tuple(range(8001, 8009))
VERIFIED = 9100
NPCS = tuple(range(1001, 1101))
ORDINARY = 8300
FEW_TEXTS = 8100
ONLY_OLD_OR_REPOSTS = 8200
# Our roles, teammates and the two unregistered accounts write one template; another team writes another.
OURS_TEMPLATE = ('Worth remembering: the nurses at Kingstonia Central told us {a}, and that is exactly why '
                 'I back a careful Yes on November 2 — {b}.')
OTHER_TEMPLATE = ('Honestly?? The {a} mess at Harbour Point is the whole story… who signs off on {b} '
                  'before anyone asks the stallholders!! #KeepItLocal')
OURS_FILLERS = ('the ward rosters', 'the night shift', 'the clinic queue', 'the vaccine stock', 'the triage desk',
                'the aged care beds')
OTHER_FILLERS = ('ferry timetable', 'school canteen', 'market stalls', 'bus depot', 'fish auction', 'regatta crews')
# Everyday English for the NPCs, so the centroid removes what any English text shares.
VOCABULARY = tuple('''
the a and to of in is it that for on was with he she they we you i my our your this at be have has had not but
are or by from so what all were when there can if more about up out one would get just like time people only
think know take see come could now than look day most also back after use two how work first well way even new
want because any these give us good very much need feel still last long great little right old big high small
large next early young important few public bad same able town harbour market ferry school beach coffee rain
weather morning evening dinner family friends kids dog cat garden boat fish shop street road bus park game match
team weekend holiday tired happy busy quiet loud cheap nice cold warm sunny windy late again never always maybe
really pretty sure okay honestly anyway though probably seems guess hope love hate miss wish wonder tonight today
tomorrow yesterday week month year sister brother mum dad neighbour council island bridge hill tide wind sand
'''.split())


def iso(hours_ago=0.0):
    return sg._key(NOW - timedelta(hours=hours_ago))


def npc_text(rng):
    words = [rng.choice(VOCABULARY) for _ in range(rng.randint(6, 18))]
    if rng.random() < 0.5:
        words[0] = words[0].capitalize()
    return ' '.join(words) + rng.choice(('', '.', '!', '?', ' lol', '...', ' :)'))


class StyleGroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'ctn.sqlite3')
        initialize(self.db)
        self.addCleanup(sg._STATE.clear)
        sg._STATE.clear()
        # The synthetic universe has ~50 calibration NPCs; production needs CALIBRATION_FLOOR=30 and warns below 250.
        patcher = mock.patch.multiple(sg, CALIBRATION_FLOOR=10, MIN_CALIBRATION_ACCOUNTS=10)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.next_id = 1000
        self.conn = connect(self.db)
        self.addCleanup(self.conn.close)
        rng = random.Random(7)
        with self.conn:
            self.conn.execute('INSERT INTO meta(key,value) VALUES(?,?)', (hidden_posts.AGENTS_KEY, json.dumps({
                f'r{account}': {'id': f'r{account}', 'name': f'Role{account}', 'account_id': account} for account in ROLES})))
            for account in (*ROLES, *TEAM):
                self.conn.execute("INSERT INTO team_accounts(account_id,team_label,is_own,evidence,updated_at) "
                                  "VALUES(?,'Agent Zero',1,?,?)", (account, '队友' if account in TEAM else '', iso()))
            for account in NPCS:
                for hours in range(6):
                    self.post(account, npc_text(rng), 2 + hours * 5 + account % 3)
            for account in (*ROLES, *TEAM, *UNREGISTERED):
                for hours in range(8):
                    self.post(account, self.ours_text(rng), 1 + hours * 4)
            for account in (*OTHER_TEAM, VERIFIED):
                for hours in range(8):
                    self.post(account, self.other_text(rng), 1.5 + hours * 4)
            for hours in range(8):
                self.post(ORDINARY, npc_text(rng), 3 + hours * 3)
            for hours in range(4):
                self.post(FEW_TEXTS, self.other_text(rng), 2 + hours)
            for hours in range(6):
                self.post(ONLY_OLD_OR_REPOSTS, self.other_text(rng), 50 + hours)  # outside the window
                self.post(ONLY_OLD_OR_REPOSTS, self.other_text(rng), 2 + hours, kind='repost')
            self.conn.execute('UPDATE accounts SET verified=1 WHERE account_id=?', (VERIFIED,))
            for account in OTHER_TEAM[:2]:
                post_id = self.conn.execute('SELECT MIN(post_id) FROM posts WHERE author_id=?', (account,)).fetchone()[0]
                self.conn.execute("INSERT INTO post_visibility(post_id,account_id,created_at,status,first_404_at,"
                                  "confirmed_at,last_checked_at,last_http,checks) VALUES(?,?,?,'hidden',?,?,?,404,2)",
                                  (post_id, account, iso(3), iso(2), iso(1.5), iso(1.5)))

    # Template posts carry an everyday tail, as real team posts mix a template with other content.
    @staticmethod
    def other_text(rng):
        return OTHER_TEMPLATE.format(a=rng.choice(OTHER_FILLERS), b=rng.choice(OTHER_FILLERS)) + ' ' + npc_text(rng)

    @staticmethod
    def ours_text(rng):
        return OURS_TEMPLATE.format(a=rng.choice(OURS_FILLERS), b=rng.choice(OURS_FILLERS)) + ' ' + npc_text(rng)

    def post(self, author, body, hours, kind='post'):
        """Insert one post (and its author); the caller commits."""
        self.next_id += 1
        self.conn.execute('INSERT INTO accounts(account_id,username,verified,first_seen,last_seen) VALUES(?,?,0,?,?) '
                          'ON CONFLICT(account_id) DO NOTHING', (author, f'user{author}', iso(), iso()))
        self.conn.execute('INSERT INTO posts(post_id,author_id,kind,body,created_at,first_seen,last_seen) '
                          'VALUES(?,?,?,?,?,?,?)', (self.next_id, author, kind, body, iso(hours), iso(hours), iso(hours)))
        return self.next_id

    def compute(self, **options):
        options.setdefault('npc', set())
        return sg.compute(self.conn, NOW, **options)

    def group_of(self, result, account):
        return next((group for group in result['groups']
                     if any(member['account_id'] == account for member in group['members'])), None)

    # -- text and features ------------------------------------------------------------------------------

    def test_text_is_normalised_and_hashed_stably_without_crossing_texts(self):
        self.assertEqual(sg.char_text('Ｈｉ  @leah_p see https://x.io/a?b=1\nnow'), ' Hi @ see ↗ now ')
        # One short text: 2 three-grams, 1 four-gram, no five-gram; two texts never share an n-gram across them.
        self.assertEqual(len(sg.gram_slots(['ab'])), 3)
        self.assertEqual(sorted(sg.gram_slots(['ab', 'cd']).tolist()),
                         sorted(sg.gram_slots(['ab']).tolist() + sg.gram_slots(['cd']).tolist()))
        # A fixed integer hash: the same slots in every process (Python's hash() is salted per process).
        self.assertEqual(sg.gram_slots(['ab']).tolist(), [1507775, 832928, 720134])
        self.assertTrue(all(0 <= slot < 2 ** sg.HASH_BITS for slot in sg.gram_slots([OTHER_TEMPLATE]).tolist()))
        # The background / calibration split is fixed per account.
        self.assertEqual([sg.calibration_half(account) for account in (1001, 1002, 1003, 1004)],
                         [sg.calibration_half(account) for account in (1001, 1002, 1003, 1004)])
        halves = {sg.calibration_half(account) for account in NPCS}
        self.assertEqual(halves, {0, 1})

    # -- grouping and threshold -------------------------------------------------------------------------

    def test_template_accounts_group_and_npcs_do_not(self):
        result, _ = self.compute()
        counts = result['counts']
        self.assertEqual(result['status'], 'ready')
        other = self.group_of(result, OTHER_TEAM[0])
        self.assertIsNotNone(other)
        self.assertEqual(other['group_id'], f'S{OTHER_TEAM[0]}')
        # The verified account writes the same template: in the group, never listed.
        self.assertEqual(other['size'], len(OTHER_TEAM) + 1)
        self.assertEqual({member['account_id'] for member in other['members']}, set(OTHER_TEAM))
        self.assertEqual(other['excluded']['verified'], 1)
        self.assertFalse(other['possible_teammate'])
        self.assertEqual(other['hidden_accounts'], 2)
        self.assertEqual(len(other['examples']), sg.EXAMPLES)
        # No NPC and no ordinary account is grouped.
        self.assertEqual(counts['npc_in_groups'], {'background': 0, 'calibration': 0})
        self.assertIsNone(self.group_of(result, ORDINARY))
        self.assertEqual(counts['groups'], 2)
        self.assertEqual(counts['background'] + counts['calibration'], len(NPCS))
        self.assertGreater(counts['dense_features'] + counts['light_features'], 0)

    def test_threshold_is_the_largest_calibration_pair_and_edges_clear_it(self):
        debug = {}
        result, _ = sg.analyse(sg.load(self.conn, NOW, npc=set()), _debug=debug)
        universe, similarity, calibration = debug['universe'], debug['similarity'], debug['calibration']
        expected = max(float(similarity[i, j]) for x, i in enumerate(calibration) for j in calibration[x + 1:])
        self.assertAlmostEqual(result['theta'], round(expected, 5), places=5)
        self.assertTrue(set(calibration) <= {universe.index(account) for account in NPCS if account in universe})
        position = {account: universe.index(account) for account in OTHER_TEAM}
        for a in OTHER_TEAM:
            for b in OTHER_TEAM:
                if a < b:
                    self.assertGreater(similarity[position[a], position[b]], expected)

    def test_a_calibration_pair_writing_the_template_raises_the_threshold_above_it(self):
        pair = [account for account in range(5000, 5100) if sg.calibration_half(account) == 1][:2]
        rng = random.Random(3)
        with self.conn:
            for account in pair:
                for hours in range(8):
                    # The bare template: the two resemble each other more than any tailed member pair does.
                    self.post(account, OTHER_TEMPLATE.format(a=rng.choice(OTHER_FILLERS), b=rng.choice(OTHER_FILLERS)),
                              2 + hours * 4)
        before, _ = self.compute()
        debug = {}
        after, _ = sg.analyse(sg.load(self.conn, NOW, npc=set(pair)), _debug=debug)
        # As calibration NPCs the pair sets theta, and no tailed template pair clears it any more.
        universe = debug['universe']
        self.assertAlmostEqual(after['theta'],
                               round(float(debug['similarity'][universe.index(pair[0]), universe.index(pair[1])]), 5))
        self.assertGreater(after['theta'], before['theta'])
        self.assertEqual(after['counts']['calibration'], before['counts']['calibration'] + 2)
        self.assertIsNone(self.group_of(after, OTHER_TEAM[0]))
        self.assertEqual(after['counts']['groups'], 0)

    def test_too_few_calibration_npcs_is_an_error(self):
        with mock.patch.object(sg, 'CALIBRATION_FLOOR', 1000), self.assertRaises(ValueError):
            self.compute()
        with mock.patch.object(sg, 'MIN_CALIBRATION_ACCOUNTS', 1000):
            result, _ = self.compute()
        self.assertTrue(any('校准 NPC' in warning for warning in result['warnings']))

    def test_held_out_npcs_are_counted_not_labelled(self):
        holdout = set(NPCS[::3])
        result, index = self.compute(holdout=holdout)
        counts = result['counts']
        self.assertEqual(counts['holdout'], len(holdout))
        self.assertEqual(counts['holdout_in_groups'], 0)
        self.assertEqual(counts['background'] + counts['calibration'], len(NPCS) - len(holdout))
        # Still NPCs for the listing: never shown.
        self.assertFalse(holdout & set(index['accounts']))

    # -- exclusions and the possible-teammate rule ------------------------------------------------------

    def test_ours_team_verified_npcs_and_thin_accounts_are_never_listed(self):
        result, index = self.compute()
        listed = {member['account_id'] for group in result['groups'] for member in group['members']}
        hidden_ids = {*ROLES, *TEAM, VERIFIED, *NPCS, FEW_TEXTS, ONLY_OLD_OR_REPOSTS}
        self.assertFalse(listed & hidden_ids)
        self.assertFalse(set(index['accounts']) & hidden_ids)
        self.assertFalse({peer for entry in index['accounts'].values() for peer in entry['peers']} & hidden_ids)
        evidence = sg.account_evidence  # cache not filled yet
        self.assertEqual(evidence([*ROLES, VERIFIED]), {})
        sg._store(result, index)
        self.assertEqual(sg.account_evidence([*ROLES, *TEAM, VERIFIED, NPCS[0], FEW_TEXTS, ONLY_OLD_OR_REPOSTS],
                                             now=NOW), {})
        self.assertEqual(result['counts']['accounts'], len(NPCS) + len(ROLES) + len(TEAM) + len(UNREGISTERED)
                         + len(OTHER_TEAM) + 2)

    def test_accounts_grouped_with_our_roles_are_possible_teammates(self):
        before = self.conn.execute('SELECT account_id,team_label,is_own,evidence FROM team_accounts '
                                   'ORDER BY account_id').fetchall()
        result, index = self.compute()
        ours = self.group_of(result, UNREGISTERED[0])
        self.assertEqual(ours['group_id'], f'S{UNREGISTERED[0]}')
        self.assertTrue(ours['possible_teammate'])
        self.assertEqual(ours['teammate_note'], '与我方文风同组，可能是未登记的队友')
        self.assertEqual(ours['size'], len(ROLES) + len(TEAM) + len(UNREGISTERED))
        self.assertEqual((ours['excluded']['ours'], ours['excluded']['team']), (len(ROLES), len(TEAM)))
        self.assertEqual({row['account_id'] for row in result['possible_teammates']}, set(UNREGISTERED))
        self.assertEqual(result['counts']['possible_teammates'], 2)
        self.assertEqual(result['counts']['groups_with_ours'], 1)
        sg._store(result, index)
        found = sg.account_evidence([*UNREGISTERED, OTHER_TEAM[0]], now=NOW)
        for account in UNREGISTERED:
            self.assertTrue(found[account]['possible_teammate'])
            self.assertEqual(found[account]['teammate_note'], sg.TEAMMATE_NOTE)
            self.assertEqual(found[account]['linked_to_ours'], len(ROLES) + len(TEAM))
        self.assertFalse(found[OTHER_TEAM[0]]['possible_teammate'])
        self.assertIsNone(found[OTHER_TEAM[0]]['teammate_note'])
        # team_accounts is left alone.
        after = self.conn.execute('SELECT account_id,team_label,is_own,evidence FROM team_accounts '
                                  'ORDER BY account_id').fetchall()
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])

    # -- account evidence -------------------------------------------------------------------------------

    def test_account_evidence_fits_the_blue_copy_evidence_slot(self):
        sg._store(*self.compute())
        found = sg.account_evidence([OTHER_TEAM[2], ORDINARY, 424242, 'x', None], now=NOW)
        self.assertEqual(set(found), {OTHER_TEAM[2], ORDINARY})
        entry = found[OTHER_TEAM[2]]
        self.assertEqual(set(entry), {'account_id', 'username', 'style_group', 'group_size', 'group_listed',
                                      'group_hidden_accounts', 'peers', 'examples', 'possible_teammate',
                                      'teammate_note', 'linked_to_ours', 'generated_at', 'source'})
        self.assertEqual(entry['style_group'], f'S{OTHER_TEAM[0]}')
        self.assertEqual((entry['group_size'], entry['group_listed'], entry['group_hidden_accounts']),
                         (len(OTHER_TEAM) + 1, len(OTHER_TEAM), 2))
        # Peers: the other listed members, never the account itself or the verified member; at most PEERS.
        self.assertEqual(set(entry['peers']), set(OTHER_TEAM) - {OTHER_TEAM[2]})
        self.assertLessEqual(len(entry['peers']), sg.PEERS)
        self.assertTrue(all(type(peer) is int for peer in entry['peers']))
        # Examples: representative posts of the two closest peers.
        self.assertEqual(len(entry['examples']), 2)
        self.assertEqual([example['account_id'] for example in entry['examples']], entry['peers'][:2])
        for example in entry['examples']:
            author = self.conn.execute('SELECT author_id FROM posts WHERE post_id=?', (example['post_id'],)).fetchone()[0]
            self.assertEqual(author, example['account_id'])
            self.assertIn('Harbour Point', example['excerpt'])
            self.assertLessEqual(len(example['excerpt']), sg.EXCERPT_CHARS)
        self.assertNotIn('template_share', entry)
        self.assertNotIn('phrases', entry)
        # An analysed account in no group.
        self.assertEqual(found[ORDINARY]['style_group'], None)
        self.assertEqual((found[ORDINARY]['group_size'], found[ORDINARY]['peers'], found[ORDINARY]['examples']),
                         (0, [], []))
        json.dumps(found)

    # -- cache, failures and the monitor ----------------------------------------------------------------

    def test_cache_expires_and_reports_staleness(self):
        self.assertEqual(sg.cached()['status'], 'pending')
        sg.refresh(self.db, NOW, npc=set())
        self.assertTrue(sg.account_evidence([OTHER_TEAM[0]], now=NOW + timedelta(minutes=sg.EXPIRE_MINUTES - 1)))
        self.assertFalse(sg.cached(NOW + timedelta(minutes=sg.EXPIRE_MINUTES - 1))['stale'])
        late = NOW + timedelta(minutes=sg.EXPIRE_MINUTES + 1)
        self.assertEqual(sg.account_evidence([OTHER_TEAM[0]], now=late), {})
        view = sg.cached(late)
        self.assertTrue(view['stale'])
        self.assertNotIn('accounts', view)
        self.assertEqual(view['counts']['groups'], 2)

    def test_a_failed_round_keeps_the_previous_result(self):
        monitor = sg.StyleGroupMonitor(self.db, isolated=False)
        first = sg.refresh(self.db, NOW, npc=set())
        before = sg.account_evidence([OTHER_TEAM[0]], now=NOW)
        self.assertTrue(before)
        with mock.patch.object(sg, 'analyse', side_effect=RuntimeError('boom')), self.assertLogs(sg.LOGGER, 'ERROR'):
            self.assertIsNone(monitor.run_once())
        view = sg.cached(NOW)
        self.assertIn('boom', view['last_error']['error'])
        self.assertEqual(view['generated_at'], first['generated_at'])
        self.assertEqual(sg.account_evidence([OTHER_TEAM[0]], now=NOW), before)
        # A database error while reading is the same: the old result stays.
        with mock.patch.object(sg, 'connect', side_effect=OSError('database down')), self.assertLogs(sg.LOGGER, 'ERROR'):
            self.assertIsNone(monitor.run_once())
        self.assertIn('database down', sg.cached(NOW)['last_error']['error'])
        self.assertEqual(sg.account_evidence([OTHER_TEAM[0]], now=NOW), before)
        # The next good round replaces the result and clears the error.
        self.assertIsNotNone(monitor.run_once())
        self.assertIsNone(sg.cached()['last_error'])

    def test_each_round_opens_a_fresh_connection_and_closes_it_before_computing(self):
        opened, closed, computing = [], [], []
        real_connect, real_analyse = sg.connect, sg.analyse

        class Tracked:
            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def close(self):
                closed.append(self)
                self._conn.close()

        def tracked(path):
            conn = Tracked(real_connect(path))
            opened.append(conn)
            return conn

        def analyse(data, **options):
            computing.append(len(closed))
            return real_analyse(data, **options)
        with mock.patch.object(sg, 'connect', side_effect=tracked), mock.patch.object(sg, 'analyse', side_effect=analyse):
            sg.refresh(self.db, NOW, npc=set())
            sg.refresh(self.db, NOW, npc=set())
        self.assertEqual(len(opened), 2)
        self.assertEqual(closed, opened)
        self.assertEqual(computing, [1, 2])

    def test_monitor_thread_runs_a_round_and_stops(self):
        done = threading.Event()
        rounds = []

        def fake_refresh(db_path):
            rounds.append(db_path)
            done.set()
            return {'counts': {'accounts': 0, 'groups': 0}, 'theta': 0.2, 'run': {'cpu_seconds': 0.0}}
        monitor = sg.StyleGroupMonitor(self.db, first_run_seconds=0, interval_seconds=3600, isolated=False)
        with mock.patch.object(sg, 'refresh', side_effect=fake_refresh):
            monitor.start()
            self.assertTrue(done.wait(10))
            monitor.stop()
        self.assertEqual(rounds, [self.db])
        self.assertFalse(monitor._thread.is_alive())

    def test_isolated_round_runs_in_a_child_process(self):
        result = sg.refresh_isolated(self.db)
        self.assertTrue(result['run']['isolated'])
        self.assertGreater(result['run']['cpu_seconds'], 0)
        self.assertEqual(result['counts']['groups'], 2)
        found = sg.account_evidence([UNREGISTERED[1], OTHER_TEAM[1]], now=datetime.now(timezone.utc))
        self.assertTrue(found[UNREGISTERED[1]]['possible_teammate'])
        self.assertEqual(found[OTHER_TEAM[1]]['style_group'], f'S{OTHER_TEAM[0]}')
        self.assertTrue(all(type(peer) is int for peer in found[OTHER_TEAM[1]]['peers']))


if __name__ == '__main__':
    unittest.main()
