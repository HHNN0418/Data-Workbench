"""Operator account pages remain authenticated, bounded and separate from memory."""
import json
import unittest

from ctn_data.agent_control import AgentError
from ctn_data.agent_manual import inventory
from tests import test_agent_manual_prepare as manual_fixtures


class Page(list):
    def __init__(self, rows=(), next_cursor=None):
        super().__init__(rows)
        self.next_cursor = next_cursor


class AgentManualInventoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = manual_fixtures.AgentManualPrepareTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.controller = self.fixture.controller
        self.sdk = self.fixture.sdk
        self.agent_id = self.fixture.agent_id
        self.page = Page([{'post_id': 7, 'author_id': 42, 'body': 'Actual own post',
                           'kind': 'post', 'token': 'SECRET-MUST-NOT-LEAK'}], 'page-2')
        self.inventory_calls = []
        self.after_inventory = None
        self.error = None

        def method(kind):
            def get(actor_id, *, limit, cursor):
                self.sdk.read(('inventory', kind))
                self.inventory_calls.append((kind, actor_id, limit, cursor))
                if self.error:
                    raise self.error
                if self.after_inventory:
                    self.after_inventory()
                return self.page
            return get
        self.sdk.actor.account_posts = method('posts')
        self.sdk.actor.account_likes = method('likes')
        self.sdk.actor.following = method('following')

    def test_own_posts_use_bound_account_and_real_fields_not_role_identity(self):
        result = inventory(self.controller, self.agent_id, 'posts')
        self.assertEqual(self.inventory_calls, [('posts', 42, 20, None)])
        self.assertEqual(result['scope'], 'bound_account')
        self.assertEqual(result['coverage'], 'one_page')
        self.assertEqual(result['account_id'], 42)
        self.assertEqual(result['items'][0]['post_id'], 7)
        self.assertEqual(result['items'][0]['author_id'], 42)
        self.assertEqual(result['items'][0]['body'], 'Actual own post')
        self.assertIsNone(result['items'][0]['like_count'])
        self.assertIsNone(result['items'][0]['username'])
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(result['next_cursor'], 'page-2')
        self.assertTrue(result['has_more'])
        self.assertEqual(self.sdk.closed, 1)

    def test_likes_preserve_other_author_as_context_without_claiming_own_posts(self):
        self.page = Page([{'post_id': 8, 'author_id': 9, 'body': 'Someone else wrote this',
                           'like_count': 0, 'reply_count': 3, 'private_token': 'SECRET'}])
        result = inventory(self.controller, self.agent_id, 'likes', limit=5)
        self.assertEqual(self.inventory_calls, [('likes', 42, 5, None)])
        self.assertEqual(result['items'][0]['author_id'], 9)
        self.assertEqual(result['items'][0]['like_count'], 0)
        self.assertEqual(result['items'][0]['reply_count'], 3)
        self.assertNotIn('SECRET', json.dumps(result))

    def test_following_uses_authenticated_actor_and_allowlists_actual_profile_fields(self):
        self.page = Page([{'account_id': 53, 'username': 'actual_target', 'display_name': 'Actual name',
                           'verified': False, 'private': True, 'token': 'SECRET',
                           'email': 'unrequested@example.invalid', 'role': 'private-role'}])
        result = inventory(self.controller, self.agent_id, 'following', limit=10, cursor='opaque-cursor')
        self.assertEqual(self.inventory_calls, [('following', 42, 10, 'opaque-cursor')])
        self.assertEqual(result['items'][0], {'account_id': 53, 'username': 'actual_target',
            'display_name': 'Actual name', 'created_at': None, 'verified': False, 'private': True})
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('private-role', json.dumps(result))

    def test_reading_operator_inventory_creates_no_draft_or_agent_reading_memory(self):
        before = self.controller.store.read()
        inventory(self.controller, self.agent_id, 'posts')
        after = self.controller.store.read()
        self.assertEqual(after['memory'], before['memory'])
        self.assertEqual(after['drafts'], before['drafts'])
        self.assertEqual(after['agents'], before['agents'])
        self.assertEqual(after['events'], before['events'])
        self.fixture.model.assert_not_called()

    def test_empty_page_with_more_cursor_does_not_claim_absent_relations(self):
        self.page = Page([], 'more')
        result = inventory(self.controller, self.agent_id, 'following')
        self.assertEqual(result['items'], [])
        self.assertTrue(result['has_more'])
        self.assertEqual(result['next_cursor'], 'more')
        self.assertEqual(result['coverage'], 'one_page')

    def test_missing_pagination_metadata_remains_unknown(self):
        self.page = []
        result = inventory(self.controller, self.agent_id, 'following')
        self.assertIsNone(result['has_more'])
        self.assertFalse(result['pagination_metadata_present'])
        self.assertEqual(result['coverage'], 'one_page')

    def test_missing_body_author_and_counts_remain_unknown(self):
        self.page = Page([{'post_id': 5}])
        result = inventory(self.controller, self.agent_id, 'posts')
        self.assertIsNone(result['items'][0]['author_id'])
        self.assertIsNone(result['items'][0]['body'])
        self.assertIsNone(result['items'][0]['kind'])
        self.assertIsNone(result['items'][0]['like_count'])

    def test_wrong_own_post_author_and_invalid_target_ids_are_not_displayed(self):
        for kind, row in (('posts', {'post_id': 5, 'author_id': 99}),
                          ('posts', {'post_id': True, 'author_id': 42}),
                          ('following', {'account_id': True}), ('following', {'account_id': 0})):
            self.page = Page([row])
            with self.subTest(kind=kind, row=row), self.assertRaises(AgentError):
                inventory(self.controller, self.agent_id, kind)
        self.assertEqual(self.controller.store.read()['memory'], {})

    def test_invalid_query_values_fail_before_sdk(self):
        for kind, values in (('bad', {}), ('posts', {'limit': 0}), ('posts', {'limit': 51}),
                             ('posts', {'limit': True}), ('posts', {'cursor': 3}),
                             ('posts', {'cursor': 'x' * 501}), ('posts', {'cursor': 'bad\x00'})):
            with self.subTest(kind=kind, values=values), self.assertRaises(AgentError):
                inventory(self.controller, self.agent_id, kind, **values)
        self.assertEqual(self.sdk.calls, [])

    def test_invalid_page_and_nonadvancing_cursor_fail_safely(self):
        for page, cursor in (({'items': []}, None), (Page([], 12), None),
                             (Page([], 'x' * 501), None), (Page([], 'same'), 'same'),
                             (Page([{'post_id': 7}, {'post_id': 8}]), None)):
            self.page = page
            with self.subTest(page=page, cursor=cursor), self.assertRaises(AgentError):
                inventory(self.controller, self.agent_id, 'posts', limit=1, cursor=cursor)

    def test_binding_change_during_read_prevents_wrong_account_display(self):
        self.after_inventory = lambda: self.controller.store.change(lambda state, conn:
            state['agents'][self.agent_id].update(account_id=99))
        with self.assertRaises(AgentError) as error:
            inventory(self.controller, self.agent_id, 'posts')
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.sdk.closed, 1)
        self.assertEqual(self.controller.store.read()['drafts'], {})

    def test_unbound_or_active_generation_returns_before_sdk(self):
        self.controller.store.change(lambda state, conn: state['agents'][self.agent_id].update(account_state='unbound'))
        with self.assertRaises(AgentError):
            inventory(self.controller, self.agent_id, 'posts')
        self.controller.store.change(lambda state, conn: state['agents'][self.agent_id].update(
            account_state='ready', generation={'status': 'running', 'heartbeat_until': self.controller.clock() + 60}))
        with self.assertRaises(AgentError):
            inventory(self.controller, self.agent_id, 'posts')
        self.assertEqual(self.sdk.calls, [])

    def test_wrong_session_is_not_used_and_upstream_error_is_visible(self):
        self.sdk.actor.account_id = 99
        with self.assertRaises(AgentError):
            inventory(self.controller, self.agent_id, 'posts')
        self.assertEqual(self.inventory_calls, [])
        self.sdk.actor.account_id = 42
        self.error = RuntimeError('connection reset by peer')
        with self.assertRaises(AgentError) as error:
            inventory(self.controller, self.agent_id, 'posts')
        self.assertIn('RuntimeError', str(error.exception))
        self.assertIn('connection reset by peer', str(error.exception))
        self.assertEqual(self.controller.store.read()['memory'], {})


if __name__ == '__main__':
    unittest.main()
