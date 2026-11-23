"""Reply-opportunity queue and the model-free rules reading selection (D48)."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ctn_data import agent_observation, agent_opportunities, agent_reading, agent_targeting, reading_selection
from ctn_data.agent_control import AgentController, AgentError
from ctn_data.db import connect, initialize


def iso(now, minutes_ago):
    value = datetime.fromtimestamp(now, timezone.utc) - timedelta(minutes=minutes_ago)
    return value.isoformat(timespec='seconds').replace('+00:00', 'Z')


ME, OTHER = 900, 901
COMPETITOR, HEAVY, BOT = 800, 108, 109


def agent(agent_id, account_id, **changes):
    return {'id': agent_id, 'account_id': account_id, 'enabled': True, 'account_state': 'ready',
            'allowed_actions': ['post', 'reply'], 'background': 'A quiet reader.', 'stance': 'Supports Yes.',
            'interests': [], **changes}


class Fixture:
    """Fresh posts: citizens (101-107, 110-112), a heavy author, a bot and a competitor."""

    def build(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'ctn.sqlite3')
        initialize(self.db)
        self.now = time.time()
        agent_targeting._cache.clear()
        agent_opportunities.invalidate()
        conn = connect(self.db)
        with conn:
            def account(account_id, username):
                conn.execute('INSERT INTO accounts(account_id,username,verified,private,first_seen,last_seen) '
                             'VALUES(?,?,0,0,?,?)', (account_id, username, iso(self.now, 60), iso(self.now, 0)))

            def post(post_id, author, body, minutes, reply_to=None, root=None):
                conn.execute('INSERT INTO posts(post_id,author_id,body,reply_to_post_id,root_post_id,visibility,'
                             'created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?)',
                             (post_id, author, body, reply_to, root, 'public', iso(self.now, minutes),
                              iso(self.now, minutes), iso(self.now, minutes)))
            names = {101: 'rosa_baker', 102: 'tom_fisher', 103: 'ana_lopes', 104: 'old_timer', 105: 'yes_voter',
                     106: 'sunset_fan', 107: 'home_cook', 110: 'deep_thread', 111: 'crowded_out', 112: 'taken_one',
                     HEAVY: 'heavy_poster', BOT: 'bot_like', COMPETITOR: 'competitor', ME: 'our_maya',
                     OTHER: 'our_ethan'}
            for account_id, username in names.items():
                account(account_id, username)
            post(1, 101, "I'm not sure how to vote on the H2D2 referendum. Will the hospital cope?", 5)
            post(2, 102, 'Vote no, this mandate is a power grab.', 15)
            post(3, 103, 'Still undecided about the vaccine mandate, honestly.', 25)
            post(4, 104, "I'm undecided on the referendum.", 40)
            post(5, 105, 'Voting yes on November 2!', 5)
            post(6, 106, 'Lovely sunset over the harbour tonight.', 5)
            post(7, 107, "I'm not sure what to cook tonight.", 5)
            post(10, 110, 'Not sure the referendum is worth it.', 5)
            post(11, 111, 'Unsure about the mandate.', 5)
            post(12, 112, 'Undecided on the H2D2 vote.', 5)
            post(13, ME, 'Undecided readers, ask me about the H2D2 vote.', 5)
            post(108, HEAVY, 'Not sure about the referendum at all.', 5)
            post(109, BOT, 'Not sure about the referendum, friends.', 5)
            for index in range(24):
                post(2000 + index, HEAVY, 'Another day.', 120 + index)
            # The competitor crowds post 11 and answers post 3 twice (25 writes: not a citizen).
            for index in range(11):
                post(3000 + index, COMPETITOR, 'Vote no.', 4, reply_to=11, root=11)
            for index in range(2):
                post(3100 + index, COMPETITOR, 'Vote no.', 20, reply_to=3, root=3)
            for index in range(12):
                post(3200 + index, COMPETITOR, 'Another day.', 200 + index)
            # Post 10 sits at depth 7: our reply would be the ninth message.
            conn.execute("INSERT INTO post_annotations(post_id,author_id,created_at,stance,concerns_json,thread_depth,"
                         "classifier,updated_at) VALUES(10,110,?,'undecided','[]',7,'test',?)",
                         (iso(self.now, 5), iso(self.now, 0)))
            conn.execute("INSERT INTO account_annotations(account_id,is_bot,bot_reasons_json,updated_at) "
                         "VALUES(?,1,'[\"behavior\"]',?)", (BOT, iso(self.now, 0)))
        conn.close()

    def data(self, other_enabled=False, drafts=None, **mine):
        return {'agents': {'a': agent('a', ME, **mine), 'b': agent('b', OTHER, enabled=other_enabled)},
                'drafts': drafts or {}}

    def authors(self, entries):
        return [entry['source']['author_id'] for entry in entries]


class QueueTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.build()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(agent_targeting._cache.clear)
        self.addCleanup(agent_opportunities.invalidate)

    def test_fresh_uncontested_citizens_rank_first_and_exclusions_hold(self):
        data = self.data()
        with patch('ctn_data.agent_targeting.radar_weights', return_value={}):
            entries = agent_opportunities.queue(self.db, data, data['agents']['a'], self.now)
        # 101 (5 min, uncontested) > 102 (15 min) > 112 (5 min, no question) ordering is by score;
        # 103 is contested by two competitor replies and 25 minutes old, so it comes last.
        authors = self.authors(entries)
        self.assertEqual(authors[0], 101)
        self.assertEqual(authors[-1], 103)
        self.assertEqual(set(authors), {101, 102, 103, 112})
        first = entries[0]['opportunity']
        self.assertIn('fresh_10m', first['reasons'])
        self.assertIn('uncontested', first['reasons'])
        self.assertEqual(entries[-1]['opportunity']['competitor_replies'], 2)
        self.assertEqual([entry['opportunity']['rank'] for entry in entries], list(range(1, len(entries) + 1)))
        source = entries[0]['source']
        self.assertEqual((source['id'], source['origin'], source['title']), ('post:1', 'opportunity', '@rosa_baker'))
        # Excluded: 40 min old (104), Yes (105), unrelated (106), off-topic "not sure" (107),
        # depth 7 (110), crowded by 11 competitor replies (111), our own (900), heavy (108), bot (109).

    def test_ranking_is_deterministic(self):
        data = self.data()
        with patch('ctn_data.agent_targeting.radar_weights', return_value={}):
            first = agent_opportunities.queue(self.db, data, data['agents']['a'], self.now)
            agent_opportunities.invalidate()
            second = agent_opportunities.queue(self.db, data, data['agents']['a'], self.now)
        self.assertEqual([e['source']['id'] for e in first], [e['source']['id'] for e in second])

    def test_radar_priority_lifts_a_concern_but_not_above_fresher_posts(self):
        data = self.data()
        score = lambda entries: next(e['opportunity'] for e in entries if e['source']['author_id'] == 102)
        with patch('ctn_data.agent_targeting.radar_weights', return_value={}):
            plain = score(agent_opportunities.queue(self.db, data, data['agents']['a'], self.now))
        agent_opportunities.invalidate()
        with patch('ctn_data.agent_targeting.radar_weights', return_value={'power_grab': 2}):
            entries = agent_opportunities.queue(self.db, data, data['agents']['a'], self.now)
        rising = score(entries)
        self.assertEqual(rising['score'], plain['score'] + 3)
        self.assertIn('radar_priority', rising['reasons'])
        # Ten minutes or younger still ranks first (D48); 112 names no concern, so any role may answer it.
        self.assertEqual(entries[0]['source']['author_id'], 112)
        # With the radar on, a concern neither the radar nor the persona covers is left out.
        self.assertNotIn(101, self.authors(entries))

    def test_concern_outside_radar_and_persona_is_left_out(self):
        data = self.data(background='A nurse at Kingstonia Central Hospital.')
        with patch('ctn_data.agent_targeting.radar_weights', return_value={}):
            entries = agent_opportunities.queue(self.db, data, data['agents']['a'], self.now)
        authors = self.authors(entries)
        self.assertIn(101, authors)      # hospital: the persona covers it
        self.assertNotIn(102, authors)   # power_grab: nobody on shift covers it
        self.assertIn(112, authors)      # no concern named: any role may answer
        self.assertIn('persona_concern', entries[0]['opportunity']['reasons'])

    def test_one_of_our_accounts_per_citizen_per_day(self):
        drafts = {'d': {'agent_id': 'b', 'action': 'reply', 'state': 'published', 'target_author_id': 112,
                        'target_post_id': 99, 'published_at': iso(self.now, 60)}}
        data = self.data(drafts=drafts)
        with patch('ctn_data.agent_targeting.radar_weights', return_value={}):
            entries = agent_opportunities.queue(self.db, data, data['agents']['a'], self.now)
        self.assertNotIn(112, self.authors(entries))

    def test_on_shift_roles_split_citizens_without_overlap(self):
        data = self.data(other_enabled=True)
        with patch('ctn_data.agent_targeting.radar_weights', return_value={}):
            mine = set(self.authors(agent_opportunities.queue(self.db, data, data['agents']['a'], self.now)))
            theirs = set(self.authors(agent_opportunities.queue(self.db, data, data['agents']['b'], self.now)))
        self.assertFalse(mine & theirs)
        self.assertEqual(mine | theirs, {101, 102, 103, 112})

    def test_queue_picks_are_not_blocked_for_low_reach(self):
        source = {'id': 'post:1', 'kind': 'post', 'post_id': 1, 'author_id': 101, 'text': 'Not sure about H2D2.',
                  'target': {'stance': 'undecided', 'concerns': []}}
        blocked = agent_targeting.blocked_reason(source, frozenset(), {}, views={1: 2})
        self.assertEqual(blocked, 'low_reach')
        queued = {**source, 'opportunity': {'rank': 1}}
        self.assertIsNone(agent_targeting.blocked_reason(queued, frozenset(), {}, views={1: 2}))

    def test_hot_concerns_count_citizens_and_radar(self):
        data = self.data()
        with patch('ctn_data.agent_targeting.radar_weights', return_value={'power_grab': 1}):
            hot = agent_opportunities.hot_concerns(self.db, data, self.now)
        self.assertEqual(hot[0], {'concern': 'power_grab', 'citizens': 1, 'radar': True})
        self.assertIn({'concern': 'hospital', 'citizens': 1}, hot)


class SelectTests(unittest.TestCase):
    def test_rule_selection_order(self):
        sources = {
            'post:1': {'id': 'post:1', 'kind': 'post', 'notification_ids': [5],
                       'target': {'score': 9, 'notes': ['engaged_with_us']}},
            'post:2': {'id': 'post:2', 'kind': 'post', 'notification_ids': [6],
                       'target': {'score': 3, 'notes': ['engaged_with_us', 'reply_to_our_original']}},
            'post:3': {'id': 'post:3', 'kind': 'post', 'opportunity': {'rank': 2}},
            'post:4': {'id': 'post:4', 'kind': 'post', 'opportunity': {'rank': 1}},
            'post:5': {'id': 'post:5', 'kind': 'post', 'target': {'score': 20}},
            'news:a': {'id': 'news:a', 'kind': 'news', 'freshness_at': '2026-10-22T01:00:00Z'},
            'news:b': {'id': 'news:b', 'kind': 'news', 'freshness_at': '2026-10-22T02:00:00Z'},
        }
        self.assertEqual(agent_opportunities.select(sources),
                         ['post:2', 'post:1', 'post:4', 'post:3', 'news:b', 'news:a'])


class RulesModeIntegrationTests(Fixture, unittest.TestCase):
    """reading_selection_mode='rules' reads the queue and never calls a model to select."""

    def setUp(self):
        self.build()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(agent_targeting._cache.clear)
        self.addCleanup(agent_opportunities.invalidate)
        root = Path(self.tmp.name)
        self.controller = AgentController(self.db, root / 'keys',
            provider_store=SimpleNamespace(get=lambda _: {'id': 'fake', 'protocol': 'openai_chat', 'model': 'fake'}),
            clock=lambda: self.now, sdk_factory=lambda **kw: self.fail('no SDK in this test'))
        self.addCleanup(self.controller.stop)
        self.controller.save({'name': 'Reader', 'username': 'reader', 'provider_id': 'fake',
                              'freshness_minutes': 10, 'allowed_actions': ['post', 'reply']})
        self.agent_id = self.controller.snapshot()['agents'][0]['id']
        self.controller.store.change(lambda data, conn: data['agents'][self.agent_id].update(
            account_id=ME, account_state='ready', enabled=True))

    def agent(self):
        return self.controller.store.read()['agents'][self.agent_id]

    def observe(self):
        with self.controller._claim(self.agent_id) as token:
            return agent_observation.observe(self.controller, self.agent(), token, refresh=False, for_decision=True)

    def test_setting_defaults_to_model_and_validates(self):
        self.assertEqual(self.agent()['reading_selection_mode'], 'model')
        self.controller.save({'id': self.agent_id, 'name': 'Reader', 'reading_selection_mode': 'rules'})
        self.assertEqual(self.agent()['reading_selection_mode'], 'rules')
        with self.assertRaises(AgentError):
            self.controller.save({'id': self.agent_id, 'name': 'Reader', 'reading_selection_mode': 'random'})

    def test_rules_mode_reads_the_queue_without_a_model_call(self):
        self.controller.save({'id': self.agent_id, 'name': 'Reader', 'reading_selection_mode': 'rules'})
        with patch('ctn_data.agent_budget.model_call', side_effect=AssertionError('no model in rules mode')), \
                patch('ctn_data.agent_targeting.radar_weights', return_value={}):
            selected = self.observe()
        targets = [s for s in selected if not s.get('context_only')]
        ids = [s['id'] for s in targets]
        # 15- and 25-minute posts are outside the 10-minute local window; only the queue finds them.
        self.assertIn('post:2', ids)
        self.assertIn('post:3', ids)
        self.assertEqual(ids[0], 'post:1')
        self.assertTrue(all(isinstance(s.get('opportunity'), dict) for s in targets))
        observation = self.controller.store.read()['memory'][self.agent_id]['observation']
        self.assertEqual(observation['reading_selection']['cache_mode'], 'rules')
        self.assertEqual(observation['reading_selection']['status'], 'selected')
        self.assertIn('未调用模型', observation['reading_selection']['reason'])
        events = [e for e in self.controller.store.read()['events'] if e['kind'] == 'reading_selection']
        self.assertTrue(events)

    def test_model_mode_still_calls_the_selector(self):
        answer = {'text': json.dumps({'source_ids': ['post:1'], 'reason': 'fresh'})}
        with patch('ctn_data.agent_budget.model_call', return_value=answer) as call, \
                patch('ctn_data.agent_targeting.radar_weights', return_value={}):
            selected = self.observe()
        call.assert_called_once()
        self.assertEqual([s['id'] for s in selected if not s.get('context_only')], ['post:1'])
        self.assertFalse(any(s.get('opportunity') for s in selected))


class ReplyBackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'ctn.sqlite3')
        initialize(self.db)
        agent_targeting._cache.clear()
        self.addCleanup(agent_targeting._cache.clear)

    def test_citizen_reply_to_our_original_is_noted_and_scored_first(self):
        data = {'agents': {'a': {'account_id': ME}},
                'drafts': {'p': {'agent_id': 'a', 'action': 'post', 'state': 'published', 'post_id': 500}}}
        sources = {
            'post:501': {'id': 'post:501', 'kind': 'post', 'post_id': 501, 'author_id': 101, 'reply_to_post_id': 500,
                         'notification_ids': [7], 'text': 'Not sure. What about the hospital?'},
            'post:502': {'id': 'post:502', 'kind': 'post', 'post_id': 502, 'author_id': 102, 'reply_to_post_id': 77,
                         'notification_ids': [8], 'text': 'Not sure. What about the hospital?'},
        }
        result = agent_targeting.annotate(self.db, data, {'id': 'a'}, sources, time.time())
        self.assertIn('reply_to_our_original', result['post:501']['target']['notes'])
        self.assertNotIn('reply_to_our_original', result['post:502']['target']['notes'])
        self.assertGreater(result['post:501']['target']['score'], result['post:502']['target']['score'])
        self.assertEqual(agent_opportunities.select(result)[0], 'post:501')


class MentionTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.build()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(agent_targeting._cache.clear)

    def test_originals_mention_persuadable_citizens_who_just_raised_the_concern(self):
        conn = connect(self.db)
        with conn:
            for post_id, author, name, minutes, body in (
                    (60, 601, 'yes_nurse', 2, 'Vote yes, the hospital needs this.'),
                    (61, 602, 'unsure_nurse', 8, 'Not sure how to vote. Can the hospital cope with H2D2?')):
                conn.execute('INSERT INTO accounts(account_id,username,first_seen,last_seen) VALUES(?,?,?,?)',
                             (author, name, iso(self.now, 60), iso(self.now, 0)))
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) '
                             'VALUES(?,?,?,?,?,?)', (post_id, author, body, iso(self.now, minutes),
                                                    iso(self.now, minutes), iso(self.now, minutes)))
        conn.close()
        names = [c['username'] for c in agent_targeting.mention_candidates(self.db, self.data(), 'hospital', self.now)]
        # The Yes nurse posted most recently but the two undecided citizens come first (newest first).
        self.assertEqual(names, ['rosa_baker', 'unsure_nurse'])


if __name__ == '__main__':
    unittest.main()
