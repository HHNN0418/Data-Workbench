"""Fact bank storage, review states, topic supersession and Agent queries."""
import json
import tempfile
import unittest
from pathlib import Path

from ctn_data import fact_bank
from ctn_data.db import connect, initialize


class FactBankTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'ctn.sqlite3')
        initialize(self.db)
        conn = connect(self.db)
        with conn:
            self.seeded = fact_bank.initialize(conn)
        conn.close()
        fact_bank.invalidate()

    def tearDown(self):
        fact_bank.invalidate()
        self.tmp.cleanup()

    def insert(self, rows, mode, observed='2026-10-21T08:00:00Z', article='herald/a'):
        conn = connect(self.db)
        with conn:
            result = fact_bank.insert_extracted(conn, rows, review_mode=mode, article_key=article,
                                                source_label='Kingstonia Herald, 2026-10-21', observed_at=observed)
        conn.close()
        return result

    def test_seed_imports_once_and_validates_every_tag(self):
        seed = json.loads(fact_bank.SEED_PATH.read_text(encoding='utf-8'))
        self.assertEqual(self.seeded, len(seed['facts']))
        conn = connect(self.db)
        with conn:
            self.assertEqual(fact_bank.initialize(conn), 0)
        conn.close()
        rules = fact_bank.list_rules(self.db)
        self.assertEqual(len(rules['rebuttals']), len(seed['rebuttals']))
        self.assertEqual(len(rules['guardrails']), len(seed['do_not_claim']))
        ids = {f['id'] for f in seed['facts']}
        for rebuttal in seed['rebuttals']:
            self.assertIn(rebuttal['concern'], fact_bank.CONCERNS)
            self.assertTrue(set(rebuttal['fact_ids']) <= ids, rebuttal['concern'])

    def test_operator_edit_survives_reseed(self):
        fact = fact_bank.list_facts(self.db, q='intensive-care')['items'][0]
        fact_bank.decide(self.db, fact['id'], 'edit', expected_revision=fact['revision'],
                         edits={'claim': 'Both hospitals have about 30 ICU beds.'})
        conn = connect(self.db)
        with conn:
            fact_bank.initialize(conn)
        conn.close()
        self.assertEqual(fact_bank.list_facts(self.db, q='icu beds')['items'][0]['id'], fact['id'])

    def test_manual_mode_keeps_news_pending_until_approved(self):
        accepted, _ = self.insert([{'claim': 'A second H2D2 case was confirmed in Kingstonia.',
                                    'evidence_quote': 'a second case', 'concerns': ['not_here_yet']}], 'manual')
        self.assertEqual(accepted, 1)
        pending = fact_bank.list_facts(self.db, status='pending')['items']
        self.assertEqual(len(pending), 1)
        self.assertNotIn(pending[0]['id'], [f['id'] for f in fact_bank.facts_for(self.db, ['not_here_yet'], limit=50)])
        fact_bank.decide(self.db, pending[0]['id'], 'approve', expected_revision=pending[0]['revision'])
        self.assertIn(pending[0]['id'], [f['id'] for f in fact_bank.facts_for(self.db, ['not_here_yet'], limit=50)])

    def test_auto_mode_activates_and_newer_topic_supersedes_older(self):
        self.insert([{'claim': 'Poll: No 36.2%, Yes 33.8%.', 'concerns': ['momentum'], 'topic_key': 'poll:referendum'}],
                    'auto', observed='2026-10-21T07:00:00Z')
        self.insert([{'claim': 'Poll: Yes 37%, No 35%.', 'concerns': ['momentum'], 'topic_key': 'poll:referendum'}],
                    'auto', observed='2026-10-24T07:00:00Z', article='herald/b')
        # A late extraction of an older poll must not replace the newer one.
        self.insert([{'claim': 'Poll: Yes 35.2%, No 35.2%.', 'concerns': ['momentum'], 'topic_key': 'poll:referendum'}],
                    'auto', observed='2026-10-17T07:00:00Z', article='herald/c')
        active = [f['claim'] for f in fact_bank.facts_for(self.db, ['momentum'], limit=10) if 'Poll' in f['claim']]
        self.assertEqual(active, ['Poll: Yes 37%, No 35%.'])
        counts = fact_bank.list_facts(self.db)['counts']
        self.assertEqual(counts.get('superseded'), 2)

    def test_findings_of_one_report_on_a_topic_stay_active_together(self):
        self.insert([{'claim': 'The panel found a strong immune response.', 'concerns': ['safety'],
                      'topic_key': 'vaccine:evidence'},
                     {'claim': 'The panel found no major safety concerns in trials.', 'concerns': ['safety'],
                      'topic_key': 'vaccine:evidence'}], 'auto', observed='2026-10-20T12:30:00Z')
        claims = [f['claim'] for f in fact_bank.facts_for(self.db, ['safety'], limit=50)]
        self.assertIn('The panel found a strong immune response.', claims)
        self.assertIn('The panel found no major safety concerns in trials.', claims)
        # A later report on the topic still replaces both.
        self.insert([{'claim': 'A new review confirmed the vaccine is effective.', 'concerns': ['safety'],
                      'topic_key': 'vaccine:evidence'}], 'auto', observed='2026-10-23T09:00:00Z', article='herald/z')
        claims = [f['claim'] for f in fact_bank.facts_for(self.db, ['safety'], limit=50)]
        self.assertNotIn('The panel found a strong immune response.', claims)
        self.assertIn('A new review confirmed the vaccine is effective.', claims)

    def test_duplicate_claims_are_skipped(self):
        row = {'claim': 'Kingstonia Central Hospital has 40 unfilled nursing posts, and the hospital warned on '
                        '18 October that this limits its ability to handle a surge.', 'concerns': ['hospital']}
        self.assertEqual(self.insert([row], 'auto'), (0, 1))

    def test_facts_for_ranks_overlap_then_news(self):
        self.insert([{'claim': 'The hospital opened a new H2D2 ward.', 'concerns': ['hospital']}], 'auto')
        ranked = fact_bank.facts_for(self.db, ['hospital', 'overreaction'], limit=3)
        self.assertEqual(len(ranked), 3)
        # Facts tagged with both concerns outrank the single-tag news fact.
        self.assertTrue(all({'hospital', 'overreaction'} <= set(f['concerns']) for f in ranked))
        self.assertEqual(fact_bank.facts_for(self.db, []), [])

    def test_core_facts_come_before_news_with_equal_overlap(self):
        self.insert([{'claim': 'Officials said a new arrival was tested at the airport.', 'concerns': ['not_here_yet']}],
                    'auto')
        ranked = fact_bank.facts_for(self.db, ['not_here_yet'], limit=50)
        layers = [f['id'].startswith('n:') for f in ranked]
        self.assertEqual(layers, sorted(layers))
        self.assertTrue(layers[-1])

    def record_use(self, agent, fact_ids, times, at='2026-10-22T01:00:00Z'):
        conn = connect(self.db)
        with conn:
            for index in range(times):
                conn.execute('''INSERT INTO post_attribution(post_id,agent_id,published_at,action,fact_ids_json,
                    captured_at) VALUES(?,?,?,?,?,?)''',
                             (len(agent) * 1000 + index + hash(tuple(fact_ids)) % 500, agent, at, 'reply',
                              json.dumps(fact_ids), at))
        conn.close()

    def test_rotation_prefers_unused_facts_and_caps_reuse_per_account(self):
        now = '2026-10-22T02:00:00Z'
        first = fact_bank.facts_for(self.db, ['not_here_yet'], limit=1, now_iso=now, agent_id='maya')[0]['id']
        self.record_use('ethan', [first], 1)
        # The team used it once: an unused fact with the same overlap now comes first.
        rotated = fact_bank.facts_for(self.db, ['not_here_yet'], limit=1, now_iso=now, agent_id='maya')[0]['id']
        self.assertNotEqual(rotated, first)
        # Over an hour ago, so the team's hourly cap does not leave it out altogether.
        self.record_use('maya', [rotated], fact_bank.REUSE_LIMIT, at='2026-10-22T00:30:00Z')
        ranked = [f['id'] for f in fact_bank.facts_for(self.db, ['not_here_yet'], limit=50, now_iso=now,
                                                         agent_id='maya')]
        self.assertEqual(ranked[-1], rotated)
        # Old uses fall out of the window.
        later = [f['id'] for f in fact_bank.facts_for(self.db, ['not_here_yet'], limit=50,
                                                        now_iso='2026-10-22T09:00:00Z', agent_id='maya')]
        self.assertNotEqual(later[-1], rotated)

    # The hourly team cap is a hard limit (24 Oct). After the reload, check on live data:
    # - no fact is in more than TEAM_HOURLY_LIMIT of our published posts in any hour
    #   (post_attribution.fact_ids_json; phased_after_access peaked at 64 an hour);
    # - fewer than 10% of our original posts say "every eligible Kingstonian" (57% after W35);
    # - the Jev pass rate and the generation failure rate are no worse than before.
    def use_this_hour(self, counts, at):
        """counts: {fact_id: uses}, each use a post by another role at the given time."""
        conn = connect(self.db)
        with conn:
            post_id = 900000 + conn.execute('SELECT COUNT(*) FROM post_attribution').fetchone()[0]
            for fact_id, times in counts.items():
                for _ in range(times):
                    post_id += 1
                    conn.execute('''INSERT INTO post_attribution(post_id,agent_id,published_at,action,fact_ids_json,
                        captured_at) VALUES(?,?,?,?,?,?)''',
                                 (post_id, f'role_{post_id}', at, 'post', json.dumps([fact_id]), at))
        conn.close()

    def test_a_fact_the_team_used_three_times_this_hour_is_left_out(self):
        self.insert([{'claim': 'Officials said a new arrival was tested at the airport.', 'concerns': ['not_here_yet']}],
                    'auto')
        now = '2026-10-22T02:00:00Z'
        ranked = [f['id'] for f in fact_bank.facts_for(self.db, ['not_here_yet'], limit=50, now_iso=now, agent_id='maya')]
        core, news = ranked[0], next(i for i in ranked if i.startswith('n:'))
        self.assertLess(ranked.index(core), ranked.index(news))
        # Other roles, so this role's own reuse cap stays out of it (D90).
        self.use_this_hour({core: fact_bank.TEAM_HOURLY_LIMIT - 1}, '2026-10-22T01:40:00Z')
        below = [f['id'] for f in fact_bank.facts_for(self.db, ['not_here_yet'], limit=50, now_iso=now, agent_id='maya')]
        self.assertLess(below.index(core), below.index(news))
        self.use_this_hour({core: 1}, '2026-10-22T01:40:00Z')
        after = [f['id'] for f in fact_bank.facts_for(self.db, ['not_here_yet'], limit=50, now_iso=now, agent_id='maya')]
        self.assertNotIn(core, after)
        self.assertIn(news, after)
        # An hour later it is back in front of the news fact.
        later = [f['id'] for f in fact_bank.facts_for(self.db, ['not_here_yet'], limit=50,
                                                        now_iso='2026-10-22T02:45:00Z', agent_id='maya')]
        self.assertLess(later.index(core), later.index(news))

    def test_the_widest_fact_is_left_out_however_many_concerns_it_matches(self):
        # phased_after_access matches 6 concerns and outranked every other fact (24 Oct).
        concerns = fact_bank.by_ids(self.db, ['phased_after_access'])[0]['concerns']
        now = '2026-10-24T09:00:00Z'
        self.assertEqual(fact_bank.facts_for(self.db, concerns, limit=12, now_iso=now, agent_id='maya')[0]['id'],
                         'phased_after_access')
        self.use_this_hour({'phased_after_access': fact_bank.TEAM_HOURLY_LIMIT}, '2026-10-24T08:30:00Z')
        ids = [f['id'] for f in fact_bank.facts_for(self.db, concerns, limit=12, now_iso=now, agent_id='maya')]
        self.assertNotIn('phased_after_access', ids)
        self.assertEqual(len(ids), 12)

    def test_a_concern_with_only_capped_facts_gets_its_least_used_one_last(self):
        now = '2026-10-24T09:00:00Z'
        rushed = [f['id'] for f in fact_bank.facts_for(self.db, ['rushed'], limit=50)]
        self.assertGreaterEqual(len(rushed), 3)
        least = rushed[1]
        self.use_this_hour({fact_id: fact_bank.TEAM_HOURLY_LIMIT + (0 if fact_id == least else 3)
                            for fact_id in rushed}, '2026-10-24T08:30:00Z')
        self.assertEqual([f['id'] for f in fact_bank.facts_for(self.db, ['rushed'], limit=50, now_iso=now,
                                                                agent_id='maya')], [least])
        # With another concern still open, the fallback comes after all of its facts.
        mixed = [f['id'] for f in fact_bank.facts_for(self.db, ['rushed', 'children'], limit=50, now_iso=now,
                                                       agent_id='maya')]
        self.assertEqual(mixed[-1], least)
        self.assertEqual(set(mixed) & set(rushed), {least})
        self.assertNotIn(least, [f['id'] for f in fact_bank.facts_for(self.db, ['children'], limit=50, now_iso=now,
                                                                       agent_id='maya')])

    def test_a_capped_partner_is_not_pulled_in_with_one_in_twenty(self):
        concerns = fact_bank.by_ids(self.db, ['one_in_twenty'])[0]['concerns']
        now = '2026-10-24T09:00:00Z'
        self.use_this_hour({'icu_capacity': fact_bank.TEAM_HOURLY_LIMIT}, '2026-10-24T08:30:00Z')
        for limit in range(1, 30):
            ids = [f['id'] for f in fact_bank.facts_for(self.db, concerns, limit=limit, now_iso=now, agent_id='maya')]
            self.assertNotIn('icu_capacity', ids)
            if 'one_in_twenty' in ids:
                self.assertIn('surge_150', ids)
        self.use_this_hour({'surge_150': fact_bank.TEAM_HOURLY_LIMIT}, '2026-10-24T08:30:00Z')
        ids = [f['id'] for f in fact_bank.facts_for(self.db, concerns, limit=50, now_iso=now, agent_id='maya')]
        self.assertFalse({'one_in_twenty', 'icu_capacity', 'surge_150'} & set(ids), ids)

    def attribute(self, post_id, draft_id, fact_ids, at):
        conn = connect(self.db)
        with conn:
            conn.execute('''INSERT INTO post_attribution(post_id,agent_id,draft_id,published_at,action,fact_ids_json,
                captured_at) VALUES(?,?,?,?,?,?,?)''', (post_id, 'leah', draft_id, at, 'post', json.dumps(fact_ids), at))
        conn.close()

    def test_drafts_not_yet_attributed_count_once(self):
        # post_attribution lands about every 10 minutes; drafts fill the gap without double counts.
        from datetime import datetime, timezone
        from ctn_data import agent_pipeline
        now = datetime(2026, 10, 24, 3, 30, tzinfo=timezone.utc)
        self.attribute(700, 'd-attributed', ['wet_season'], '2026-10-24T03:10:00Z')
        used = fact_bank.usage(self.db, 'maya', now_iso='2026-10-24T03:30:00Z')
        self.assertIn(('draft', 'd-attributed'), used[3])
        self.assertIn(('post', 700), used[3])
        draft = lambda i, state, **kw: {'id': i, 'agent_id': 'ethan', 'action': 'post', 'state': state,
                                        'fact_ids': ['wet_season'], **kw}
        drafts = {
            # Already in post_attribution, by draft id and by post id: not counted again.
            'a': draft('d-attributed', 'published', post_id=700, published_at='2026-10-24T03:10:00Z'),
            'b': draft('d-other-id', 'published', post_id=700, published_at='2026-10-24T03:10:00Z'),
            # Published, not captured yet; and two still being sent.
            'c': draft('d-new', 'published', post_id=701, published_at='2026-10-24T03:25:00Z'),
            'd': draft('d-sending', 'sending', created_at='2026-10-24T03:28:00Z'),
            'e': draft('d-pending', 'pending', created_at='2026-10-24T03:29:00Z', agent_id='maya'),
            # Not uses: stuck in sending for over an hour, discarded, a like, published before the window.
            'f': draft('d-stuck', 'uncertain', created_at='2026-10-24T02:00:00Z'),
            'g': draft('d-discarded', 'discarded', created_at='2026-10-24T03:20:00Z'),
            'h': {**draft('d-like', 'published', post_id=702, published_at='2026-10-24T03:20:00Z'), 'action': 'like'},
            'i': draft('d-old', 'published', post_id=703, published_at='2026-10-23T20:00:00Z'),
        }
        mine, team, recent, _ = agent_pipeline.with_unattributed(used, drafts, 'maya', now=now)
        self.assertEqual(recent['wet_season'], 4)  # 1 attributed + d-new + d-sending + d-pending
        self.assertEqual(team['wet_season'], 4)
        self.assertEqual(mine['wet_season'], 1)
        # An hour-old published draft still counts toward the 6-hour rotation, not the hourly cap.
        drafts['j'] = draft('d-earlier', 'published', post_id=704, published_at='2026-10-24T02:00:00Z')
        _, team, recent, _ = agent_pipeline.with_unattributed(used, drafts, 'maya', now=now)
        self.assertEqual((team['wet_season'], recent['wet_season']), (5, 4))

    def test_offered_facts_caps_a_fact_the_store_used_before_attribution(self):
        from datetime import datetime, timedelta, timezone
        from ctn_data import agent_pipeline
        stamp = lambda minutes: (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(
            timespec='seconds').replace('+00:00', 'Z')
        self.use_this_hour({'phased_after_access': fact_bank.TEAM_HOURLY_LIMIT - 1}, stamp(20))
        offer = lambda drafts: [f['id'] for f in agent_pipeline.offered_facts(
            self.db, [], None, agent_id='maya', concerns=['access', 'timing'], drafts=drafts)]
        self.assertIn('phased_after_access', offer(None))
        drafts = {'x': {'id': 'x', 'agent_id': 'ethan', 'action': 'reply', 'state': 'published', 'post_id': 9001,
                        'published_at': stamp(3), 'fact_ids': ['phased_after_access']}}
        self.assertNotIn('phased_after_access', offer(drafts))

    def test_radar_picks_and_concern_facts_skip_capped_facts(self):
        from datetime import datetime, timedelta, timezone
        from ctn_data import agent_pipeline
        at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec='seconds').replace('+00:00', 'Z')
        strategy = {'kind': 'concern_radar', 'angles': [{'concern': 'access',
                                                         'fact_ids': ['phased_after_access', 'free_vaccine']}]}
        offered = [f['id'] for f in agent_pipeline.offered_facts(self.db, [], strategy, agent_id='maya',
                                                                 concerns=['access', 'timing'])]
        self.assertEqual(offered[0], 'phased_after_access')
        self.use_this_hour({'phased_after_access': fact_bank.TEAM_HOURLY_LIMIT}, at)
        offered = [f['id'] for f in agent_pipeline.offered_facts(self.db, [], strategy, agent_id='maya',
                                                                 concerns=['access', 'timing'])]
        self.assertNotIn('phased_after_access', offered)
        self.assertEqual(offered[0], 'free_vaccine')

    def test_one_in_twenty_is_paired_with_capacity(self):
        concerns = fact_bank.by_ids(self.db, ['one_in_twenty'])[0]['concerns']
        for limit in range(1, 8):
            ids = [f['id'] for f in fact_bank.facts_for(self.db, concerns, limit=limit)]
            self.assertLessEqual(len(ids), limit)
            self.assertEqual(len(ids), len(set(ids)))
            if 'one_in_twenty' in ids:
                self.assertTrue({'icu_capacity', 'surge_150'} & set(ids), ids)

    def test_auto_mode_holds_no_leaning_claims_for_review(self):
        self.insert([{'claim': 'KITB argued the plan would create a two-tier society.', 'concerns': ['two_tier']},
                     {'claim': 'A third pop-up clinic opened in Port Ellis.', 'concerns': ['access']}], 'auto')
        pending = fact_bank.list_facts(self.db, status='pending')['items']
        self.assertEqual([f['claim'] for f in pending], ['KITB argued the plan would create a two-tier society.'])
        self.assertEqual(pending[0]['decision_note'], '可能偏向 No 方，需人工确认')
        self.assertEqual(fact_bank.list_facts(self.db, status='active', layer='news')['total'], 1)

    def test_revision_conflict_and_invalid_claims(self):
        fact = fact_bank.list_facts(self.db, q='under 12')['items'][0]
        with self.assertRaises(fact_bank.FactBankError) as raised:
            fact_bank.decide(self.db, fact['id'], 'retire', expected_revision=fact['revision'] + 1)
        self.assertEqual(raised.exception.status_code, 409)
        with self.assertRaises(fact_bank.FactBankError):
            fact_bank.create_fact(self.db, {'claim': '中文事实', 'concerns': ['children'], 'source_label': 'x'})
        with self.assertRaises(fact_bank.FactBankError):
            fact_bank.create_fact(self.db, {'claim': 'x' * 300, 'concerns': ['children'], 'source_label': 'x'})
        fact_bank.decide(self.db, fact['id'], 'retire', expected_revision=fact['revision'])
        self.assertNotIn(fact['id'], [f['id'] for f in fact_bank.facts_for(self.db, ['children'], limit=20)])

    def test_expired_facts_are_hidden(self):
        fact_bank.create_fact(self.db, {'claim': 'Clinics open late this week.', 'concerns': ['access'],
                                        'source_label': 'operator', 'valid_until': '2026-10-22T00:00:00Z'})
        before = fact_bank.facts_for(self.db, ['access'], limit=50, now_iso='2026-10-21T00:00:00Z')
        after = fact_bank.facts_for(self.db, ['access'], limit=50, now_iso='2026-10-23T00:00:00Z')
        self.assertIn('Clinics open late this week.', [f['claim'] for f in before])
        self.assertNotIn('Clinics open late this week.', [f['claim'] for f in after])


if __name__ == '__main__':
    unittest.main()
