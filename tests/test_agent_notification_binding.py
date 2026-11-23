"""Valid direct targets survive a mistaken, known notification selection."""
import unittest

from ctn_data import agent_decision, agent_thread_context
from ctn_data.agent_control import AgentError
from tests import test_agent_actions as fixtures


def post(post_id, author_id, *, parent=None, root=None, notifications=()):
    return {'id': 'post:' + str(post_id), 'kind': 'post', 'post_id': post_id,
            'post_kind': 'reply' if parent else 'post', 'author_id': author_id,
            'reply_to_post_id': parent, 'root_post_id': root,
            'text': 'A concrete referendum discussion ' + str(post_id), 'version': 'v' + str(post_id),
            'notification_ids': list(notifications)}


class NotificationBindingHelperTests(unittest.TestCase):
    def test_partition_keeps_order_and_does_not_share_a_threads_notifications(self):
        sources = [post(7, 99, parent=6, root=3, notifications=[101]),
                   post(6, 98, parent=3, root=3, notifications=[104, 105]), post(3, 97)]
        kept, dropped = agent_decision.partition_notification_bindings('reply', 6, [105, 101, 104, 105], sources)
        self.assertEqual(kept, [105, 104])
        self.assertEqual(dropped, [101])
        self.assertEqual(sources[0]['notification_ids'], [101])
        for action in ('post', 'skip'):
            self.assertEqual(agent_decision.partition_notification_bindings(action, None, [101, 104], sources),
                             ([101, 104], []))

    def test_schema_enumerates_only_current_known_notification_ids(self):
        sources = [post(7, 99, notifications=[101]), post(8, 98, notifications=[104, 101])]
        shape = agent_decision.response_schema({'account_id': 10, 'allowed_actions': ['reply', 'skip']}, sources)
        notifications = shape['properties']['notification_ids']
        self.assertEqual(notifications['items']['enum'], [101, 104])
        self.assertIn('target_post_id', notifications['description'])
        empty = agent_decision.response_schema({'account_id': 10, 'allowed_actions': ['post', 'skip']},
                                              [post(7, 99)])['properties']['notification_ids']
        self.assertEqual(empty['maxItems'], 0)
        self.assertNotIn('enum', empty['items'])


class NotificationBindingWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.AutonomousActions(methodName='runTest')
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.controller = self.case.controller
        self.agent_id = self.case.agent_id
        self.child = post(7, 99, parent=6, root=3, notifications=[101])
        self.parent = post(6, 98, parent=3, root=3)
        self.root = post(3, 97)
        self.load_sources([self.child])
        self.case.social.posts.update({
            3: {'post_id': 3, 'author_id': 97, 'body': self.root['text']},
            6: {'post_id': 6, 'author_id': 98, 'body': self.parent['text'],
                'reply_to_post_id': 3, 'root_post_id': 3},
            7: {'post_id': 7, 'author_id': 99, 'body': self.child['text'],
                'reply_to_post_id': 6, 'root_post_id': 3},
        })

    def load_sources(self, targets):
        background = {3: self.root, 6: self.parent}
        rows, _ = agent_thread_context.pack(targets, lambda ids: [background[value] for value in ids if value in background])
        self.case.sources = rows

        def inbox(data, conn):
            data['memory'].setdefault(self.agent_id, {})['inbox'] = [
                {'id': value, 'notification_id': value, 'post_id': source['post_id'], 'status': 'unresolved'}
                for source in rows for value in source.get('notification_ids', [])]
        self.controller.store.change(inbox)

    def memory(self):
        return self.controller.store.read()['memory'][self.agent_id]

    def assert_notice_unhandled(self, notification_id):
        self.assertNotIn(notification_id, self.memory().get('decision_notification_ids', []))
        notice = next(row for row in self.memory()['inbox'] if row['id'] == notification_id)
        self.assertEqual(notice['status'], 'unresolved')
        self.assertNotIn(notification_id, self.case.account.resolved)

    def test_reply_to_root_keeps_root_target_and_leaves_child_notice_for_later(self):
        draft = self.case.decide('reply', target_post_id=3, source_ids=['post:3'], notification_ids=[101])
        self.assertEqual(draft['state'], 'pending')
        self.assertEqual(draft['target_post_id'], 3)
        self.assertEqual(draft['notification_ids'], [])
        self.assertEqual(draft['notification_binding_adjustment'],
                         {'target_post_id': 3, 'dropped_notification_ids': [101]})
        self.assertEqual(self.case.model.call_count, 1)
        self.assert_notice_unhandled(101)
        self.controller.publish(draft['id'])
        self.assertEqual(self.case.social.writes[0]['reply_to_post_id'], 3)
        self.assert_notice_unhandled(101)
        events = self.controller.logs(self.agent_id, kind='notification_binding_adjusted')['items']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['details']['target_post_id'], 3)
        self.assertEqual(events[0]['details']['dropped_notification_ids'], [101])
        self.assertEqual(events[0]['details']['dropped_notification_count'], 1)
        self.assertNotIn(draft['body'], events[0]['message'])
        prompt = self.case.model.call_args.args[2][0]['content']
        self.assertIn('通知不能跨帖转移', prompt)

    def test_reply_to_parent_does_not_resolve_child_notice_or_change_target(self):
        draft = self.case.decide('reply', target_post_id=6, source_ids=['post:6'], notification_ids=[101])
        self.assertEqual(draft['target_post_id'], 6)
        self.assertEqual(draft['notification_ids'], [])
        self.controller.publish(draft['id'])
        self.assertEqual(self.case.social.writes[0]['reply_to_post_id'], 6)
        self.assertEqual(self.case.social.writes[0]['root_post_id'], 3)
        self.assert_notice_unhandled(101)

    def test_correct_direct_reply_binding_is_preserved_and_confirmed(self):
        draft = self.case.decide('reply', notification_ids=[101])
        self.assertEqual(draft['target_post_id'], 7)
        self.assertEqual(draft['notification_ids'], [101])
        self.assertNotIn('notification_binding_adjustment', draft)
        self.controller.publish(draft['id'])
        self.assertEqual(self.case.social.writes[0]['reply_to_post_id'], 7)
        self.assertEqual(self.case.social.writes[0]['root_post_id'], 3)
        self.assertEqual(self.case.account.resolved, [101])

    def test_mixed_notification_ids_keep_only_direct_targets_notification(self):
        self.parent = post(6, 98, parent=3, root=3, notifications=[104])
        self.load_sources([self.child, self.parent])
        draft = self.case.decide('reply', target_post_id=6, source_ids=['post:6'], notification_ids=[101, 104])
        self.assertEqual(draft['notification_ids'], [104])
        self.assertEqual(draft['notification_binding_adjustment']['dropped_notification_ids'], [101])
        self.controller.publish(draft['id'])
        self.assertEqual(self.case.account.resolved, [104])
        self.assert_notice_unhandled(101)
        self.assertIn(104, self.memory()['decision_notification_ids'])

    def test_unrelated_known_notice_is_dropped_without_a_second_model_call(self):
        self.load_sources([self.child, post(8, 98)])
        draft = self.case.decide('like', target_post_id=8, source_ids=['post:8'], notification_ids=[101])
        self.assertEqual(draft['target_post_id'], 8)
        self.assertEqual(draft['notification_ids'], [])
        self.controller.publish(draft['id'])
        self.assertEqual(self.case.social.like_calls, [(self.case.account.account_id, 8)])
        self.assertEqual(self.case.model.call_count, 1)
        self.assert_notice_unhandled(101)

    def test_unknown_or_forged_notice_ids_still_reject_the_entire_decision(self):
        for ids in ([999], [101, 999], [True], ['101']):
            with self.subTest(ids=ids), self.assertRaisesRegex(AgentError, '通知不在本次阅读资料中'):
                self.case.decide('reply', target_post_id=3, source_ids=['post:3'], notification_ids=ids)
        self.assertEqual(self.controller.snapshot()['drafts'], [])
        self.assertEqual(self.case.account.resolved, [])

    def test_relation_actions_still_cannot_handle_notifications(self):
        for action in ('follow', 'unfollow', 'unlike'):
            fields = ({'target_account_id': 98, 'target_post_id': None} if action != 'unlike'
                      else {'target_account_id': None, 'target_post_id': 7})
            with self.subTest(action=action), self.assertRaisesRegex(AgentError, '不能自动解决通知'):
                self.case.decide(action, body='', source_ids=['post:7'], notification_ids=[101], **fields)
        self.assertEqual(self.controller.snapshot()['drafts'], [])

    def test_unrelated_original_post_does_not_automatically_select_notifications(self):
        draft = self.case.decide('post', body='A separate original viewpoint.',
                                 source_ids=['post:3'], notification_ids=[])
        self.assertEqual(draft['notification_ids'], [])
        self.controller.publish(draft['id'])
        self.assert_notice_unhandled(101)

    def test_original_response_keeps_its_explicit_notice_contract(self):
        draft = self.case.decide('post', body='An original answer to the question I received.',
                                 source_ids=['post:7'], notification_ids=[101])
        self.assertEqual(draft['notification_ids'], [101])
        self.assertNotIn('notification_binding_adjustment', draft)
        self.controller.publish(draft['id'])
        self.assertEqual(self.case.account.resolved, [101])


if __name__ == '__main__':
    unittest.main()
