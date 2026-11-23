"""Account identity: one label per active account from the Agents' targeting rules; read-only."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from ctn_data import account_identity, agent_targeting
from ctn_data.db import connect, initialize
from ctn_data.server import create_app
from tests.auth_helpers import login

NOW = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)


def stamp(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec='microseconds').replace('+00:00', 'Z')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'test.sqlite3'
        initialize(self.db)
        agent_targeting._cache.clear()
        self.addCleanup(agent_targeting._cache.clear)
        self.next_id = 1000
        conn = connect(self.db)
        with conn:
            for account, name, verified in ((100, 'tessa', 0), (200, 'loud_one', 0), (300, 'botty', 0),
                                            (5554, 'maya_collins', 0), (6383, 'Dr Jeff Vance', 0), (8, 'DrAmaraSingh', 1)):
                conn.execute('INSERT INTO accounts(account_id,username,verified,private,first_seen,last_seen) '
                             'VALUES(?,?,?,0,?,?)', (account, name, verified, stamp(0), stamp(0)))
            conn.execute("INSERT INTO team_accounts VALUES(5554,'Agent Zero',1,'test',?)", (stamp(0),))
            # 6383 is our team's but run by a teammate: in team_accounts, not in the Agent store.
            conn.execute("INSERT INTO team_accounts VALUES(6383,'Agent Zero',1,'teammate',?)", (stamp(0),))
            conn.execute('INSERT INTO meta(key,value) VALUES(?,?)',
                         (account_identity.AGENTS_META_KEY, json.dumps({'maya': {'account_id': 5554}})))
            conn.execute('INSERT INTO account_annotations(account_id,is_bot,bot_reasons_json,flagged_at,updated_at) '
                         'VALUES(300,1,?,?,?)', (json.dumps(['always_on', 'high_volume']), stamp(0), stamp(0)))
            self.citizen_post = self.post(conn, 100, 30, "I'm not sure about the mandate. Will it track my health?")
            ours = self.post(conn, 5554, 20, 'Medical exemptions are in the rule.', parent=self.citizen_post)
            self.post(conn, 100, 10, 'Thanks, that helps.', parent=ours)
            for minutes in range(30):
                self.post(conn, 200, 60 + minutes, f'Vote no, keep Kingstonia open ({minutes})')
            self.post(conn, 300, 5, 'Vote yes today!')
            self.post(conn, 6383, 25, 'Protect Kingstonia: vote yes on November 2.')
            self.post(conn, 8, 15, 'Clinics are open late this week.')
        conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def post(self, conn, author, minutes_ago, body, parent=None):
        self.next_id += 1
        at = stamp(minutes_ago)
        conn.execute('INSERT INTO posts(post_id,author_id,kind,body,reply_to_post_id,visibility,created_at,first_seen,'
                     'last_seen) VALUES(?,?,?,?,?,?,?,?,?)',
                     (self.next_id, author, 'reply' if parent else 'post', body, parent, 'public', at, at, at))
        return self.next_id


class BuildTests(Fixture):
    def test_one_label_per_account_with_reasons(self):
        result = account_identity.build(self.db, now=NOW)
        rows = {row['account_id']: row for row in result['rows']}
        self.assertEqual({a: rows[a]['label'] for a in rows},
                         {100: 'citizen', 200: 'non_citizen', 300: 'bot', 5554: 'ours', 6383: 'teammate', 8: 'public_figure'})
        self.assertEqual(rows[200]['reasons'], ['heavy'])
        self.assertEqual((rows[200]['actions_total'], rows[200]['writes_48h']), (30, 30))
        self.assertEqual(rows[300]['reasons'], ['always_on', 'high_volume'])
        self.assertEqual(rows[8]['reasons'], ['verified'])
        self.assertTrue(rows[8]['figure_mentioned'])
        self.assertEqual(result['summary'], {'citizen': 1, 'bot': 1, 'non_citizen': 1, 'ours': 1, 'teammate': 1, 'public_figure': 1})
        self.assertTrue(result['read_only'])

    def test_our_accounts_go_through_the_same_rules(self):
        result = account_identity.build(self.db, now=NOW)
        rows = {row['account_id']: row for row in result['rows']}
        # The label stays ours/teammate; the rules' verdict is shown beside it.
        self.assertEqual((rows[6383]['label'], rows[6383]['reasons']), ('teammate', []))
        self.assertIn('profile', rows[6383]['rule_reasons'])  # a username with spaces
        self.assertIsInstance(rows[5554]['rule_reasons'], list)
        self.assertNotIn('rule_reasons', rows[100])
        self.assertEqual(result['own_flagged']['teammate'], {'flagged': 1, 'total': 1})
        self.assertEqual(result['own_flagged']['ours']['total'], 1)
        self.assertEqual(rows[6383]['rule_label'], 'non_citizen')
        self.assertEqual((rows[5554]['rule_label'], rows[5554]['rule_reasons']), ('citizen', []))
        self.assertNotIn('rule_label', rows[100])

    def test_our_stored_bot_reasons_make_the_bot_verdict(self):
        # annotations keeps our accounts at is_bot=0 but stores their reasons.
        conn = connect(self.db)
        with conn:
            conn.execute('INSERT INTO account_annotations(account_id,is_bot,bot_reasons_json,flagged_at,updated_at) '
                         'VALUES(5554,0,?,NULL,?)', (json.dumps(['always_on', 'duplicate_text', 'fast_reply']), stamp(0)))
            conn.execute('INSERT INTO account_annotations(account_id,is_bot,bot_reasons_json,flagged_at,updated_at) '
                         'VALUES(6383,0,?,NULL,?)', (json.dumps(['high_volume']), stamp(0)))
        conn.close()
        rows = {row['account_id']: row for row in account_identity.build(self.db, now=NOW)['rows']}
        self.assertEqual(rows[5554]['label'], 'ours')
        self.assertEqual((rows[5554]['rule_label'], rows[5554]['rule_reasons']), ('bot', ['always_on', 'fast_reply']))
        # One always-on feature is a note only, as for everyone else.
        self.assertEqual(rows[6383]['rule_label'], 'non_citizen')
        self.assertNotIn('high_volume', rows[6383]['rule_reasons'])

    def test_own_risks_list_roles_near_suspension(self):
        conn = connect(self.db)
        with conn:
            conn.execute('UPDATE meta SET value=? WHERE key=?',
                         (json.dumps({'maya': {'name': 'Maya', 'account_id': 5554}}), account_identity.AGENTS_META_KEY))
            conn.executemany('INSERT INTO hidden_posts(post_id,account_id,created_at,detected_at) VALUES(?,?,?,?)',
                             [(1, 5554, stamp(60), stamp(5)), (2, 5554, stamp(60), stamp(5))])
        conn.close()
        result = account_identity.select(account_identity.build(self.db, now=NOW), label='ours')
        self.assertEqual([(r['account_id'], r['level']) for r in result['own_risks']], [(5554, 'suspend_next')])
        self.assertEqual(result['own_risks'][0]['text'], 'Maya（5554）已有 2 条帖子被隐藏，再被隐藏 1 条就可能封号')
        self.assertEqual(account_identity.build(self.db, now=NOW)['own_risks'], result['own_risks'])

    def test_contact_with_us_and_stance(self):
        result = account_identity.build(self.db, now=NOW)
        tessa = next(row for row in result['rows'] if row['account_id'] == 100)
        self.assertEqual((tessa['our_replies_24h'], tessa['answered_us_24h']), (1, 1))
        self.assertEqual((tessa['posts_24h'], tessa['replies_24h']), (1, 1))
        self.assertEqual(tessa['stance'], 'undecided')
        self.assertEqual(tessa['last_excerpt'], 'Thanks, that helps.')
        self.assertEqual(result['citizens_answering_us'], 1)
        loud = next(row for row in result['rows'] if row['account_id'] == 200)
        self.assertEqual(loud['stance'], 'no')

    def test_citizens_are_exactly_the_targeting_rule(self):
        result = account_identity.build(self.db, now=NOW)
        active = {row['account_id'] for row in result['rows']}
        citizens = agent_targeting.citizen_accounts(self.db, active, {5554}, now=NOW.timestamp())
        self.assertEqual({row['account_id'] for row in result['rows'] if row['label'] == 'citizen'}, citizens)

    def test_select_filters_by_label_and_name_or_id(self):
        result = account_identity.build(self.db, now=NOW)
        self.assertEqual([row['account_id'] for row in account_identity.select(result, label='bot')['rows']], [300])
        self.assertEqual([row['account_id'] for row in account_identity.select(result, q='@Tess')['rows']], [100])
        self.assertEqual([row['account_id'] for row in account_identity.select(result, q='#300')['rows']], [300])
        limited = account_identity.select(result, limit=2)
        self.assertEqual((len(limited['rows']), limited['matched']), (2, 6))
        self.assertEqual(limited['summary']['citizen'], 1)
        for bad in ({'label': 'npc'}, {'limit': 0}, {'limit': account_identity.MAX_LIMIT + 1}):
            with self.assertRaises(ValueError):
                account_identity.select(result, **bad)


class EndpointTests(Fixture):
    def test_endpoint_is_read_only_and_filters(self):
        web = Path(self.temp.name) / 'dist'
        web.mkdir()
        (web / 'index.html').write_text('<html></html>')
        client = TestClient(create_app(self.db, backup_dir=Path(self.temp.name) / 'backups', web_root=web),
                            base_url='http://127.0.0.1:8765')
        login(client)
        response = client.get('/api/accounts/identity?label=citizen')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['read_only'])
        self.assertTrue(all(row['label'] == 'citizen' for row in data['rows']))
        self.assertEqual(client.get('/api/accounts/identity?label=npc').status_code, 400)
        self.assertIn(client.post('/api/accounts/identity').status_code, (403, 405))
        self.assertEqual(client.get('/processing/bots').status_code, 200)


if __name__ == '__main__':
    unittest.main()
