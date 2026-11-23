"""Offline discussion-unit packing using only supplied, verifiable sources."""
import copy
import json
import unittest

from ctn_data.agent_thread_context import pack


def post(post_id, *, parent=None, root=None, text='A concrete discussion. ' * 6, **changes):
    return {'id': 'post:' + str(post_id), 'kind': 'post', 'post_id': post_id,
            'post_kind': 'reply' if parent is not None else 'post', 'author_id': 1000 + post_id,
            'reply_to_post_id': parent, 'root_post_id': root,
            'text': text, 'version': 'v' + str(post_id), 'origin': 'notification', **changes}


class ThreadContextTests(unittest.TestCase):
    def loader(self, sources):
        self.calls = []
        def load(ids):
            self.calls.append(list(ids))
            return [copy.deepcopy(sources[value]) for value in ids if value in sources]
        return load

    def assert_bounded(self, rows, metadata, count=20, budget=12000):
        self.assertLessEqual(len(rows), count)
        size = len(json.dumps(rows, ensure_ascii=False))
        self.assertLessEqual(size, budget)
        self.assertEqual(metadata['serialized_characters'], size)
        self.assertEqual(len({row['id'] for row in rows}), len(rows))

    def test_busy_batch_packs_parent_and_root_before_spending_all_slots_on_targets(self):
        targets = [post(value, parent=100 + value, root=1) for value in range(10, 30)]
        background = {100 + value: post(100 + value, parent=1, root=1) for value in range(10, 30)}
        background[1] = post(1)
        rows, meta = pack(targets, self.loader(background))
        self.assert_bounded(rows, meta)
        self.assertEqual(meta['requested_count'], 20)
        self.assertLess(meta['included_count'], 20)
        self.assertGreater(meta['background_count'], 0)
        ids = {row.get('post_id') for row in rows}
        complete = [row for row in rows if not row['context_only'] and row['thread_context']['complete']]
        self.assertTrue(complete)
        for row in complete:
            self.assertIn(row['reply_to_post_id'], ids)
            self.assertIn(row['root_post_id'], ids)
        self.assertLessEqual(len(self.calls), 2)

    def test_many_replies_share_one_root_without_duplicate_quotes(self):
        targets = [post(value, parent=1, root=1) for value in range(10, 29)]
        rows, meta = pack(targets, self.loader({1: post(1)}))
        self.assert_bounded(rows, meta)
        self.assertEqual(meta['included_count'], 19)
        self.assertEqual(meta['background_count'], 1)
        self.assertEqual(sum(row.get('post_id') == 1 for row in rows), 1)
        self.assertEqual(self.calls, [[1]])
        self.assertTrue(all(row['thread_context']['complete'] for row in rows if not row['context_only']))

    def test_missing_posts_are_explicit_without_fabricating_sources(self):
        rows, meta = pack([post(7, parent=6, root=1)], self.loader({}))
        self.assertEqual([row['post_id'] for row in rows], [7])
        missing = rows[0]['thread_context']['missing']
        self.assertEqual({item['post_id'] for item in missing}, {1, 6})
        self.assertEqual({item['reason'] for item in missing}, {'unavailable'})
        self.assertFalse(rows[0]['thread_context']['complete'])
        self.assertEqual(meta['missing_count'], 2)

    def test_direct_parent_can_reveal_missing_root_id(self):
        rows, meta = pack([post(7, parent=6)], self.loader({6: post(6, parent=2, root=1), 1: post(1)}))
        self.assertEqual(self.calls, [[6], [1]])
        self.assertEqual({row['post_id'] for row in rows}, {1, 6, 7})
        thread = rows[0]['thread_context']
        self.assertEqual(thread['root_post_id'], 1)
        self.assertTrue(thread['root_inferred_from_parent'])
        self.assertTrue(thread['complete'])
        self.assertEqual(meta['missing_count'], 0)

    def test_original_parent_is_itself_the_root(self):
        rows, _ = pack([post(7, parent=6)], self.loader({6: post(6)}))
        self.assertEqual(self.calls, [[6]])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['thread_context']['root_post_id'], 6)
        self.assertTrue(rows[0]['thread_context']['complete'])

    def test_missing_root_on_reply_parent_is_not_guessed_or_recursively_fetched(self):
        rows, _ = pack([post(7, parent=6)], self.loader({6: post(6, parent=5), 5: post(5)}))
        self.assertEqual(self.calls, [[6]])
        self.assertEqual({row['post_id'] for row in rows}, {6, 7})
        self.assertEqual(rows[0]['thread_context']['missing'], [
            {'post_id': None, 'role': 'root', 'reason': 'unknown_id'}])

    def test_long_units_shorten_each_body_and_keep_true_ids_versions_and_lengths(self):
        target = post(7, parent=6, root=1, text='甲' * 20000)
        background = {6: post(6, parent=1, root=1, text='乙' * 25000), 1: post(1, text='丙' * 30000)}
        before = copy.deepcopy((target, background))
        rows, meta = pack([target], self.loader(background), max_context=2500)
        self.assert_bounded(rows, meta, budget=2500)
        self.assertEqual({row['post_id'] for row in rows}, {1, 6, 7})
        for row in rows:
            self.assertTrue(row['text_truncated'])
            self.assertEqual(row['coverage'], 'excerpt')
            self.assertGreaterEqual(len(row['text']), 80)
            self.assertEqual(row['version'], 'v' + str(row['post_id']))
        self.assertEqual(next(row for row in rows if row['post_id'] == 7)['text_length'], 20000)
        self.assertEqual((target, background), before)

    def test_no_room_for_context_keeps_explicit_incomplete_target(self):
        target = post(7, parent=6, root=1)
        rows, meta = pack([target], self.loader({6: post(6, parent=1, root=1), 1: post(1)}), max_sources=1)
        self.assert_bounded(rows, meta, count=1)
        self.assertEqual(rows[0]['id'], target['id'])
        self.assertFalse(rows[0]['context_only'])
        self.assertEqual({item['reason'] for item in rows[0]['thread_context']['missing']}, {'budget'})
        self.assertEqual(meta['included_count'], 1)

    def test_target_used_as_context_keeps_its_target_identity_and_own_thread(self):
        target = post(7, parent=6, root=1)
        selected_parent = post(6, parent=1, root=1, origin='recent')
        rows, meta = pack([target, selected_parent], self.loader({1: post(1)}))
        self.assertEqual(self.calls, [[1]])
        self.assertEqual(meta['included_count'], 2)
        self.assertEqual(meta['background_count'], 1)
        parent = next(row for row in rows if row['post_id'] == 6)
        self.assertFalse(parent['context_only'])
        self.assertEqual(parent['context_role'], 'target')
        self.assertEqual(parent['origin'], 'recent')
        self.assertTrue(parent['thread_context']['complete'])

    def test_target_already_used_as_background_can_fill_its_parent_in_spare_slot(self):
        first = post(7, parent=6)
        second = post(6, parent=5)
        rows, meta = pack([first, second], self.loader({5: post(5, parent=2, root=2), 2: post(2)}),
                          max_sources=3)
        self.assert_bounded(rows, meta, count=3)
        self.assertIn(5, {row['post_id'] for row in rows})
        second = next(row for row in rows if row['post_id'] == 6)
        self.assertFalse(second['context_only'])
        self.assertEqual(second['thread_context']['missing'], [{'post_id': 2, 'role': 'root', 'reason': 'budget'}])

    def test_parent_without_original_or_root_evidence_does_not_imply_complete_root(self):
        uncertain_parent = {'id': 'post:6', 'post_id': 6, 'kind': 'post', 'text': 'A fragment.', 'version': 'v6'}
        rows, _ = pack([post(7, parent=6)], self.loader({6: uncertain_parent}))
        self.assertFalse(rows[0]['thread_context']['complete'])
        self.assertEqual(rows[0]['thread_context']['missing'], [{'post_id': None, 'role': 'root', 'reason': 'unknown_id'}])

    def test_loaded_title_only_parent_does_not_claim_complete_discussion(self):
        rows, meta = pack([post(7, parent=6, root=6)], self.loader({6: post(6, text='', coverage='title_only')}))
        self.assertEqual({row['post_id'] for row in rows}, {6, 7})
        self.assertFalse(rows[0]['thread_context']['complete'])
        self.assertEqual(rows[0]['thread_context']['missing'], [
            {'post_id': 6, 'role': 'parent/root', 'reason': 'text_unavailable'}])
        self.assertEqual(meta['missing_count'], 1)

    def test_news_and_original_posts_do_not_trigger_context_fetches(self):
        news = {'id': 'news:site/article', 'kind': 'news', 'title': 'Article', 'text': 'Only known text.',
                'coverage': 'excerpt', 'text_truncated': True, 'text_length': 500, 'version': 'article-v1'}
        rows, meta = pack([news, post(8)], self.loader({}))
        self.assertEqual(self.calls, [])
        self.assertEqual(meta['included_count'], 2)
        self.assertEqual(rows[0]['version'], 'article-v1')
        self.assertTrue(rows[0]['text_truncated'])
        self.assertEqual(rows[0]['coverage'], 'excerpt')
        self.assertNotIn('post_id', rows[0])

    def test_exact_budget_and_smaller_budget_are_strict(self):
        source = post(7, text='x' * 200)
        full, _ = pack([source], self.loader({}))
        exact = len(json.dumps(full, ensure_ascii=False))
        rows, meta = pack([source], self.loader({}), max_context=exact)
        self.assertEqual(rows, full)
        self.assert_bounded(rows, meta, budget=exact)
        shorter, meta = pack([source], self.loader({}), max_context=exact - 1)
        self.assert_bounded(shorter, meta, budget=exact - 1)
        self.assertLess(len(shorter[0]['text']), len(source['text']))
        self.assertTrue(shorter[0]['text_truncated'])
        rows, meta = pack([source], self.loader({}), max_context=2)
        self.assertEqual(rows, [])
        self.assertEqual(meta['deferred_count'], 1)
        rows, meta = pack([source], self.loader({}), max_sources=0)
        self.assertEqual(rows, [])
        self.assertEqual(self.calls, [])

    def test_load_errors_and_unrelated_returned_posts_never_become_background(self):
        def failed(_):
            raise RuntimeError('not accessible')
        rows, _ = pack([post(7, parent=6, root=1)], failed)
        self.assertEqual({item['reason'] for item in rows[0]['thread_context']['missing']}, {'load_failed'})
        rows, _ = pack([post(7, parent=6, root=1)], lambda _: [post(999)])
        self.assertEqual([row['post_id'] for row in rows], [7])

    def test_conflicting_root_and_self_references_are_explicitly_incomplete(self):
        rows, _ = pack([post(7, parent=6, root=1)], self.loader({6: post(6, parent=2, root=2), 1: post(1)}))
        self.assertFalse(rows[0]['thread_context']['complete'])
        self.assertIn('conflicting_root_ids', {item['reason'] for item in rows[0]['thread_context']['missing']})
        rows, _ = pack([post(7, parent=7, root=7)], self.loader({}))
        self.assertEqual(self.calls, [])
        self.assertFalse(rows[0]['thread_context']['complete'])
        self.assertEqual({item['reason'] for item in rows[0]['thread_context']['missing']}, {'self_reference'})

    def test_duplicate_selected_source_merges_notices_without_mutating_input(self):
        source = post(7, notification_ids=[1])
        duplicate = {**source, 'notification_ids': [2]}
        before = copy.deepcopy((source, duplicate))
        rows, meta = pack([source, duplicate], self.loader({}))
        self.assertEqual(meta['requested_count'], 1)
        self.assertEqual(meta['duplicate_count'], 1)
        self.assertEqual(rows[0]['notification_ids'], [1, 2])
        self.assertEqual((source, duplicate), before)

    def test_hundred_reply_targets_still_respect_both_limits(self):
        targets = [post(value, parent=1000 + value, root=1, text='z' * 2000) for value in range(10, 110)]
        background = {1000 + value: post(1000 + value, parent=1, root=1) for value in range(10, 110)}
        background[1] = post(1)
        rows, meta = pack(targets, self.loader(background))
        self.assert_bounded(rows, meta)
        self.assertEqual(meta['included_count'] + meta['deferred_count'], 100)
        self.assertEqual(len(self.calls), 2)

    def test_invalid_budgets_are_rejected(self):
        for kwargs in ({'max_context': 1}, {'max_context': True}, {'max_sources': -1}, {'max_sources': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                pack([], lambda _: [], **kwargs)


if __name__ == '__main__':
    unittest.main()
