"""Manual operations respect the review switch and local language guard."""
from contextlib import closing
import json
import unittest
import uuid

from ctn_data.agent_control import AgentError
from ctn_data.db import connect
from tests import test_agent_manual_execution as fixtures
from tests import test_agent_review as review_fixtures


class ManualReviewTests(unittest.TestCase):
    setUp = fixtures.ManualExecutionTests.setUp
    draft = fixtures.ManualExecutionTests.draft
    row = fixtures.ManualExecutionTests.row
    publish = fixtures.ManualExecutionTests.publish

    def enable_review(self):
        current = self.controller.review()['settings']
        self.controller.review({'enabled': True, 'provider_id': 'fake', 'platform_policy': 'hold',
                                'expected_revision': current['revision']})

    def assert_no_review(self, draft_id):
        row = self.row(draft_id)
        self.assertNotIn('review', row)
        self.assertNotIn('_review_approval', row)
        self.assertFalse(row.get('requires_review'))
        with closing(connect(self.controller.store.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_reviews').fetchone()[0], 0)
        self.model.assert_not_called()

    def test_manual_english_with_emoji_publishes_without_review_or_model(self):
        draft_id = self.draft('post', body='Hello world 🙂')
        self.assertEqual(self.publish(draft_id)['state'], 'published')
        self.assertEqual(self.social.writes[-1]['body'], 'Hello world 🙂')
        self.assert_no_review(draft_id)

    def test_manual_clear_nonenglish_is_locally_blocked_without_review_hold(self):
        draft_id = self.draft('post', body='Hello，明天再见 🙂')
        with self.assertRaises(AgentError):
            self.publish(draft_id)
        row = self.row(draft_id)
        self.assertNotIn(row['state'], ('sending', 'uncertain', 'published'))
        self.assertEqual(self.social.writes, [])
        self.assert_no_review(draft_id)

    def test_manual_enabled_review_holds_nonenglish_without_model(self):
        self.enable_review()
        draft_id = self.draft('post', body='Hello，明天再见 🙂')
        with self.assertRaises(AgentError):
            self.publish(draft_id)
        self.assertTrue(self.row(draft_id)['requires_review'])
        self.assertEqual(self.social.writes, [])
        self.model.assert_not_called()

    def test_manual_unknown_latin_and_media_do_not_require_ai_when_disabled(self):
        for body in ('Bonjour tout le monde', 'Hello world https://example.test/photo.png 🙂'):
            self.now += 1
            draft_id = self.draft('post', body=body)
            self.assertEqual(self.publish(draft_id)['state'], 'published')
            self.assertEqual(self.social.writes[-1]['body'], body)
            self.assert_no_review(draft_id)

    def test_manual_english_reply_may_use_nonenglish_parent_context(self):
        self.social.posts[7]['body'] = '这是父帖的阅读背景'
        self.enable_review()
        self.model.return_value = review_fixtures.Reviews.answer(self)
        draft_id = self.draft('reply', body='Hello world 🙂')
        self.assertEqual(self.publish(draft_id)['state'], 'published')
        self.assertEqual(self.social.writes[-1]['reply_to_post_id'], 7)
        self.model.assert_called_once()

    def test_manual_empty_repost_still_checks_propagated_original(self):
        self.social.posts[7]['body'] = '这是一篇中文原帖'
        draft_id = self.draft('repost')
        with self.assertRaises(AgentError):
            self.publish(draft_id)
        self.assertEqual(self.social.writes, [])
        self.assert_no_review(draft_id)

    def test_manual_delete_can_prepare_without_copying_sensitive_target_text(self):
        sensitive = 'api_key: sk-syntheticSensitiveTarget0123456789'
        self.social.posts[71]['body'] = sensitive
        prepared = self.controller.prepare_manual({'agent_id': self.agent_id,
            'action': 'delete_post', 'target_post_id': 71, 'idempotency_key': str(uuid.uuid4())})
        draft_id = prepared['draft']['id']
        self.assertNotIn(sensitive, json.dumps(self.row(draft_id), ensure_ascii=False))
        self.assertEqual(self.publish(draft_id, confirm=71)['state'], 'published')
        self.assertEqual(self.deleted, [71])
        self.model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
