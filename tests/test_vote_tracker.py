"""Hourly Jev voting intention of known NPCs and the Poll 2 forecast (D74), without network access."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from ctn_data import jev, vote_tracker
from ctn_data.ai_providers import ProviderError
from ctn_data.db import connect, initialize
from ctn_data.server import create_app
from tests.auth_helpers import login

NOW = datetime(2026, 10, 23, 3, 30, tzinfo=timezone.utc)   # latest window: 22 Oct 03:00 - 23 Oct 03:00
JEV = {'id': 'jev', 'protocol': 'typesafe', 'enabled': True, 'model': 'jev-latest',
       'base_url': 'https://api.typesafe.ai/v1', 'api_key': 'secret'}


class Store:
    def __init__(self, providers):
        self.providers = providers

    def list_public(self):
        return {'providers': self.providers}

    def get(self, provider_id):
        return next(p for p in self.providers if p['id'] == provider_id)


def fake_label(provider, posts):
    vote = posts[0].split()[0]
    probabilities = {'yes': 0.1, 'no': 0.1, 'unsure': 0.1}
    probabilities[vote] = 0.8
    return {'choice': vote, 'probabilities': probabilities, 'confidence': 0.7, 'model': 'jev-test'}


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'test.sqlite3'
        initialize(self.db)
        self.next_id = 100
        conn = connect(self.db)
        with conn:
            # Before Poll 1: 10 yes, 11 no, 12 unsure, 13 no. Latest: 10 yes, 11 yes, 12 unsure, 14 no.
            for author, text, at in ((10, 'yes jab', '2026-10-20T01:00:00Z'), (11, 'no pass', '2026-10-20T02:00:00Z'),
                                     (12, 'unsure hmm', '2026-10-20T03:00:00Z'), (13, 'no way', '2026-10-20T04:00:00Z'),
                                     (10, 'yes still', '2026-10-22T10:00:00Z'), (11, 'yes now', '2026-10-22T11:00:00Z'),
                                     (12, 'unsure still', '2026-10-22T12:00:00Z'), (14, 'no thanks', '2026-10-22T13:00:00Z'),
                                     # A team or competitor account is never labelled.
                                     (5000, 'yes bot', '2026-10-22T14:00:00Z')):
                self.post(conn, author, text, at)
        conn.close()
        self.calls = []

    def post(self, conn, author, body, at):
        self.next_id += 1
        conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                     (self.next_id, author, body, at, at, at))

    def label(self, provider, posts):
        self.calls.append(posts)
        return fake_label(provider, posts)

    def test_labels_known_npcs_and_forecasts_poll_2(self):
        snap = vote_tracker.run_once(self.db, Store([JEV]), NOW, label=self.label)
        self.assertEqual(snap['status'], 'ok')
        self.assertEqual((snap['baseline']['people'], snap['latest']['people']), (4, 4))
        self.assertEqual(snap['latest']['window'], ['2026-10-22T03:00:00Z', '2026-10-23T03:00:00Z'])
        self.assertNotIn(['yes bot'], self.calls)
        self.assertEqual(snap['changes']['people'], 3)
        self.assertEqual(snap['changes']['net'], {'yes': 1, 'no': -1, 'unsure': 0})
        forecast = snap['forecast']
        self.assertAlmostEqual(sum(forecast['shares'].values()), 100, delta=0.2)
        self.assertGreater(forecast['yes_of_decided'], 48.3)   # more Yes than at Poll 1
        self.assertAlmostEqual(sum(forecast['outcomes'].values()), 1, places=2)
        low, high = forecast['range_90']
        self.assertLessEqual(low, high)
        self.assertEqual(len(snap['history']), 1)
        self.assertEqual(vote_tracker.snapshot(self.db)['forecast'], forecast)

    def test_compares_npcs_we_replied_to_with_the_rest(self):
        conn = connect(self.db)
        with conn:
            target = conn.execute("SELECT post_id FROM posts WHERE author_id=11 AND body='yes now'").fetchone()[0]
            self.next_id += 1
            conn.execute('INSERT INTO posts(post_id,author_id,body,reply_to_post_id,created_at,first_seen,last_seen) '
                         "VALUES(?,6464,'Free at GP clinics.',?,?,?,?)", (self.next_id, target, *('2026-10-22T11:05:00Z',) * 3))
        conn.close()
        effect = vote_tracker.run_once(self.db, Store([JEV]), NOW, label=self.label)['persuasion']
        self.assertEqual((effect['replied']['people'], effect['replied']['net']['yes']), (1, 1))
        self.assertEqual(effect['replied']['shift_points']['no'], -100.0)
        self.assertEqual((effect['not_replied']['people'], effect['not_replied']['net']['yes']), (2, 0))

    def test_a_window_is_labelled_once(self):
        vote_tracker.run_once(self.db, Store([JEV]), NOW, label=self.label)
        calls = len(self.calls)
        snap = vote_tracker.run_once(self.db, Store([JEV]), NOW, label=self.label)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(len(snap['history']), 1)

    def test_unchanged_population_reproduces_poll_1(self):
        rows = [{'choice': 'yes', 'p': {'yes': 0.5, 'no': 0.3, 'unsure': 0.2}}]
        same = vote_tracker._calibrated(vote_tracker._shares(rows), vote_tracker._shares(rows),
                                        {'yes': 0.338, 'no': 0.362, 'unsure': 0.3})
        self.assertAlmostEqual(same['yes'] / (same['yes'] + same['no']), 0.338 / 0.7, places=6)

    def test_without_a_jev_connection_nothing_is_labelled(self):
        snap = vote_tracker.run_once(self.db, Store([{**JEV, 'protocol': 'openai_chat'}]), NOW, label=self.label)
        self.assertEqual(snap['status'], 'no_jev_connection')
        self.assertEqual(self.calls, [])

    def test_route_serves_the_snapshot(self):
        vote_tracker.run_once(self.db, Store([JEV]), NOW, label=self.label)
        client = TestClient(create_app(self.db), base_url='http://127.0.0.1:8765')
        self.addCleanup(client.close)
        login(client)
        self.addCleanup(client.app.state.agent_control.stop)
        response = client.get('/api/polls/forecast')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'ok')


class JevVoteTests(unittest.TestCase):
    def answer(self, probabilities, choice='yes'):
        return {'model': 'jev-latest', 'answers': {'vote': {'type': 'choice', 'choice': choice,
                'probabilities': probabilities, 'confidence': 0.6}}, 'usage': {'input_tokens': 1, 'output_tokens': 0}}

    def test_reads_the_choice_and_probabilities(self):
        with patch('ctn_data.jev._request_json', return_value=self.answer({'yes': 0.7, 'no': 0.1, 'unsure': 0.2})) as call:
            result = jev.vote_intention(JEV, ['I will vote yes.'])
        self.assertEqual((result['choice'], result['probabilities']['yes']), ('yes', 0.7))
        self.assertTrue(call.call_args.args[0].endswith('/systemone'))

    def test_rejects_probabilities_that_do_not_add_up(self):
        with patch('ctn_data.jev._request_json', return_value=self.answer({'yes': 0.7, 'no': 0.7, 'unsure': 0.2})):
            with self.assertRaises(ProviderError):
                jev.vote_intention(JEV, ['I will vote yes.'])


if __name__ == '__main__':
    unittest.main()
