"""Concern radar: citizen-only evidence, code-computed numbers, grounded output and Agent context."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ctn_data import agent_pipeline, agent_targeting, concern_radar, content_research, fact_bank
from ctn_data.db import connect, initialize

# Duplicate-text bot detection looks back 24 hours from the wall clock, so the
# fixture must be recent; a fixed date stopped finding the bot cluster a day later.
NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def at(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec='seconds').replace('+00:00', 'Z')


class RadarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'ctn.sqlite3')
        initialize(self.db)
        agent_targeting._cache.clear()
        fact_bank.invalidate()
        conn = connect(self.db)
        with conn:
            fact_bank.initialize(conn)
            conn.execute('INSERT INTO team_accounts VALUES(?,?,?,?,?)', (900, 'Agent Zero', 1, 'manual', at(30)))
            conn.execute('INSERT INTO accounts(account_id,username,first_seen,last_seen) VALUES(?,?,?,?)',
                         (800, 'KingstoniaHerald', at(30), at(1)))
            rows = [
                # Current window: three undecided citizens worried about the ID card, one Yes, one unrelated.
                (1, 101, "Not sure about the vote. Will the national ID card track my health status?", 1),
                (2, 102, "I'm undecided. Worried the referendum lets the ID card become a health passport.", 2),
                (3, 103, "Still unsure: what about privacy and the national ID when we vote?", 3),
                (4, 104, 'Voting yes on November 2, mandate now!', 2),
                (5, 105, 'Lovely sunset over the harbour tonight.', 2),
                # Previous window: one ID worry.
                (6, 106, "Unsure on the referendum, the national ID worries me.", 8),
                # Excluded: our account, the news account and a duplicate-text bot cluster.
                (7, 900, "I'm unsure about the ID card too, says our own account.", 1),
                (8, 800, 'Herald: national ID questions in the referendum, not sure yet.', 1),
            ]
            for post_id, author, body, hours in rows:
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                             (post_id, author, body, at(hours), at(hours), at(hours)))
            copy = 'Exactly the same copied text about the national ID card and the referendum vote here.'
            for post_id, author in ((20, 501), (21, 502), (22, 503)):
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                             (post_id, author, copy, at(2), at(2), at(2)))
            # Our reply (post 30) and a citizen's answer to it (post 31).
            conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen,reply_to_post_id) '
                         'VALUES(?,?,?,?,?,?,?)', (30, 900, 'The certificate is separate from the ID card. Voting Yes.',
                                                   at(5), at(5), at(5), 2))
            conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen,reply_to_post_id) '
                         'VALUES(?,?,?,?,?,?,?)', (31, 102, "Okay, that helps. I'm leaning yes now.", at(4), at(4), at(4), 30))
        conn.close()
        self.state = {'drafts': {'d': {'post_id': 30, 'concern': 'id_privacy',
                                       'fact_ids': ['certificate_separate']}}}

    def tearDown(self):
        agent_targeting._cache.clear()
        fact_bank.invalidate()
        self.tmp.cleanup()

    def evidence(self):
        return concern_radar.build_evidence(self.db, agent_state=self.state, now=NOW)

    def test_counts_only_citizens_and_computes_trends(self):
        evidence = self.evidence()
        population = evidence['population']
        self.assertEqual(population['excluded_accounts'], {'own': 1, 'news': 1, 'suspected_bots': 3})
        self.assertEqual(population['stance_current'].get('yes'), 1)
        stats = {row['concern']: row for row in evidence['concern_stats']}
        self.assertEqual(stats['id_privacy']['posts'], 3)
        self.assertEqual(stats['id_privacy']['previous_posts'], 1)
        sample_authors = {row['id'] for row in evidence['samples']}
        self.assertNotIn('post:7', sample_authors)
        self.assertNotIn('post:8', sample_authors)

    def test_reactions_link_replies_to_our_concern_and_facts(self):
        evidence = self.evidence()
        reaction = next(row for row in evidence['reactions'] if row['concern'] == 'id_privacy')
        self.assertEqual((reaction['replies'], reaction['distinct_repliers']), (1, 1))
        pair = evidence['reply_pairs'][0]
        self.assertEqual((pair['id'], pair['our_post_id'], pair['our_fact_ids']),
                         ('post:31', 'post:30', ['certificate_separate']))

    def test_validation_keeps_grounded_items_and_code_numbers(self):
        evidence = self.evidence()
        facts = [f['id'] for f in fact_bank.catalog(self.db)]
        output = {'top_concerns': [{'concern': 'id_privacy', 'why_now': 'Rising worry.', 'source_ids': ['post:1', 'post:999']},
                                   {'concern': 'not_a_tag', 'why_now': 'x', 'source_ids': []}],
                  'angles': [{'concern': 'id_privacy', 'angle': 'Say the certificate is separate.',
                              'fact_ids': ['certificate_separate', 'made_up'], 'evidence': 'A reply leaned yes.',
                              'source_ids': ['post:31']},
                             {'concern': 'id_privacy', 'angle': 'Paper certificates work too.', 'fact_ids': [],
                              'evidence': '', 'source_ids': []}],
                  'avoid': ['Do not call people paranoid.'], 'fact_gaps': [{'concern': 'venue_list', 'need': 'Venue list.'}],
                  'summary': 'ID-card privacy worries lead among undecided citizens; answer them with the separate certificate fact.'}
        with self.assertRaises(concern_radar.RadarError):
            concern_radar.validate({**output, 'angles': output['angles'][:1]}, evidence, facts)
        with self.assertRaises(concern_radar.RadarError):
            concern_radar.validate({**output, 'summary': 'Too short.'}, evidence, facts)
        radar = concern_radar.validate(output, evidence, facts)
        self.assertEqual([row['concern'] for row in radar['top_concerns']], ['id_privacy'])
        self.assertEqual(radar['top_concerns'][0]['source_ids'], ['post:1'])
        self.assertEqual(radar['top_concerns'][0]['posts'], 3)
        self.assertEqual(radar['angles'][0]['fact_ids'], ['certificate_separate'])
        report = concern_radar.render_report(radar)
        self.assertIn('[post:1]', report)
        with self.assertRaises(concern_radar.RadarError):
            concern_radar.validate({'top_concerns': [], 'angles': []}, evidence, facts)
        schema = concern_radar.schema(fact_bank.CONCERNS, facts, [s['id'] for s in evidence['sources']])
        self.assertIn('summary', schema['required'])

    def test_review_fixes_keep_reports_activatable(self):
        # 1. A non-public post and a post by a private author never enter the evidence.
        conn = connect(self.db)
        with conn:
            conn.execute('INSERT INTO accounts(account_id,username,private,first_seen,last_seen) VALUES(?,?,?,?,?)',
                         (777, 'secretive', 1, at(30), at(1)))
            conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen,visibility) '
                         'VALUES(?,?,?,?,?,?,?)', (40, 108, 'Not sure: will the national ID card track health data?',
                                                   at(1), at(1), at(1), 'followers'))
            conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                         (41, 777, 'Undecided. Will the national ID card become a health passport for us?', at(1), at(1), at(1)))
        conn.close()
        evidence = self.evidence()
        ids = {source['id'] for source in evidence['sources']}
        self.assertFalse({'post:40', 'post:41'} & ids)
        # 2 and 3. Free-text citations are stripped and non-dict items are skipped.
        facts = [f['id'] for f in fact_bank.catalog(self.db)]
        output = {'top_concerns': ['id_privacy', {'concern': 'id_privacy', 'why_now': 'See [post:30] and [post:1].',
                                                  'source_ids': ['post:1']}],
                  'angles': [None, {'concern': 'id_privacy', 'angle': 'Separate certificate [post:30].', 'fact_ids': [],
                                    'evidence': 'Reply [post:30] leaned yes.', 'source_ids': ['post:31']},
                             {'concern': 'id_privacy', 'angle': 'Paper proof works.', 'fact_ids': 'oops',
                              'evidence': '', 'source_ids': []}],
                  'avoid': ['No slogans [news:x].', 7], 'fact_gaps': ['gap'],
                  'summary': 'ID-card privacy worries lead among undecided citizens [post:30]; answer with the separate certificate.'}
        radar = concern_radar.validate(output, evidence, facts)
        report = concern_radar.render_report(radar)
        self.assertNotIn('[post:30]', report)
        self.assertEqual(radar['angles'][1]['fact_ids'], [])
        saved = content_research.save_report(self.db, report=report, evidence=evidence, provider_id='p', model='m',
                                             scope={'kind': 'concern_radar'}, radar=radar)
        brief = next(row for row in saved['briefs'] if row['brief_id'] == saved['created_brief_id'])
        self.assertEqual(brief['citation_status'], 'valid')
        self.assertTrue(brief['can_activate'])

    def test_saved_radar_feeds_agents_compactly(self):
        evidence = self.evidence()
        radar = concern_radar.validate({'top_concerns': [{'concern': 'id_privacy', 'why_now': 'Rising.', 'source_ids': ['post:1']}],
                                        'angles': [{'concern': 'id_privacy', 'angle': 'Separate certificate.',
                                                    'fact_ids': ['certificate_separate'], 'evidence': '', 'source_ids': []},
                                                   {'concern': 'hospital', 'angle': 'ICU beds are few.',
                                                    'fact_ids': ['icu_capacity'], 'evidence': '', 'source_ids': []}],
                                        'avoid': ['No slogans.'], 'fact_gaps': [], 'summary': 'ID-card privacy worries lead among undecided citizens; answer them with the separate certificate fact.'},
                                       evidence, [f['id'] for f in fact_bank.catalog(self.db)])
        saved = content_research.save_report(self.db, report=concern_radar.render_report(radar), evidence=evidence,
                                             provider_id='p', model='m', scope={'kind': 'concern_radar'}, radar=radar,
                                             expires_hours=concern_radar.EXPIRES_HOURS)
        brief = next(row for row in saved['briefs'] if row['brief_id'] == saved['created_brief_id'])
        self.assertEqual(brief['kind'], 'concern_radar')
        content_research.activate(self.db, brief['brief_id'], saved['revision'])
        context = content_research.context_for(self.db)
        self.assertEqual(context['kind'], 'concern_radar')
        self.assertLess(len(json.dumps(context)), 1500)
        self.assertEqual(context['priorities'][0]['concern'], 'id_privacy')
        # Targeting: a persuadable post raising the priority concern gets the radar bonus.
        self.assertEqual(agent_targeting.radar_weights(self.db), {'id_privacy': 1})  # low volume, not rising
        # Facts recommended by the radar are offered first; the writer gets the matching angle.
        offered = agent_pipeline.offered_facts(self.db, [], context)
        self.assertEqual(offered[0]['id'], 'certificate_separate')
        messages = agent_pipeline.write_messages(
            {'name': 'A', 'stance': 's', 'voice': 'v'},
            {'action': 'post', 'concern': 'id_privacy', 'angle': ''}, [], offered[:2], db_path=self.db,
            memory_view={}, recent_posts=[], tags=None, feedback=None, strategy=context)
        payload = json.loads(messages[1]['content'])
        self.assertEqual(payload['ANGLE_HINTS'][0]['angle'], 'Separate certificate.')
        self.assertEqual(payload['AVOID'], ['No slogans.'])
        self.assertNotIn('WRITING_REFERENCE', payload)
        decide = agent_pipeline.decide_messages({'name': 'A'}, {'allowed_actions': ['post']}, offered[:2],
                                                team_goal='Vote Yes', blocked=False, strategy=context)
        self.assertIn('concern radar', decide[0]['content'])
        self.assertEqual(json.loads(decide[1]['content'])['priorities']['concerns'][0]['concern'], 'id_privacy')


class RadarAutomationEndpointTest(unittest.TestCase):
    def test_scheduler_runs_radar_and_activates_a_grounded_brief(self):
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from ctn_data.server import create_app
        from tests.auth_helpers import login
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'research.db'
            app = create_app(db)
            client = TestClient(app, base_url='http://127.0.0.1:8765')
            self.addCleanup(client.close)
            token = login(client)
            now = datetime.now(timezone.utc)
            conn = connect(str(db))
            with conn:
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                             (1, 101, 'Not sure about the referendum: will the national ID card track my health?',
                              *(now.isoformat(timespec='seconds').replace('+00:00', 'Z'),) * 3))
            conn.close()
            agent_targeting._cache.clear()
            fact_bank.invalidate()
            output = {'top_concerns': [{'concern': 'id_privacy', 'why_now': 'ID worries.', 'source_ids': ['post:1']}],
                      'angles': [{'concern': 'id_privacy', 'angle': 'Certificate is separate.', 'fact_ids': [],
                                  'evidence': '', 'source_ids': []},
                                 {'concern': 'id_privacy', 'angle': 'Paper proof works.', 'fact_ids': [],
                                  'evidence': '', 'source_ids': []}],
                      'avoid': ['No slogans.'], 'fact_gaps': [], 'summary': 'ID-card privacy worries lead among undecided citizens; answer them with the separate certificate fact.'}
            with patch.object(app.state.ai_providers, 'get', return_value={'id': 'fake', 'protocol': 'openai_chat',
                                                                            'model': 'm', 'enabled': True}), \
                    patch('ctn_data.ai_providers.complete', return_value={'text': json.dumps(output), 'model': 'm'}) as call:
                saved = client.post('/api/ai/research/automation', headers={'X-CTN-Token': token},
                                    json={'expected_revision': 0, 'enabled': True, 'provider_id': 'fake',
                                          'auto_activate': True})
                self.assertEqual(saved.status_code, 200, saved.text)
                self.assertEqual(saved.json()['settings']['mode'], 'radar')
                app.state.research_automation.tick()
            call.assert_called_once()
            self.assertIn('response_schema', call.call_args.kwargs)
            state = client.get('/api/ai/research').json()
            active = next(b for b in state['briefs'] if b['brief_id'] == state['active_brief_id'])
            self.assertEqual(active['kind'], 'concern_radar')
            self.assertEqual(content_research.context_for(str(db))['priorities'][0]['concern'], 'id_privacy')
            agent_targeting._cache.clear()
            fact_bank.invalidate()


class ScheduleCompatibilityTest(unittest.TestCase):
    def test_settings_saved_before_radar_load_in_radar_mode(self):
        from ctn_data import content_research_schedule as schedule
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / 'ctn.sqlite3')
            initialize(db)
            old = {k: v for k, v in schedule.DEFAULTS.items() if k != 'mode'}
            old['interval_seconds'] = 3600
            conn = connect(db)
            with conn:
                conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)',
                             (schedule.KEY, json.dumps({'settings': old, 'status': {k: None for k in schedule.STATUS_FIELDS}}), 'x'))
                state = schedule._load(conn)
            conn.close()
            self.assertEqual(state['settings']['mode'], 'radar')
            self.assertEqual(state['settings']['interval_seconds'], 3600)


if __name__ == '__main__':
    unittest.main()
