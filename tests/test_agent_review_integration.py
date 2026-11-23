"""All cases use temporary state and a fake social platform; no live writes."""
from contextlib import closing
import unittest
from unittest.mock import patch

from ctn_data.agent_control import AgentError
from ctn_data.db import connect
from tests import test_agent_actions as fixtures
from tests import test_agent_review as review_fixtures


class ReviewDeliveryTests(unittest.TestCase):
    decide = fixtures.AutonomousActions.decide

    def setUp(self):
        fixtures.AutonomousActions.setUp(self)

    def enable_review(self):
        current = self.controller.review()['settings']
        self.controller.review({'enabled': True, 'provider_id': 'fake', 'platform_policy': 'hold',
                                'expected_revision': current['revision']})

    def assert_no_review(self, draft_id):
        row = self.controller.store.read()['drafts'][draft_id]
        self.assertNotIn('review', row)
        self.assertNotIn('_review_approval', row)
        self.assertFalse(row.get('requires_review'))
        with closing(connect(self.controller.store.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_reviews').fetchone()[0], 0)
        self.model.assert_not_called()

    def test_disabled_review_checks_clear_nonenglish_locally_without_hold(self):
        draft = self.decide('post', body='你好，明天再见 🙂')
        self.model.reset_mock()
        with self.assertRaises(AgentError):
            self.controller.publish(draft['id'])
        self.assertEqual(self.social.writes, [])
        self.assert_no_review(draft['id'])

    def test_enabled_review_blocks_chinese_edit_before_model(self):
        draft = self.decide('post', body='Hello world')
        self.enable_review()
        self.model.reset_mock()
        with self.assertRaises(AgentError):
            self.controller.publish(draft['id'], 'Hello，明天再见 🙂')
        state = self.controller.store.read()
        self.assertEqual(self.social.writes, [])
        self.assertNotIn(state['drafts'][draft['id']]['state'], ('sending', 'uncertain', 'published'))
        self.assertTrue(state['drafts'][draft['id']].get('requires_review'))
        self.assertEqual(self.account.resolved, [])
        self.model.assert_not_called()

    def test_enabled_review_allows_combined_and_plain_emoji(self):
        draft = self.decide('post', body='👩🏽‍⚕️ 🇦🇺 🎉 ❤️')
        self.enable_review()
        self.model.reset_mock()
        self.model.return_value = review_fixtures.Reviews.answer(self)
        self.controller.publish(draft['id'])
        self.assertEqual(self.social.writes[-1]['body'], '👩🏽‍⚕️ 🇦🇺 🎉 ❤️')
        self.model.assert_called_once()

    def test_disabled_review_skips_unknown_language_and_media(self):
        for body in ('Bonjour tout le monde', 'Hello world https://example.test/picture.png 🙂'):
            draft = self.decide('post', body=body)
            self.model.reset_mock()
            self.controller.publish(draft['id'])
            self.assertEqual(self.social.writes[-1]['body'], body)
            self.assert_no_review(draft['id'])

    def test_disabled_review_allows_emoji_faces_and_symbols_without_ai(self):
        # Four posts in a row would hit the post, post, reply mix (D38) on the
        # fourth; this test is about review, so keep every round a post.
        patch('ctn_data.agent_decision.next_mix', return_value=None).start()
        for body in ('👩🏽‍⚕️ 🇦🇺 🎉 ❤️', r'¯\_(ツ)_/¯', '(｡･ω･｡)ﾉ♡', '→ ∑ √∞ ≠ 1 + 2 = 3'):
            draft = self.decide('post', body=body)
            self.model.reset_mock()
            self.controller.publish(draft['id'])
            self.assertEqual(self.social.writes[-1]['body'], body)
            self.assert_no_review(draft['id'])

    def test_enabled_review_uses_model_for_unknown_language(self):
        draft = self.decide('post', body='Bonjour tout le monde')
        self.enable_review()
        self.model.reset_mock()
        self.model.return_value = review_fixtures.Reviews.answer(self, language='blocked',
            evidence=[{'field': 'body', 'quote': 'Bonjour', 'start': 0, 'end': 7}])
        with self.assertRaises(AgentError):
            self.controller.publish(draft['id'])
        self.assertEqual(self.social.writes, [])
        self.model.assert_called_once()

    def test_other_uncertain_action_stops_before_language_or_model_review(self):
        draft = self.decide('post', body='An unfamiliar expression.')
        def pending_receipt(data, conn):
            data['drafts']['previous'] = dict(data['drafts'][draft['id']], id='previous', state='uncertain')
        self.controller.store.change(pending_receipt)
        self.model.reset_mock()
        with self.assertRaisesRegex(AgentError, '操作结果待核对'):
            self.controller.publish(draft['id'])
        row = self.controller.store.read()['drafts'][draft['id']]
        self.assertNotIn('review', row)
        self.assertEqual(self.social.writes, [])
        self.model.assert_not_called()

    def test_disabled_review_checks_repost_language_locally_without_hold(self):
        self.social.posts[7]['body'] = '这是一篇中文原帖'
        self.social.posts[7]['media'] = [{'url': 'https://example.test/original.png'}]
        draft = self.decide('repost', body='')
        self.model.reset_mock()
        with self.assertRaises(AgentError):
            self.controller.publish(draft['id'])
        self.assertEqual(self.social.writes, [])
        self.assert_no_review(draft['id'])

    def test_disabled_review_skips_embedded_media_inspection(self):
        self.social.posts[7]['media'] = [{'url': 'https://example.test/original.png', 'caption': '中文图片'}]
        draft = self.decide('repost', body='')
        self.model.reset_mock()
        self.controller.publish(draft['id'])
        self.assertEqual(self.social.writes[-1]['repost_of_post_id'], 7)
        self.assert_no_review(draft['id'])

    def test_disabled_review_does_not_add_final_target_reread(self):
        draft = self.decide('reply', body='Hello world 🙂')
        self.model.reset_mock()
        with patch.object(self.social, 'get_post', wraps=self.social.get_post) as get_post:
            self.controller.publish(draft['id'])
        get_post.assert_called_once_with(7)
        self.assertEqual(self.social.writes[-1]['reply_to_post_id'], 7)
        self.assert_no_review(draft['id'])

    def test_enabled_review_checks_reposted_original_language(self):
        self.social.posts[7]['body'] = '这是一篇中文原帖'
        draft = self.decide('repost', body='')
        self.enable_review()
        self.model.reset_mock()
        with self.assertRaises(AgentError):
            self.controller.publish(draft['id'])
        self.assertEqual(self.social.writes, [])
        self.model.assert_not_called()

    def test_target_changed_at_final_gate_is_proven_unsent(self):
        draft = self.decide('repost', body='')
        self.enable_review()
        self.model.reset_mock()
        self.model.return_value = review_fixtures.Reviews.answer(self)
        calls = 0
        original_get = self.social.get_post
        def changing(post_id):
            nonlocal calls
            calls += 1
            result = dict(original_get(post_id))
            if calls >= 2:
                result['body'] = '目标在审核后被修改'
            return result
        with patch.object(self.social, 'get_post', side_effect=changing):
            with self.assertRaises(AgentError):
                self.controller.publish(draft['id'])
        row = self.controller.store.read()['drafts'][draft['id']]
        self.assertEqual(row['state'], 'rejected')
        self.assertTrue(row.get('delivery_not_started'))
        self.assertNotIn('reservation', row)
        self.assertEqual(self.social.writes, [])

    def test_role_output_language_is_fixed_without_changing_chinese_background(self):
        self.controller.save({'id': self.agent_id, 'name': 'Fictional local',
                              'language': '中文', 'background': '这是中文人设编辑内容'})
        role = self.controller.store.read()['agents'][self.agent_id]
        self.assertEqual(role['language'], 'English')
        self.assertEqual(role['background'], '这是中文人设编辑内容')


if __name__ == '__main__':
    unittest.main()
