"""D29: manual previews flag risky reply targets, cap mentions, hint style and cite only active facts."""
import unittest
from unittest.mock import patch

from ctn_data import agent_manual
from ctn_data.agent_control import AgentError
from ctn_data.agent_manual import prepare
from ctn_data.db import utc_now
from ctn_data.ingest import save_posts
from tests import test_agent_manual_api as api
from tests import test_agent_manual_prepare as base


class ManualPreflightTests(unittest.TestCase):
    def setUp(self):
        base.AgentManualPrepareTests.setUp(self)

    def payload(self, action='reply', **values):
        return base.AgentManualPrepareTests.payload(self, action, **values)

    def drafts(self):
        return self.controller.store.read()['drafts']

    def posts(self, *rows):
        self.controller.store.change(lambda data, conn: save_posts(conn, list(rows), 'fixture', utc_now()))

    def fact(self, fact_id, status):
        def save(data, conn):
            conn.execute('''INSERT INTO facts(id,layer,status,claim,claim_hash,concerns_json,source_label,
                created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)''',
                (fact_id, 'core', status, 'Claim ' + fact_id, 'hash-' + fact_id, '["hospital"]', 'fixture',
                 utc_now(), utc_now()))
        self.controller.store.change(save)

    def codes(self, result):
        return {row['code']: row['blocking'] for row in result['preview'].get('warnings', [])}

    def test_mention_limits_are_four_for_posts_and_three_for_replies(self):
        names = ' '.join('@npc_' + letter for letter in 'abcde')
        with self.assertRaisesRegex(AgentError, '帖子最多 @ 4'):
            prepare(self.controller, self.payload('post', body='Hello ' + names))
        with self.assertRaisesRegex(AgentError, '回复最多 @ 3'):
            prepare(self.controller, self.payload(body='Hello ' + ' '.join(names.split()[:4])))
        self.assertEqual(self.drafts(), {})
        prepare(self.controller, self.payload('post', body='Hello ' + ' '.join(names.split()[:4])))
        prepare(self.controller, self.payload(body='Hello ' + ' '.join(names.split()[:3])))
        self.assertEqual(len(self.drafts()), 2)

    def test_style_hints_stay_in_the_preview_and_do_not_block(self):
        result = prepare(self.controller, self.payload('post', body='My view — plainly put.'))
        self.assertTrue(result['preview']['style_warnings'])
        self.assertEqual(result['draft']['style_warnings'], [])
        self.assertEqual(result['draft']['body'], 'My view — plainly put.')

    def test_blocking_targets_need_acknowledgement_before_any_draft_exists(self):
        cases = (('bot_like_author', '机器人', {'bot_accounts': frozenset({9})}),
                 ('thread_too_deep', '第 8 层', {'depths_for': {7: 7}}))
        for code, message, mocks in cases:
            with self.subTest(code=code), patch.multiple('ctn_data.agent_targeting', **{
                    name: (lambda value: (lambda *args, **kwargs: value))(value) for name, value in mocks.items()}):
                with self.assertRaisesRegex(AgentError, message) as refused:
                    prepare(self.controller, self.payload())
                self.assertEqual(refused.exception.status_code, 409)
                self.assertEqual(self.drafts(), {})
                result = prepare(self.controller, self.payload(acknowledge_warnings=True))
                self.assertTrue(self.codes(result)[code])
                self.assertEqual(result['draft']['manual_warnings_acknowledged'], [code])
                self.controller.discard(result['draft_id'])
                self.controller.store.change(lambda data, conn: data['drafts'].clear())

    def test_a_stored_teammate_reply_requires_acknowledgement(self):
        self.posts({'post_id': 900, 'author_id': 42, 'body': 'Our earlier reply.', 'reply_to_post_id': 7,
                    'root_post_id': 3, 'created_at': utc_now()})
        with self.assertRaisesRegex(AgentError, '我方已有账号回复过'):
            prepare(self.controller, self.payload())
        self.assertEqual(self.drafts(), {})

    def test_an_in_flight_teammate_reply_requires_acknowledgement(self):
        self.controller.store.change(lambda data, conn: data['drafts'].update({'other': {
            'id': 'other', 'agent_id': 'someone', 'action': 'reply', 'target_post_id': 7, 'state': 'pending'}}))
        with self.assertRaisesRegex(AgentError, '我方已有账号回复过'):
            prepare(self.controller, self.payload())
        result = prepare(self.controller, self.payload(acknowledge_warnings=True))
        self.assertTrue(self.codes(result)['team_already_replied'])

    def test_unverifiable_target_counts_as_blocking(self):
        with patch('ctn_data.agent_targeting.bot_accounts', side_effect=RuntimeError('annotations unavailable')):
            with self.assertRaisesRegex(AgentError, '暂时无法核对'):
                prepare(self.controller, self.payload())

    def test_informational_warnings_are_shown_without_blocking(self):
        with patch('ctn_data.agent_targeting.team_thread_replies', return_value={3: 2}):
            result = prepare(self.controller, self.payload())
        codes = self.codes(result)
        self.assertIs(codes['thread_reply_limit'], False)
        self.assertIs(codes['not_persuadable'], False)
        self.assertNotIn('manual_warnings_acknowledged', result['draft'])

    def test_context_has_thread_author_posts_and_assessment_without_secrets(self):
        self.posts({'post_id': 3, 'author_id': 5, 'body': 'Root question about the vote.', 'created_at': utc_now()},
                   {'post_id': 101, 'author_id': 9, 'body': 'Earlier view on hospital beds.', 'created_at': utc_now()},
                   {'post_id': 102, 'author_id': 9, 'body': 'Token sk-ABCDEFGHIJKLMNOPQRS leaked.', 'created_at': utc_now()})
        context = prepare(self.controller, self.payload())['preview']['context']
        self.assertEqual([row['post_id'] for row in context['thread']], [3])
        self.assertEqual([row['post_id'] for row in context['author_recent']], [101])
        self.assertIn(context['stance'], ('yes', 'no', 'undecided', 'unrelated'))
        self.assertIn('concerns', context)
        stored = self.drafts()[next(iter(self.drafts()))]['manual_preview']['context']
        self.assertEqual(stored, context)

    def test_only_active_facts_and_known_concerns_are_recorded(self):
        self.fact('fixture_active', 'active')
        self.fact('old_claim', 'retired')
        result = prepare(self.controller, self.payload(fact_ids=['fixture_active'], concern='hospital'))
        self.assertEqual(result['draft']['fact_ids'], ['fixture_active'])
        self.assertEqual(result['draft']['concern'], 'hospital')
        with self.assertRaisesRegex(AgentError, '生效中的事实.*old_claim'):
            prepare(self.controller, self.payload(fact_ids=['fixture_active', 'old_claim']))
        with self.assertRaisesRegex(AgentError, '生效中的事实.*missing'):
            prepare(self.controller, self.payload(fact_ids=['missing']))
        for values, message in (({'concern': 'not-a-concern'}, '顾虑类别'),
                                ({'fact_ids': ['x'] * 7}, '最多 6 个'),
                                ({'fact_ids': ['fixture_active'], 'action': 'like', 'body': ''}, '只有发帖')):
            with self.subTest(values=values), self.assertRaisesRegex(AgentError, message):
                action = values.pop('action', 'reply')
                prepare(self.controller, self.payload(action, **values))
        self.assertEqual(len(self.drafts()), 1)

    def test_requests_without_new_fields_keep_their_earlier_identity(self):
        payload = self.payload()
        normalized, _, fingerprint = agent_manual._request(payload)
        self.assertEqual(set(normalized), {'agent_id', 'action', 'body', 'target_post_id', 'target_account_id'})
        _, _, acknowledged = agent_manual._request({**payload, 'acknowledge_warnings': True})
        self.assertNotEqual(acknowledged, fingerprint)
        _, _, unset = agent_manual._request({**payload, 'acknowledge_warnings': False, 'fact_ids': []})
        self.assertEqual(unset, fingerprint)


class ManualPreflightApiTests(unittest.TestCase):
    def setUp(self):
        api.ManualApiTests.setUp(self)

    def prepare(self, *args, **kwargs):
        return api.ManualApiTests.prepare(self, *args, **kwargs)

    def test_prepare_route_accepts_only_typed_optional_fields(self):
        accepted = self.prepare(body='Plain words.', acknowledge_warnings=True, concern='hospital')
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()['draft']['concern'], 'hospital')
        for values in ({'acknowledge_warnings': 'yes'}, {'fact_ids': ['a'] * 7}, {'fact_ids': ['']},
                       {'concern': 'x' * 41}, {'fact_ids': 'icu_capacity'}):
            with self.subTest(values=values):
                response = self.prepare(body='Plain words.', **values)
                self.assertIn(response.status_code, (400, 422), response.text)
        retired = self.prepare(body='Plain words.', fact_ids=['no_such_fact'])
        self.assertEqual(retired.status_code, 400, retired.text)
        self.assertEqual(len(self.social.writes), 0)


if __name__ == '__main__':
    unittest.main()
