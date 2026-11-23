"""D24/D26: ranked use of the shared post allowance (manual > notification replies > replies > posts)."""
import tempfile
import unittest
from pathlib import Path

from ctn_data import agent_budget as budget
from ctn_data.agent_control import AgentController


class PublishPriorityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.now = 1792450000.0
        path = Path(folder.name) / 'budget.sqlite3'
        self.controller = AgentController(path, Path(folder.name) / 'secrets', clock=lambda: self.now)
        self.other = AgentController(path, Path(folder.name) / 'secrets', clock=lambda: self.now)

    def drain(self):
        # The official post allowance is 3 per minute with continuous refill (1 per 20 s).
        for _ in range(3):
            budget.reserve(self.controller, 'post')

    def reserve(self, level=None, key=None, controller=None):
        controller = controller or self.controller
        if level is None:
            return budget.reserve(controller, 'post')
        with budget.publish_priority(level, key=key):
            return budget.reserve(controller, 'post')

    def holds(self):
        return self.controller.store.read()['rate'].get('priority_holds', {}).get('post', {})

    def test_priority_follows_manual_then_notification_reply_then_reply_then_post(self):
        cases = (({'origin': 'manual', 'action': 'post'}, 0), ({'manual_prepared': True, 'action': 'reply'}, 0),
                 ({'action': 'reply', 'notification_ids': [7]}, 1), ({'action': 'reply'}, 2),
                 ({'action': 'post'}, 3), ({'action': 'repost'}, 3), (None, 3))
        for draft, expected in cases:
            with self.subTest(draft=draft):
                self.assertEqual(budget.priority_for(draft), expected)

    def test_deferred_manual_post_keeps_the_next_slot(self):
        self.drain()
        with self.assertRaises(budget.Deferred):
            self.reserve(0, 'manual-draft')
        self.assertEqual(self.holds()['manual-draft']['priority'], 0)
        self.now += 20  # one slot refills
        for level in (None, 3, 2, 1):
            with self.subTest(level=level), self.assertRaises(budget.Deferred):
                self.reserve(level, None if level is None else 'auto-' + str(level), controller=self.other)
        self.reserve(0, 'manual-draft')
        self.assertNotIn('manual-draft', self.holds())
        self.now += 20
        # The deferred notification reply now ranks first; unranked callers rank last.
        with self.assertRaises(budget.Deferred):
            self.reserve(None, controller=self.other)
        self.reserve(1, 'auto-1', controller=self.other)

    def test_lower_holds_never_block_higher_or_equal_priorities(self):
        self.drain()
        with self.assertRaises(budget.Deferred):
            self.reserve(3, 'post-draft')
        self.now += 20
        self.reserve(2, 'reply-draft')
        self.now += 20
        self.reserve(3, 'second-post')  # an equal-priority hold is not ahead of it
        self.now += 20
        self.reserve(3, 'post-draft')
        self.assertEqual(self.holds(), {})

    def test_each_pending_higher_post_keeps_one_slot(self):
        self.drain()
        for key in ('manual-a', 'manual-b'):
            with self.assertRaises(budget.Deferred):
                self.reserve(0, key)
        self.now += 40  # two slots refill, both claimed by the two manual posts
        with self.assertRaises(budget.Deferred) as waiting:
            self.reserve(None)
        self.assertGreater(waiting.exception.retry_after, 0)
        self.reserve(0, 'manual-a')
        self.reserve(0, 'manual-b')
        self.assertEqual(self.holds(), {})

    def test_hold_expires_and_can_be_released(self):
        self.drain()
        with self.assertRaises(budget.Deferred) as waiting:
            self.reserve(0, 'abandoned')
        self.now += waiting.exception.retry_after + budget.PRIORITY_HOLD_GRACE_SECONDS + 1
        self.reserve(None)
        self.assertNotIn('abandoned', self.holds())
        self.now += 60  # refill to the full allowance
        self.drain()
        with self.assertRaises(budget.Deferred):
            self.reserve(0, 'cancelled')
        budget.release_priority(self.controller, 'post', 'cancelled')
        self.assertEqual(self.holds(), {})
        self.now += 20
        self.reserve(None)

    def test_team_policy_cap_also_respects_holds(self):
        budget.configure_quota(self.controller, 'post', 1)
        self.reserve(None)
        with self.assertRaises(budget.Deferred) as waiting:
            self.reserve(0, 'manual-draft')
        self.assertTrue(waiting.exception.quota_policy)
        self.now += 60
        with self.assertRaises(budget.Deferred) as blocked:
            self.reserve(3, 'auto-post')
        self.assertTrue(blocked.exception.quota_policy)
        self.reserve(0, 'manual-draft')

    def test_other_buckets_and_unranked_calls_without_holds_are_unchanged(self):
        self.drain()
        with self.assertRaises(budget.Deferred):
            self.reserve(0, 'manual-draft')
        with budget.publish_priority(3, key='liker'):
            budget.reserve(self.controller, 'like')
        self.assertNotIn('liker', self.holds())


if __name__ == '__main__':
    unittest.main()
