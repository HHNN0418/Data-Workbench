"""Persuasion targeting: stance/concern classification and source ordering."""
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ctn_data import agent_targeting
from ctn_data.db import connect, initialize


def iso(delta_minutes=0):
    value = datetime.now(timezone.utc) - timedelta(minutes=delta_minutes)
    return value.isoformat(timespec='seconds').replace('+00:00', 'Z')


class ClassifyTest(unittest.TestCase):
    def test_stances(self):
        cases = {
            "I'm not sure how to vote on the H2D2 referendum, worried about side effects.": 'undecided',
            'Vote No. This mandate creates a two-tier society and is a power grab.': 'no',
            'Mandate now. Protect Kingstonia. Voting yes on November 2!': 'yes',
            'Lovely sunset over Crown Harbour tonight.': 'unrelated',
            'The referendum is in two weeks.': 'undecided',
        }
        for text, stance in cases.items():
            with self.subTest(text=text):
                self.assertEqual(agent_targeting.classify(text)['stance'], stance)

    def test_concerns(self):
        found = agent_targeting.classify(
            'Will the national ID card track my health? And what about the kids at school?')['concerns']
        self.assertIn('id_privacy', found)
        self.assertIn('children', found)


class AnnotateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'ctn.sqlite3')
        initialize(self.db)
        conn = connect(self.db)
        with conn:
            copy = 'Let us save our friends and family from H2D2, vote yes on November 2 everyone!'
            for post_id, author in ((1, 501), (2, 502), (3, 503)):
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                             (post_id, author, copy, iso(30), iso(30), iso(30)))
            conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                         (10, 700, 'Not sure about the mandate.', iso(60), iso(60), iso(60)))
        conn.close()
        agent_targeting._cache.clear()

    def tearDown(self):
        agent_targeting._cache.clear()
        self.tmp.cleanup()

    def test_scores_and_exclusions(self):
        data = {'agents': {'a': {'account_id': 900}},
                'drafts': {'d': {'state': 'published', 'action': 'reply', 'target_post_id': 10,
                                 'published_at': iso(10)}}}
        sources = {
            'post:20': {'id': 'post:20', 'kind': 'post', 'author_id': 600, 'text': "I'm undecided on the vaccine rule."},
            'post:21': {'id': 'post:21', 'kind': 'post', 'author_id': 700, 'text': "Still unsure about the mandate."},
            'post:22': {'id': 'post:22', 'kind': 'post', 'author_id': 501, 'text': 'Vote no, two-tier society.'},
            'post:23': {'id': 'post:23', 'kind': 'post', 'author_id': 900, 'text': 'Vote yes.'},
            'post:24': {'id': 'post:24', 'kind': 'post', 'author_id': 601, 'text': 'Vote yes!', 'notification_ids': [5]},
            'news:1': {'id': 'news:1', 'kind': 'news', 'text': 'Herald story'},
        }
        result = agent_targeting.annotate(self.db, data, {'id': 'a'}, sources, time.time())
        self.assertNotIn('target', result['news:1'])
        self.assertEqual(result['post:20']['target']['score'], 3)
        self.assertIn('recently_targeted', result['post:21']['target']['notes'])
        self.assertIn('duplicate_text', result['post:22']['target']['notes'])
        self.assertNotIn('suspected_bot', result['post:22']['target']['notes'])
        self.assertEqual(result['post:23']['target']['stance'], 'own')
        self.assertIn('engaged_with_us', result['post:24']['target']['notes'])
        ranked = sorted(result.values(), key=agent_targeting.score, reverse=True)
        self.assertEqual(ranked[0]['id'], 'post:20')

    def test_confirmed_npcs_rank_first(self):
        sources = {
            'post:20': {'id': 'post:20', 'kind': 'post', 'author_id': 600, 'text': "I'm undecided on the vaccine rule."},
            'post:21': {'id': 'post:21', 'kind': 'post', 'author_id': 650, 'text': "I'm undecided on the vaccine rule."},
            'post:22': {'id': 'post:22', 'kind': 'post', 'author_id': 900, 'text': "I'm undecided on the vaccine rule."},
        }
        with patch('ctn_data.npc_calibration.certain_npcs', return_value=frozenset({650, 900})):
            result = agent_targeting.annotate(self.db, {'agents': {'a': {'account_id': 900}}, 'drafts': {}},
                                              {'id': 'a'}, sources, time.time())
        self.assertEqual(result['post:21']['target']['score'],
                         result['post:20']['target']['score'] + agent_targeting.CONFIRMED_NPC_BONUS)
        self.assertIn('confirmed_npc', result['post:21']['target']['notes'])
        self.assertNotIn('confirmed_npc', result['post:22']['target']['notes'])  # our own account

    def test_vote_labels_favour_persuadable_and_avoid_firm_no(self):
        sources = {f'post:{n}': {'id': f'post:{n}', 'kind': 'post', 'author_id': n,
                                 'text': "I'm undecided on the vaccine rule."} for n in (600, 601, 602, 603)}
        votes = {601: {'choice': 'unsure', 'p': {'yes': 0.2, 'no': 0.2, 'unsure': 0.6}},
                 602: {'choice': 'no', 'p': {'yes': 0.05, 'no': 0.9, 'unsure': 0.05}},
                 603: {'choice': 'no', 'p': {'yes': 0.1, 'no': 0.55, 'unsure': 0.35}}}
        with patch('ctn_data.vote_tracker.latest_labels', return_value=votes):
            result = agent_targeting.annotate(self.db, {'agents': {}, 'drafts': {}}, {'id': 'a'}, sources, time.time())
        base = result['post:600']['target']['score']
        self.assertEqual(result['post:601']['target']['score'], base + agent_targeting.PERSUADABLE_VOTE_BONUS)
        self.assertEqual(result['post:603']['target']['score'], base + agent_targeting.PERSUADABLE_VOTE_BONUS)
        self.assertEqual(result['post:602']['target']['score'], base - agent_targeting.FIRM_NO_PENALTY)
        self.assertIn('firm_no_vote', result['post:602']['target']['notes'])


class BehaviorAndDepthTest(unittest.TestCase):
    """Bot-like authors and deep threads are found from stored posts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'ctn.sqlite3')
        initialize(self.db)
        self.next_id = 1000
        conn = connect(self.db)
        with conn:
            # 800 answers 6 of 10 replies (bot-like); 801 answers 1 of 10 (citizen-like).
            for author, answered in ((800, 6), (801, 1)):
                root = self.add(conn, author, None)
                for index in range(10):
                    reply = self.add(conn, 2000 + index, root)
                    if index < answered:
                        self.add(conn, author, reply)
            # 802 keeps a two-person thread going to depth 14.
            parent = self.add(conn, 803, None)
            for depth in range(1, 15):
                parent = self.add(conn, 802 if depth % 2 else 803, parent)
            self.chain = []
            parent = None
            for depth in range(9):
                parent = self.add(conn, 3000 + depth, parent)
                self.chain.append(parent)
        conn.close()
        agent_targeting._cache.clear()

    def tearDown(self):
        agent_targeting._cache.clear()
        self.tmp.cleanup()

    def add(self, conn, author, parent):
        self.next_id += 1
        conn.execute('INSERT INTO posts(post_id,author_id,reply_to_post_id,body,created_at,first_seen,last_seen) '
                     'VALUES(?,?,?,?,?,?,?)', (self.next_id, author, parent, 'text', iso(30), iso(30), iso(30)))
        return self.next_id

    def test_answer_rate_marks_bot_like_authors(self):
        bots = agent_targeting.behavior_bots(self.db)
        self.assertIn(800, bots)
        self.assertNotIn(801, bots)

    def test_too_few_received_replies_are_not_evidence(self):
        # 802 answers nearly every reply but received only seven of them.
        self.assertNotIn(802, agent_targeting.behavior_bots(self.db))
        with patch.object(agent_targeting, 'BEHAVIOR_MIN_RECEIVED', 5):
            agent_targeting._cache.clear()
            self.assertIn(802, agent_targeting.behavior_bots(self.db))

    def test_thread_depths_and_blocked_targets(self):
        depths = agent_targeting.thread_depths(self.db, self.chain)
        self.assertEqual([depths[p] for p in self.chain], list(range(9)))
        sources = [{'id': f'post:{p}', 'kind': 'post', 'post_id': p, 'author_id': 1,
                    'text': 'Not sure about the mandate.'} for p in self.chain]
        sources.append({'id': 'post:x', 'kind': 'post', 'post_id': 99999, 'author_id': 800})
        sources.append({'id': 'news:1', 'kind': 'news'})
        marked = {s['id']: s.get('target_blocked') for s in agent_targeting.mark_blocked_targets(self.db, sources)}
        self.assertIsNone(marked[f'post:{self.chain[6]}'])
        self.assertEqual(marked[f'post:{self.chain[7]}'], 'thread_too_deep')
        self.assertEqual(marked['post:x'], 'bot_like_author')
        self.assertIsNone(marked['news:1'])
        self.assertNotIn('target_blocked', sources[0])

    def test_only_persuadable_citizens_and_unanswered_concerns_are_targets(self):
        now = time.time()
        published = lambda minutes, **d: {'state': 'published', 'action': 'reply', 'target_post_id': 50,
                                          'published_at': iso(minutes), **d}
        data = {'drafts': {
            'a': published(60, target_author_id=40, concern='cost'),
            # Unknown concern: classified from the stored target source text.
            'b': published(30, sources=[{'post_id': 50, 'author_id': 41, 'text': 'Who will track my national id?'}]),
            'old': published(7 * 60, target_author_id=42, concern='cost'),
            'like': {'state': 'published', 'action': 'like', 'target_author_id': 43, 'published_at': iso(5)}}}
        self.assertEqual(agent_targeting.answered_concerns(data, now), {40: {'cost'}, 41: {'id_privacy'}})
        post = lambda pid, author, text, **extra: {'id': f'post:{pid}', 'kind': 'post', 'post_id': pid,
                                                   'author_id': author, 'text': text, **extra}
        sources = [post(1, 40, 'Not sure, the vaccine cost worries me.'),
                   post(2, 40, 'Not sure, will kids at school be excluded?'),
                   post(3, 42, 'Not sure, the vaccine cost worries me.'),
                   post(4, 44, 'Vote yes! Mandate now.'),
                   post(5, 44, 'Lovely sunset over the harbour.'),
                   post(6, 44, 'Vote no, two-tier society.'),
                   post(7, 45, 'Not sure about the mandate.', news_account='herald'),
                   post(8, 41, 'Undecided. Is this a digital id tracking scheme?'),
                   post(9, 46, 'Thanks, what about the ferry times?', notification_ids=[5]),
                   post(10, 46, 'Yes! Mandate now.', notification_ids=[6])]
        marked = {s['post_id']: s.get('target_blocked')
                  for s in agent_targeting.mark_blocked_targets(self.db, sources, data, now)}
        self.assertEqual(marked, {1: 'answered_recently', 2: None, 3: None, 4: 'not_persuadable',
                                  5: 'not_persuadable', 6: None, 7: 'not_a_citizen', 8: 'answered_recently',
                                  9: None, 10: 'not_persuadable'})

    def test_annotation_tables_are_read_when_filled(self):
        # Empty tables: computed live (800 is a behaviour bot in this fixture).
        self.assertIn(800, agent_targeting.bot_accounts(self.db))
        conn = connect(self.db)
        with conn:
            conn.execute("INSERT INTO account_annotations(account_id,is_bot,bot_reasons_json,flagged_at,updated_at) "
                         "VALUES(901,1,'[\"behavior\"]',?,?)", (iso(), iso()))
            conn.execute("INSERT INTO account_annotations(account_id,is_bot,bot_reasons_json,flagged_at,updated_at) "
                         "VALUES(902,1,'[\"duplicate_text\"]',?,?)", (iso(), iso()))
            conn.execute("INSERT INTO post_annotations(post_id,author_id,created_at,stance,concerns_json,thread_depth,"
                         "classifier,updated_at) VALUES(?,1,?,'undecided','[]',9,'x',?)", (self.chain[0], iso(), iso()))
        conn.close()
        agent_targeting._cache.clear()
        # Filled tables win; duplicate text alone is not a bot.
        self.assertEqual(agent_targeting.bot_accounts(self.db), frozenset({901}))
        depths = agent_targeting.depths_for(self.db, [self.chain[0], self.chain[3]])
        self.assertEqual(depths, {self.chain[0]: 9, self.chain[3]: 3})

    def test_team_replies_per_thread_are_capped(self):
        conn = connect(self.db)
        with conn:
            # Our account 77 already replied once in thread 5000 (stored post 5002).
            for post_id, author, parent in ((5000, 1, None), (5001, 2, 5000), (5002, 77, 5001)):
                conn.execute('INSERT INTO posts(post_id,author_id,reply_to_post_id,root_post_id,body,created_at,'
                             'first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?)',
                             (post_id, author, parent, 5000 if parent else None, 'x', iso(5), iso(5), iso(5)))
        conn.close()
        source = lambda pid, root: {'post_id': pid, 'root_post_id': root, 'author_id': 3}
        data = {'agents': {'a': {'account_id': 77}}, 'drafts': {
            # The published draft is the stored post 5002 and must not count twice.
            'p': {'id': 'p', 'action': 'reply', 'state': 'published', 'post_id': 5002, 'target_post_id': 5001,
                  'sources': [source(5001, 5000)]},
            'q': {'id': 'q', 'action': 'reply', 'state': 'discarded', 'target_post_id': 5001,
                  'sources': [source(5001, 5000)]}}}
        self.assertEqual(agent_targeting.team_thread_replies(self.db, data, [5000]), {5000: 1})
        data['drafts']['r'] = {'id': 'r', 'action': 'reply', 'state': 'pending', 'target_post_id': 5000,
                               'sources': [source(5000, None)]}
        self.assertEqual(agent_targeting.team_thread_replies(self.db, data, [5000]), {5000: 2})
        sources = [{'id': 'post:5001', 'kind': 'post', 'post_id': 5001, 'root_post_id': 5000, 'author_id': 2,
                    'text': 'Not sure about the mandate.'},
                   {'id': 'post:6000', 'kind': 'post', 'post_id': 6000, 'author_id': 2,
                    'text': 'Not sure about the mandate.'}]
        marked = {s['post_id']: s.get('target_blocked')
                  for s in agent_targeting.mark_blocked_targets(self.db, sources, data)}
        self.assertEqual(marked, {5001: 'thread_reply_limit', 6000: None})

    def test_replies_need_a_conversation_or_a_widely_seen_post(self):
        conn = connect(self.db)
        with conn:
            for post_id, views in ((7001, 3), (7002, 25), (7003, 1)):
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) '
                             'VALUES(?,?,?,?,?,?)', (post_id, 5, 'x', iso(5), iso(5), iso(5)))
                conn.execute('INSERT INTO counter_snapshots(post_id,observed_at,view_count) VALUES(?,?,?)',
                             (post_id, iso(1), views))
        conn.close()
        text = 'Not sure about the mandate.'
        sources = [{'id': f'post:{p}', 'kind': 'post', 'post_id': p, 'author_id': 5, 'text': text, **extra}
                   for p, extra in ((7001, {}), (7002, {}), (7003, {'notification_ids': [9]}), (7004, {}))]
        marked = {s['post_id']: s.get('target_blocked')
                  for s in agent_targeting.mark_blocked_targets(self.db, sources, {'drafts': {}})}
        # Low views and no conversation: low reach. Unsampled 7004 is not judged.
        self.assertEqual(marked, {7001: 'low_reach', 7002: None, 7003: None, 7004: None})

    def test_only_citizens_and_one_of_our_roles_per_citizen(self):
        conn = connect(self.db)
        with conn:
            for index in range(agent_targeting.CITIZEN_MAX_WRITES + 1):  # 8001: too many actions for a citizen
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) '
                             'VALUES(?,?,?,?,?,?)', (9000 + index, 8001, 'x', iso(60), iso(60), iso(60)))
            conn.execute('INSERT INTO accounts(account_id,username,verified,first_seen,last_seen) '
                         "VALUES(8002,'press',1,?,?)", (iso(), iso()))
            # 8005 replied to post 8100 by citizen 8006; 8005 is a high-volume competitor.
            conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) '
                         'VALUES(8100,8006,?,?,?,?)', ('Not sure about the mandate.', iso(9), iso(9), iso(9)))
            conn.execute('INSERT INTO posts(post_id,author_id,reply_to_post_id,body,created_at,first_seen,last_seen) '
                         'VALUES(8101,8001,8100,?,?,?,?)', ('x', iso(8), iso(8), iso(8)))
        conn.close()
        text = 'Not sure about the mandate.'
        post = lambda pid, author: {'id': f'post:{pid}', 'kind': 'post', 'post_id': pid, 'author_id': author,
                                    'text': text}
        now = time.time()
        data = {'agents': {'me': {'account_id': 70}, 'other': {'account_id': 71}}, 'drafts': {
            'd': {'id': 'd', 'agent_id': 'other', 'action': 'reply', 'state': 'published', 'target_post_id': 1,
                  'target_author_id': 8004, 'concern': 'cost', 'published_at': iso(120)},
            'old': {'id': 'old', 'agent_id': 'other', 'action': 'reply', 'state': 'published', 'target_post_id': 2,
                    'target_author_id': 8007, 'published_at': iso(25 * 60)}}}
        sources = [post(1, 8001), post(2, 8002), post(3, 8003), post(4, 8004), post(5, 8007)]
        marked = {s['post_id']: s.get('target_blocked')
                  for s in agent_targeting.mark_blocked_targets(self.db, sources, data, now, 'me')}
        self.assertEqual(marked, {1: 'not_a_citizen', 2: 'not_a_citizen', 3: None, 4: 'citizen_taken', 5: None})
        # The role that already answered 8004 may continue with that citizen on a new concern.
        kids = {**post(4, 8004), 'text': 'Not sure, will kids at school be excluded?'}
        mine = {s['post_id']: s.get('target_blocked')
                for s in agent_targeting.mark_blocked_targets(self.db, [kids], data, now, 'other')}
        self.assertEqual(mine, {4: None})
        # A citizen post already answered by a competitor ranks lower for reading.
        sources = {'post:8100': post(8100, 8006), 'post:3': post(3, 8003)}
        notes = {k: v['target']['notes'] for k, v in agent_targeting.annotate(
            self.db, {'agents': {}, 'drafts': {}}, {'id': 'me'}, sources, now).items()}
        self.assertIn('contested', notes['post:8100'])
        self.assertNotIn('contested', notes['post:3'])

    def test_calibrated_citizen_proxy(self):
        conn = connect(self.db)
        add = lambda pid, author, body='Not sure about the mandate.', repost=None: conn.execute(
            'INSERT INTO posts(post_id,author_id,repost_of_post_id,body,created_at,first_seen,last_seen) '
            'VALUES(?,?,?,?,?,?,?)', (pid, author, repost, body, iso(30), iso(30), iso(30)))
        with conn:
            for index in range(8):  # 8601: a repost ring account (8 actions, 5 reposts)
                add(40000 + index, 8601, repost=1 if index < 5 else None)
            add(40100, 8602, "Sure, here's a draft post about the referendum.")  # LLM artefact
            for index in range(5):  # Swan_150_00x: five batch-numbered accounts
                conn.execute('INSERT INTO accounts(account_id,username,first_seen,last_seen) VALUES(?,?,?,?)',
                             (8610 + index, f'Swan_150_00{index}', iso(), iso()))
            for index, account in enumerate((100, 8620, 8621, 8622, 8623)):  # generator names incl. a pre-competition one
                conn.execute('INSERT INTO accounts(account_id,username,first_seen,last_seen) VALUES(?,?,?,?)',
                             (account, f'i_have_{10 + index}_anchors', iso(), iso()))
            add(40200, 8603)
            # 30 actions each: 8630 wrote 25 of them three days ago (5 in 48 h), 8631 all today.
            for index in range(30):
                stamp = iso(72 * 60 if index < 25 else 30)
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) '
                             'VALUES(?,?,?,?,?,?)', (40300 + index, 8630, 'x', stamp, stamp, stamp))
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) '
                             'VALUES(?,?,?,?,?,?)', (40400 + index, 8631, 'x', iso(30), iso(30), iso(30)))
        conn.close()
        agent_targeting._cache.clear()
        ids = [8601, 8602, 8603, 8610, 8620, 100, 70, 8630, 8631]
        self.assertEqual(agent_targeting.non_citizens(self.db, ids), frozenset({8601, 8602, 8610, 8631}))
        self.assertEqual(agent_targeting.citizen_accounts(self.db, ids, own={70}),
                         frozenset({8603, 8620, 100, 8630}))
        # Bot-flagged accounts are never citizens, whatever their volume.
        with patch.object(agent_targeting, 'bot_accounts', return_value=frozenset({8603})):
            self.assertNotIn(8603, agent_targeting.citizen_accounts(self.db, ids))

    def test_profile_and_style_reasons(self):
        bot_text = 'Have you read the IHO report? ' + 'x' * 260 + ' #VoteYes.'
        npc_text = 'honestly not sure about the mandate yet'
        conn = connect(self.db)
        with conn:
            for account, username, display, created in (
                    (8650, 'WiPa_Possum', 'William Padilla', iso()),
                    (8651, "liam_oneil", "Liam O'Neil", iso()),
                    (8652, 'ctnyes_policy_b1701e', 'Policy Voice', iso()),  # 6-hex suffix
                    (8653, 'Koa Nguyen', None, iso()),  # space in the username
                    (8654, 'ninahughes376', None, iso()),  # fetched profile, no display name
                    (8655, 'ninahughes377', None, None),  # only the id seen: not judged
                    (8656, 'sam', 'sam', iso()),
                    (8657, 'dr_santos', 'Dr. Michael Santos', iso())):
                conn.execute('INSERT INTO accounts(account_id,username,display_name,created_at,first_seen,last_seen) '
                             'VALUES(?,?,?,?,?,?)', (account, username, display, created, iso(), iso()))
            for author, bodies in ((8660, [bot_text]), (8661, [npc_text] * 3),
                                   (8662, [bot_text] * 5 + [npc_text] * agent_targeting.STYLE_TEXTS)):
                for index, body in enumerate(bodies):  # later post ids are more recent
                    conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) '
                                 'VALUES(?,?,?,?,?,?)', (42000 + author * 40 + index, author, body, iso(), iso(), iso()))
        conn.close()
        reasons = agent_targeting.non_citizen_reasons(self.db, [*range(8650, 8658), 8660, 8661, 8662])
        self.assertEqual({author for author, found in reasons.items() if 'profile' in found},
                         {8652, 8653, 8654, 8656, 8657})
        # Only the latest STYLE_TEXTS originals count: 8662's older bot texts drop out.
        self.assertEqual({author for author, found in reasons.items() if 'style' in found}, {8660})
        self.assertLess(agent_targeting.style_score([bot_text]), agent_targeting.STYLE_MIN_SCORE)
        self.assertGreater(agent_targeting.style_score([npc_text]), 0)

    def test_failing_citizen_check_is_logged_and_raised_for_strict_callers(self):
        conn = connect(self.db)
        with conn:
            conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                         (43000, 8670, 'honestly not sure about the mandate yet', iso(), iso(), iso()))
        conn.close()
        agent_targeting._cache.clear()
        agent_targeting._cannot_judge_logged.clear()
        with patch.object(agent_targeting, 'style_score', side_effect=RuntimeError('check failed')):
            # Targeting keeps going (the author passes), but the failure is no longer silent.
            with self.assertLogs('ctn_data.agent_targeting', 'WARNING') as logs:
                self.assertEqual(agent_targeting.non_citizen_reasons(self.db, [8670]), {})
            self.assertIn('could not judge', logs.output[0])
            with self.assertRaises(RuntimeError):
                agent_targeting.citizen_accounts(self.db, [8670], strict=True)

    def test_crowded_posts_blocked_and_fresh_uncontested_preferred(self):
        conn = connect(self.db)
        with conn:
            conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                         (41000, 8700, 'Not sure about the mandate.', iso(60), iso(60), iso(60)))
            conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                         (41001, 8701, 'Not sure about the mandate.', iso(3), iso(3), iso(3)))
            for index in range(agent_targeting.CROWDED_COMPETITOR_REPLIES):
                author = 8800 + index  # each competitor has 25 actions, so none is a citizen
                for extra in range(agent_targeting.CITIZEN_MAX_WRITES + 1):
                    parent = 41000 if extra == 0 else None
                    conn.execute('INSERT INTO posts(post_id,author_id,reply_to_post_id,body,created_at,first_seen,'
                                 'last_seen) VALUES(?,?,?,?,?,?,?)',
                                 (50000 + index * 100 + extra, author, parent, 'x', iso(50), iso(50), iso(50)))
        conn.close()
        post = lambda pid, author: {'id': f'post:{pid}', 'kind': 'post', 'post_id': pid, 'author_id': author,
                                    'text': 'Not sure about the mandate.'}
        marked = {s['post_id']: s.get('target_blocked') for s in agent_targeting.mark_blocked_targets(
            self.db, [post(41000, 8700), post(41001, 8701)], {'drafts': {}})}
        self.assertEqual(marked, {41000: 'crowded', 41001: None})
        notes = agent_targeting.annotate(self.db, {'agents': {}, 'drafts': {}}, {'id': 'me'},
                                         {'a': post(41000, 8700), 'b': post(41001, 8701)}, time.time())
        self.assertIn('contested', notes['a']['target']['notes'])
        self.assertIn('fresh_uncontested', notes['b']['target']['notes'])

    def test_mention_candidates_for_original_posts(self):
        conn = connect(self.db)
        with conn:
            for account, name, minutes, body in ((8901, 'tessa', 5, 'Will the national id track my health?'),
                                                 (8902, 'raven', 10, 'Privacy worries me: a digital id?'),
                                                 (8903, 'old_one', 90, 'Health passport tracking?'),
                                                 (8904, 'kids', 5, 'What about kids at school?'),
                                                 (8905, 'third', 15, 'Surveillance by health passport?')):
                conn.execute('INSERT INTO accounts(account_id,username,first_seen,last_seen) VALUES(?,?,?,?)',
                             (account, name, iso(), iso()))
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) '
                             'VALUES(?,?,?,?,?,?)', (42000 + account, account, body, iso(minutes), iso(), iso()))
        conn.close()
        data = {'agents': {}, 'drafts': {'d': {'state': 'published', 'body': 'Thanks @Raven for asking.',
                                               'published_at': iso(60)}}}
        names = [c['username'] for c in agent_targeting.mention_candidates(self.db, data, 'id_privacy')]
        # Newest first; raven was mentioned today; old_one is outside the hour; kids is another concern.
        self.assertEqual(names, ['tessa', 'third'])
        self.assertEqual(agent_targeting.mention_candidates(self.db, data, 'none'), [])

    def test_figure_mention_matches_the_concern_and_rotates_per_role(self):
        pick = lambda data, concern: agent_targeting.figure_mention(data, 'me', concern)['username']
        empty = {'agents': {}, 'drafts': {}}
        self.assertEqual(pick(empty, 'hospital'), 'DrAmaraSingh')
        self.assertEqual(pick(empty, 'tourism'), 'MarinaCastilloCEO')
        self.assertEqual(pick(empty, 'id_privacy'), 'RealVictorHawthorne')
        self.assertEqual(pick(empty, None), 'DrAmaraSingh')
        self.assertIn('Medical Officers Union', agent_targeting.figure_mention(empty, 'me', 'hospital')['who'])
        # 'how' says what they are responsible for, never words to say to them (D97).
        for info in agent_targeting.PUBLIC_FIGURES.values():
            self.assertNotRegex(info['how'], r'(?i)\b(?:thank|ask|keep|answer|commitment)')
            # A short label, not a sentence a writer can paste ("heads the government that would...", 24 Oct).
            self.assertLessEqual(len(info['how']), 60)
        # The role's latest original named Dr Singh: the next health post names the President instead.
        data = {'agents': {}, 'drafts': {
            'old': {'agent_id': 'me', 'action': 'post', 'state': 'published', 'created_at': '2026-10-22T01:00:00Z',
                    'body': '@RealVictorHawthorne, please spell it out.'},
            'new': {'agent_id': 'me', 'action': 'post', 'state': 'published', 'created_at': '2026-10-22T02:00:00Z',
                    'body': 'Thank you @drAmaraSingh for the clear answers.'},
            'other': {'agent_id': 'you', 'action': 'post', 'state': 'published', 'created_at': '2026-10-22T03:00:00Z',
                      'body': '@RealVictorHawthorne again.'},
            'gone': {'agent_id': 'me', 'action': 'post', 'state': 'discarded', 'created_at': '2026-10-22T04:00:00Z',
                     'body': '@RealVictorHawthorne.'}}}
        self.assertEqual(pick(data, 'hospital'), 'RealVictorHawthorne')
        self.assertEqual(pick(data, 'id_privacy'), 'RealVictorHawthorne')

    def test_always_on_features(self):
        base = time.time() - 3600
        conn = connect(self.db)
        at = lambda seconds: datetime.fromtimestamp(base - seconds, timezone.utc).isoformat().replace('+00:00', 'Z')
        with conn:
            rows, post_id = [], 20000
            # 8501: a write every hour for 47 hours, exactly 3600 s apart (always on, regular rhythm).
            for hour in range(47):
                rows.append((post_id, 8501, None, at(hour * 3600))); post_id += 1
            # 8502: 15 writes inside one clock hour (high volume) and nothing else.
            hour_start = (int(base) // 3600 - 2) * 3600
            for minute in range(15):
                rows.append((post_id, 8502, None, at(base - hour_start - 60 - minute * 60))); post_id += 1
            # 8503: six replies 30 s after citizen posts (fast reply).
            for index in range(6):
                parent = post_id; rows.append((parent, 8504, None, at(index * 5000 + 30))); post_id += 1
                rows.append((post_id, 8503, parent, at(index * 5000))); post_id += 1
            for pid, author, parent, created in rows:
                conn.execute('INSERT INTO posts(post_id,author_id,reply_to_post_id,body,created_at,first_seen,last_seen) '
                             'VALUES(?,?,?,?,?,?,?)', (pid, author, parent, 'x', created, created, created))
        conn.close()
        flags = agent_targeting.bot_feature_flags(self.db)
        self.assertEqual(set(flags[8501]['reasons']), {'always_on', 'regular_rhythm'})
        self.assertEqual(flags[8502]['reasons'], ['high_volume'])
        self.assertEqual(flags[8503]['reasons'], ['fast_reply'])
        self.assertNotIn(8504, flags)
        self.assertEqual(set(flags[8501]['metrics']),
                         {'active_hours', 'writes_per_day', 'max_hourly', 'reply_median_s', 'interval_cv'})

    def test_our_shifted_and_delayed_accounts_do_not_look_always_on(self):
        """Self-check (D39): D37 shifts keep features 1, 3 and 4 off; replies have no added pause since D72."""
        import random
        from ctn_data import agent_control, agent_schedule
        rng = random.Random(7)
        schedule = agent_schedule.DEFAULT_SCHEDULE
        end = time.time() - 60
        conn = connect(self.db)
        post_id = 30000
        with conn:
            for account in (10783, 6464, 11563, 6494):
                t, written = end - 48 * 3600, []
                while t < end:
                    shift = agent_schedule.current_shift(schedule, account, t)
                    if shift is None:
                        t = agent_schedule.next_start(schedule, account, t)
                        continue
                    if sum(1 for w in written if t - w < 3600) >= agent_schedule.hourly_limit(schedule, shift, t):
                        t = min(w for w in written if t - w < 3600) + 3600
                        continue
                    t += rng.uniform(60, 240)           # reading, deciding and writing
                    reply = len(written) % 3 == 2       # post, post, reply
                    parent = None
                    if reply:
                        # A citizen posted shortly before we read it; the reply then waits.
                        parent, post_id = post_id, post_id + 1
                        cited = t - rng.uniform(60, 600)
                        stamp = datetime.fromtimestamp(cited, timezone.utc).isoformat().replace('+00:00', 'Z')
                        conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) '
                                     'VALUES(?,?,?,?,?,?)', (parent, 9999, 'x', stamp, stamp, stamp))
                        t += rng.uniform(*agent_control.REPLY_DELAY_SECONDS)
                    if t >= end:
                        break
                    stamp = datetime.fromtimestamp(t, timezone.utc).isoformat().replace('+00:00', 'Z')
                    conn.execute('INSERT INTO posts(post_id,author_id,reply_to_post_id,body,created_at,first_seen,'
                                 'last_seen) VALUES(?,?,?,?,?,?,?)', (post_id, account, parent, 'x', stamp, stamp, stamp))
                    post_id += 1
                    written.append(t)
        conn.close()
        flags = agent_targeting.bot_feature_flags(self.db, now=end + 60)
        for account in (10783, 6464, 11563, 6494):
            reasons = set(flags.get(account, {}).get('reasons', []))
            with self.subTest(account=account):
                self.assertFalse(reasons & {'always_on', 'fast_reply', 'regular_rhythm'}, flags.get(account))
                self.assertLess(len(reasons), 2)

    def test_blocked_sources_are_never_post_targets(self):
        from ctn_data import agent_relationships
        draft = {'action': 'reply', 'sources': [{'post_id': 1}, {'post_id': 2, 'target_blocked': 'thread_too_deep'}]}
        self.assertEqual(agent_relationships.allowed_post_targets(draft, include_policy_blocked=True), {1})


if __name__ == '__main__':
    unittest.main()
