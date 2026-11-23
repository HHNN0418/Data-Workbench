"""Local relationship facts remain distinct from inferred social meaning."""
import unittest

from ctn_data import agent_relationships as relationships


class RelationshipFacts(unittest.TestCase):
    def setUp(self):
        self.data = {'rate': {}, 'drafts': {}, 'memory': {'a': {'summary': 'A subjective note.'}}}
        self.agent = {'id': 'a', 'account_id': 10}

    def observe(self, kind='follow', target=99, present=True, at='2026-10-20T00:00:00Z', **kwargs):
        return relationships.observe(self.data, 10, kind, target, present, at, 'exact authenticated read', **kwargs)

    def test_current_state_replaces_prior_receipt_and_keeps_reversal_time(self):
        self.observe(changed=True)
        self.observe(present=False, at='2026-10-20T08:00:00Z', changed=True)
        result = relationships.snapshot_for(self.data, self.agent)
        self.assertEqual(len(result['relations']), 1)
        self.assertFalse(result['relations'][0]['present'])
        self.assertEqual(result['relations'][0]['last_changed_at'], '2026-10-20T08:00:00Z')

    def test_incomplete_read_is_unknown_and_preserves_last_known_only_as_history(self):
        self.observe()
        self.observe(present=None, at='2026-10-20T01:00:00Z')
        row = relationships.snapshot_for(self.data, self.agent)['relations'][0]
        self.assertIsNone(row['present'])
        self.assertTrue(row['last_known_present'])
        self.assertEqual(row['last_known_at'], '2026-10-20T00:00:00Z')
        self.assertNotIn('friend', row)
        self.assertNotIn('stance', row)

    def test_account_isolation_and_source_relevance(self):
        self.observe(target=99)
        self.observe(target=88, at='2026-10-20T01:00:00Z')
        relationships.observe(self.data, 11, 'follow', 77, True, '2026-10-20T02:00:00Z', 'exact read')
        result = relationships.context_for(self.data, self.agent, [{'author_id': 99, 'post_id': 7}])
        self.assertEqual([row['target_account_id'] for row in result['relations']], [99, 88])

    def test_memory_reset_does_not_falsify_existing_relationship(self):
        self.observe()
        self.data['memory'].clear()
        self.assertTrue(relationships.snapshot_for(self.data, self.agent)['relations'][0]['present'])

    def test_reconcile_preserves_original_change_time_without_strategy_cooldown(self):
        draft = {'action': 'follow', 'target_account_id': 99, 'send_started_at': 1792454400.0}
        row = relationships.record_success(self.data, draft, self.agent, '2026-10-21T00:00:00Z')
        self.assertEqual(row['last_changed_at'], '2026-10-20T00:00:00Z')
        self.assertEqual(relationships.cooldown_remaining(self.data, self.agent, draft, 1792454400.0 + 60), 0)
        self.assertEqual(relationships.context_for(self.data, self.agent, [])['cooldown_seconds'], 0)

    def test_invalid_ids_states_and_time_are_rejected(self):
        for args in ((10, 'follow', True, True, '2026-10-20T00:00:00Z'),
                     (10, 'follow', 99, 'yes', '2026-10-20T00:00:00Z'),
                     (10, 'follow', 99, True, 'invalid')):
            with self.subTest(args=args), self.assertRaises(ValueError):
                relationships.observe(self.data, *args, 'source')

    def test_legacy_exact_like_receipt_survives_memory_clear_and_supplies_unlike_target(self):
        self.data['drafts']['like'] = {'action': 'like', 'account_id': 10, 'state': 'published',
            'target_post_id': 7, 'published_at': '2026-10-20T00:00:00Z'}
        self.data['memory'].clear()
        context = relationships.context_for(self.data, self.agent, [])
        self.assertTrue(context['relations'][0]['present'])
        self.assertEqual(context['relations'][0]['observed_at'], '2026-10-20T00:00:00Z')
        self.assertEqual(context['relations'][0]['source'], 'retained confirmed action receipt')
        self.assertEqual(relationships.allowed_post_targets({'action': 'unlike', 'sources': [],
            'relationship_context': context}), {7})

    def test_legacy_history_uses_latest_action_and_never_overrides_new_unknown_fact(self):
        for action, stamp in (('like', '00'), ('unlike', '08')):
            self.data['drafts'][action] = {'action': action, 'account_id': 10, 'state': 'published',
                'target_post_id': 7, 'published_at': f'2026-10-20T{stamp}:00:00Z'}
        self.assertFalse(relationships.snapshot_for(self.data, self.agent)['relations'][0]['present'])
        self.observe('like', 7, None, '2026-10-20T09:00:00Z')
        result = relationships.snapshot_for(self.data, self.agent)['relations'][0]
        self.assertIsNone(result['present'])
        self.assertEqual(result['observed_at'], '2026-10-20T09:00:00Z')

    def test_other_accounts_pending_drafts_or_subjective_summary_never_create_relationships(self):
        self.data['memory']['a']['summary'] = 'I followed everyone and liked post 7.'
        for draft_id, account_id, state in (('other', 11, 'published'), ('pending', 10, 'pending')):
            self.data['drafts'][draft_id] = {'action': 'follow', 'account_id': account_id, 'state': state,
                'target_account_id': 99, 'published_at': '2026-10-20T00:00:00Z'}
        self.assertEqual(relationships.snapshot_for(self.data, self.agent)['relations'], [])

    def test_readonly_confirmation_preserves_change_time_and_does_not_create_familiarity(self):
        from ctn_data import agent_memory
        self.observe(changed=True)
        original_change = self.data['rate']['relationships']['10:follow:99']['last_changed_at']
        for number, marker in enumerate(({'relationship_already_applied': True},
                                         {'result': {'already_applied': True}})):
            draft = {'id': 'readonly-' + str(number), 'agent_id': 'a', 'account_id': 10,
                'action': 'follow', 'target_account_id': 99, 'state': 'published',
                'body': '', 'published_at': '2026-10-20T02:00:00Z', **marker}
            self.data['drafts'][draft['id']] = draft
            relationships.record_success(self.data, draft, self.agent, '2026-10-20T02:00:00Z')
            agent_memory.record_outcome(self.data, draft, 'published', 'Exact existing relationship verified')
        row = relationships.snapshot_for(self.data, self.agent)['relations'][0]
        self.assertEqual(row['last_changed_at'], original_change)
        self.assertEqual(relationships.cooldown_remaining(self.data, self.agent,
            {'action': 'unfollow', 'target_account_id': 99}, 1792454400 + 3 * 3600), 0)
        memory = agent_memory.public_view(self.data, self.agent)
        self.assertEqual(memory['relationships'], [])
        self.assertTrue(all(row['already_applied'] for row in memory['completed_actions']))


if __name__ == '__main__':
    unittest.main()
