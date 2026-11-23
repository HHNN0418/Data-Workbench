"""D99/D100: hidden posts, shared wording and style groups merged into Blue's account cards and plan."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ctn_data import blue_reports, blue_view, copy_patterns
from ctn_data.db import connect, initialize
from tests.test_blue_accounts import STAMP, accounts, item
from tests.test_blue_reports import NOW, memory_db, scenario


def style(group='S100', size=8, hidden=3, teammate=False, linked=0):
    return {'style_group': group, 'group_size': size, 'group_listed': size, 'group_hidden_accounts': hidden,
            'peers': [1, 2], 'examples': [], 'possible_teammate': teammate,
            'teammate_note': '与我方文风同组，可能是未登记的队友' if teammate else None, 'linked_to_ours': linked}


class EvidenceTests(unittest.TestCase):
    def test_style_and_shared_text_are_one_family_that_alone_stays_below_threshold(self):
        self.assertEqual(blue_reports.LOG_ODDS['style_group'], ('writing', 1.0))
        self.assertEqual(blue_reports.LOG_ODDS['style_group_hidden'], ('writing', 1.6))
        self.assertEqual(blue_reports.LOG_ODDS['shared_text_post'][0], 'writing')
        # One family: only its strongest rule counts, and the single-family cap keeps p below the threshold.
        p, support = blue_reports.precision(['style_group_hidden', 'shared_text_post', 'shared_text_author'])
        self.assertEqual(support, 1)
        self.assertLessEqual(p, blue_reports.SINGLE_FAMILY_CAP)
        self.assertEqual(p, blue_reports.precision(['shared_text_post'])[0])
        p, support = blue_reports.precision(['style_group_hidden', 'behaviour_bot_rule'])
        self.assertEqual(support, 2)
        self.assertAlmostEqual(p, 1 / (1 + 2.718281828 ** -(blue_reports.PRIOR_LOG_ODDS + 1.6 + 2.0)), places=4)

    def test_evidence_rule_follows_the_group_s_other_hidden_accounts_and_the_citizen_proxy(self):
        post = {'post_id': 1, 'author_id': 55}
        def rules(entry):
            return [e['rule'] for e in blue_reports._evidence(post, {}, 0, {}, {'signature_of': {}, 'shared': set(),
                    'shared_authors': {}, 'coordination': {}, 'self_repeat': {}, 'chain': set(), 'authors_of': {}},
                    style={55: entry})]
        base = {'id': 'S1', 'size': 6, 'possible_teammate': False, 'linked_to_ours': 0}
        self.assertEqual(rules({**base, 'others_hidden': 2, 'counts': True}), ['style_group_hidden'])
        self.assertEqual(rules({**base, 'others_hidden': 1, 'counts': True}), ['style_group'])
        self.assertEqual(rules({**base, 'others_hidden': 4, 'counts': False}), ['style_group_info'])
        self.assertEqual(rules({**base, 'id': None, 'others_hidden': 0, 'counts': True}), [])

    def test_style_lookup_discounts_the_author_s_own_hidden_post_and_citizens(self):
        found = {55: style(hidden=3), 66: style(hidden=3), 77: style(teammate=True), 88: style(group=None, linked=1)}
        with patch('ctn_data.style_groups.account_evidence', return_value=found), \
                patch('ctn_data.risk_assessment.account_risk', return_value={55: {'hidden': 1}}), \
                patch.object(blue_reports.agent_targeting, 'citizen_accounts', return_value=frozenset({66})):
            result = blue_reports._style(None, 'test.db', {55, 66, 77, 88})
        self.assertEqual((result[55]['others_hidden'], result[55]['counts']), (2, True))
        self.assertEqual((result[66]['others_hidden'], result[66]['counts']), (3, False))
        self.assertEqual((result[77]['possible_teammate'], result[77]['counts']), (True, False))
        self.assertEqual((result[88]['id'], result[88]['linked_to_ours']), (None, 1))

    def test_an_unavailable_citizen_proxy_means_style_is_not_scored(self):
        with patch('ctn_data.style_groups.account_evidence', return_value={55: style()}), \
                patch('ctn_data.risk_assessment.account_risk', return_value={}), \
                patch.object(blue_reports.agent_targeting, 'citizen_accounts', side_effect=RuntimeError('down')):
            self.assertFalse(blue_reports._style(None, 'test.db', {55})[55]['counts'])

    def test_no_style_round_yet_changes_nothing(self):
        with patch('ctn_data.style_groups.account_evidence', return_value={}):
            self.assertEqual(blue_reports._style(None, 'test.db', {55}), {})


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.conn = memory_db()
        self.ids = scenario(self.conn)
        self.addCleanup(self.conn.close)

    def run_with(self, found):
        with patch('ctn_data.style_groups.account_evidence', side_effect=lambda ids: {a: found[a] for a in ids if a in found}), \
                patch.object(blue_reports.agent_targeting, 'citizen_accounts', return_value=frozenset()):
            return blue_reports.candidates(self.conn, now=NOW, limit=50)

    def test_style_evidence_reaches_candidates_and_carries_the_group(self):
        items = {item['post_id']: item for item in self.run_with({801: style(group='S801', hidden=3)})}
        top = items[self.ids['a801']]
        self.assertEqual(top['style_group'], 'S801')
        self.assertIn('style_group_hidden', {e['rule'] for e in top['evidence']})
        self.assertFalse(top['possible_teammate'])

    def test_probable_teammates_never_take_a_place_among_the_others(self):
        baseline = self.run_with({})
        flagged = self.run_with({801: style(group='S801', teammate=True)})
        others = [item for item in flagged if not item['possible_teammate']]
        mates = [item for item in flagged if item['possible_teammate']]
        self.assertEqual({item['author_id'] for item in mates}, {801})
        self.assertNotIn(801, {item['author_id'] for item in others})
        self.assertEqual(flagged[-len(mates):], mates)
        self.assertEqual(len(others), len([item for item in baseline if item['author_id'] != 801]))


class PlanTests(unittest.TestCase):
    def candidate(self, post_id, author, cluster='c1', group=None, **extra):
        return {'post_id': post_id, 'author_id': author, 'cluster_id': cluster, 'style_group': group, 'p': 0.95,
                'ev': 10.0, 'verified': False, 'side': 'no', **extra}

    def test_a_style_group_has_its_own_full_rate_places_beside_the_text_cluster(self):
        today = [{'cluster_id': f'c{n}', 'style_group': 'S1', 'post_id': 100 + n, 'author_id': 500 + n} for n in range(4)]
        plan = blue_reports.daily_plan([self.candidate(1, 55, group='S1'), self.candidate(2, 66, cluster='c9', group='S1'),
                                        self.candidate(3, 77, cluster='c9', group='S2')], today)
        self.assertEqual([item['post_id'] for item in plan['items']], [1, 3])
        self.assertEqual(plan['skipped'], [{'post_id': 2, 'cluster_id': 'c9', 'reason': 'style_group_daily_cap'}])

    def test_probable_teammates_are_never_planned(self):
        plan = blue_reports.daily_plan([self.candidate(1, 55, possible_teammate=True)])
        self.assertEqual((plan['items'], plan['skipped'][0]['reason']), ([], 'possible_teammate'))


class ViewTests(unittest.TestCase):
    def test_probable_teammates_leave_candidates_plan_and_buttons_but_stay_listed(self):
        mate = {**item(1, 55), 'possible_teammate': True, 'style_group': 'S9',
                'author_copy': {'style': style(group='S9', teammate=True)}}
        view = blue_view._view([mate, item(2, 66)], [])
        self.assertEqual([row['account_id'] for row in view['accounts']], [66])
        self.assertEqual([row['post_id'] for row in view['plan']['items']], [2])
        self.assertEqual([row['post_id'] for row in view['candidates']], [2])
        excluded = view['excluded_teammates'][0]
        self.assertEqual((excluded['account_id'], excluded['account_report']['eligible']), (55, False))
        self.assertEqual(excluded['account_report']['missing'][0], 'possible_teammate')
        self.assertEqual(excluded['teammate_note'], '与我方文风同组，可能是未登记的队友')
        self.assertEqual(view['account_summary']['excluded_teammates'], 1)
        self.assertEqual(blue_view._all_items(view)[-1]['post_id'], 1)

    def test_the_style_group_s_places_count_for_account_reports(self):
        today = [{'cluster_id': f'x{n}', 'style_group': 'S1', 'post_id': 100 + n} for n in range(5)]
        rows = accounts([{**item(1, 55, cluster='c7'), 'style_group': 'S1'}], today)
        self.assertIn('style_group_daily_cap', rows[55]['account_report']['missing'])

    def test_accounts_order_by_own_hidden_then_the_group_s_hidden_accounts(self):
        def with_group(post_id, author, group_hidden, cluster):
            return {**item(post_id, author, cluster=cluster, hidden=1, actions=150),
                    'author_copy': {'style': style(hidden=group_hidden)}}
        view = blue_view._view([with_group(1, 55, 1, 'c1'), with_group(2, 66, 6, 'c2'), with_group(3, 77, 3, 'c3')], [])
        self.assertEqual([row['account_id'] for row in view['accounts']], [66, 77, 55])
        self.assertEqual(view['accounts'][0]['group_hidden_accounts'], 6)

    def test_copy_evidence_nests_the_style_group_and_skips_our_and_verified_accounts(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'test.sqlite3'
            initialize(db)
            conn = connect(db)
            with conn:
                conn.execute("INSERT INTO team_accounts(account_id,team_label,is_own,evidence,updated_at) "
                             "VALUES(66,'Agent Zero',0,'',?)", (STAMP,))
            conn.close()
            copy = {55: {'template_share': 0.5, 'peers': [9], 'phrases': []}, 66: {'template_share': 0.9},
                    77: {'template_share': 0.7}}
            rows = [{**item(1, 55)}, {**item(2, 66)}, {**item(3, 77), 'verified': True}, {**item(4, 88)}]
            with patch('ctn_data.copy_patterns.account_evidence', return_value=copy), \
                    patch('ctn_data.style_groups.account_evidence', return_value={55: style(), 88: style(group='S8')}):
                blue_view._attach_accounts(db, rows, [])
        by_author = {row['author_id']: row['author_copy'] for row in rows}
        self.assertEqual(by_author[55]['template_share'], 0.5)
        self.assertEqual(by_author[55]['style']['style_group'], 'S100')
        self.assertIsNone(by_author[66])  # a team account
        self.assertIsNone(by_author[77])  # verified
        self.assertEqual(by_author[88], {'style': style(group='S8')})


class CacheKeyTests(unittest.TestCase):
    def test_one_cache_entry_however_the_database_is_spelled(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'ctn.sqlite3'
            index = {'stats': {55: {'username': 'u', 'group': 'g', 'posts': 3, 'shared_posts': 2, 'shared_no_npc_posts': 1,
                                    'share': 0.66, 'coverage': 0.2, 'fact_posts': 0, 'fact_ids': []}},
                     'families': {}, 'peers': {}, 'accounts': {}, 'generated_at': STAMP}
            self.assertEqual(copy_patterns.cache_key(path), copy_patterns.cache_key(str(path)))
            with patch.dict(copy_patterns._CACHE, {copy_patterns.cache_key(path): {'index': index}}):
                self.assertEqual(copy_patterns.account_evidence(str(path), [55])[55]['template_share'], 0.66)
                self.assertEqual(copy_patterns.account_evidence(path, [55])[55]['template_share'], 0.66)
            with patch.dict('os.environ', {'CTN_DATABASE_URL': 'postgresql://example/ctn'}):
                self.assertEqual(copy_patterns.cache_key(path), 'postgresql://example/ctn')


if __name__ == '__main__':
    unittest.main()
