"""Scheduler work that holds the agent-control lock less (24 Oct: lock held 87% of the time, p90 4.3 s)."""
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ctn_data import agent_control, agent_observation
from ctn_data.agent_control import AgentController
from ctn_data.db import connect


class LockLoadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.now = [1792450000.0]
        self.controller = AgentController(
            Path(temporary.name) / 'test.db', Path(temporary.name) / 'secrets',
            provider_store=SimpleNamespace(get=lambda _: {'id': 'fake', 'protocol': 'openai_chat', 'model': 'fake'}),
            clock=lambda: self.now[0])
        self.addCleanup(self.controller.stop)
        self.controller.save({'name': 'Maya', 'username': 'maya_test', 'provider_id': 'fake',
                              'stance': 'Supports Yes.', 'voice': 'Plain.'})
        self.agent_id = self.controller.snapshot()['agents'][0]['id']
        self.controller.store.change(lambda data, conn: data['agents'][self.agent_id].update(
            account_id=5555, account_state='ready', mode='auto', enabled=True))
        self.controller.store.change(lambda data, conn: data['settings'].update(enabled=True))

    def counted(self, name):
        """Patch store.<name> to count its calls (and keep working)."""
        real = getattr(self.controller.store, name)
        calls = []
        patcher = patch.object(self.controller.store, name, side_effect=lambda *a, **k: (calls.append(a), real(*a, **k))[1])
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def competition(self):
        return self.controller.store.read_budget()['rate'].get('competition', {})

    # C: competition sync
    def test_off_mode_runs_no_store_wide_transaction_and_writes_readiness_only_when_it_changes(self):
        changes, budget = self.counted('change'), self.counted('change_budget')
        self.controller._competition_tick()
        self.assertEqual(changes, [])
        self.assertEqual(len(budget), 1)
        self.assertIn('5555', self.competition()['profiles'])
        self.now[0] += 31
        self.controller._competition_tick()
        self.assertEqual((len(changes), len(budget)), (0, 1))

    # D: competition data the hot row need not carry
    def test_off_mode_moves_old_estimates_once_and_then_leaves_the_rate_rows_alone(self):
        self.controller.store.change_budget(lambda data, conn: data['rate'].setdefault('competition', {}).update(
            posts=[{'post_id': 1}], pending_samples=[{'post_id': 1}]))
        self.controller._competition_tick()
        state = self.competition()
        self.assertEqual((state['posts'], state['pending_samples']), ([], []))
        budget, changes = self.counted('change_budget'), self.counted('change')
        self.now[0] += 31
        self.controller._competition_tick()
        self.assertEqual((budget, changes), ([], []))
        self.assertEqual(self.controller.competition()['posts'], [{'post_id': 1}])  # the page reads the cold row

    def test_other_modes_keep_the_store_pass_with_receipts_prefetched(self):
        self.controller.store.change_budget(lambda data, conn: data['rate'].setdefault('competition', {}).setdefault(
            'settings', {}).update(mode='observe'))
        changes = self.counted('change')
        with patch('ctn_data.agent_competition.prefetch_receipts', return_value={}) as prefetch, \
                patch('ctn_data.agent_competition.prepare_refresh', return_value=None):
            self.controller._competition_tick()
        prefetch.assert_called_once()
        self.assertEqual(len(changes), 2)  # sync, then maintain

    # A: tick
    def quiet(self):
        """The role is not due: no decision before next_run_at, no notification refresh."""
        self.controller.store.change(lambda data, conn: (
            data['agents'][self.agent_id].update(next_run_at=self.now[0] + 600),
            data['memory'].setdefault(self.agent_id, {}).update(observation={'next_refresh_at': self.now[0] + 600})))

    def test_an_idle_tick_reads_the_store_once_and_says_it_did_nothing(self):
        self.quiet()
        reads, budget = self.counted('read'), self.counted('change_budget')
        with patch.object(self.controller, '_competition_tick'), \
                patch('ctn_data.agent_observation.pending_revision', return_value=None):
            self.assertFalse(self.controller.tick())
        self.assertEqual(len(reads), 1)
        self.assertEqual(budget, [])

    def test_a_tick_that_polls_and_schedules_writes_both_cursors_once_in_their_own_row(self):
        self.controller.store.change(lambda data, conn: data['agents'][self.agent_id].update(next_run_at=0))
        reads, budget, cursors = self.counted('read'), self.counted('change_budget'), self.counted('save_cursors')
        with patch.object(self.controller, '_competition_tick'), \
                patch('ctn_data.agent_observation.refresh'), patch('ctn_data.agent_observation.drain_resolutions'), \
                patch('ctn_data.agent_observation.pending_revision', return_value=None), \
                patch('ctn_data.agent_schedule.gate', return_value=False), \
                patch.object(self.controller.generations, 'submit') as submit:
            self.assertTrue(self.controller.tick())
        submit.assert_called_once_with(self.agent_id, automatic=True)
        both = {'observation_cursor': self.agent_id, 'scheduler_cursor': self.agent_id}
        self.assertEqual(cursors, [(both,)])
        self.assertEqual(budget, [])  # the 1.5 MB rate rows are not rewritten for a cursor
        self.assertEqual(self.controller.store.read_cursors(), both)
        self.assertNotIn('scheduler_cursor', self.controller.store.read_budget()['rate'])
        self.assertEqual(len(reads), 2)  # polling changed the role, so the scheduler reads again

    def test_cursors_from_before_the_move_still_set_the_rotation(self):
        self.controller.save({'name': 'Ethan', 'username': 'ethan_test', 'provider_id': 'fake',
                              'stance': 'Supports Yes.', 'voice': 'Plain.'})
        ethan = next(a['id'] for a in self.controller.snapshot()['agents'] if a['name'] == 'Ethan')
        later = {'next_refresh_at': self.now[0] + 600}
        self.controller.store.change(lambda data, conn: (
            data['agents'][ethan].update(account_id=5556, account_state='ready', mode='auto', enabled=True),
            [data['memory'].setdefault(aid, {}).update(observation=dict(later)) for aid in (self.agent_id, ethan)],
            data['rate'].update(scheduler_cursor=self.agent_id)))
        # Both roles are due; only the old cursor (Maya served last) puts Ethan first.
        with patch.object(self.controller, '_competition_tick'), \
                patch('ctn_data.agent_observation.pending_revision', return_value=None), \
                patch('ctn_data.agent_schedule.gate', return_value=False), \
                patch.object(self.controller.generations, 'submit') as submit:
            self.controller.tick()
        submit.assert_called_once_with(ethan, automatic=True)

    # The local window every idle role's pending_revision reads: once per tick, never kept past it
    def posts(self, *ids):
        from ctn_data.ingest import save_posts
        stamp = agent_observation._at(self.now[0] - 60)
        conn = connect(self.controller.db_path)
        try:
            save_posts(conn, [{'post_id': i, 'author_id': 900 + i, 'kind': 'post', 'body': f'Citizen post {i}',
                               'created_at': stamp} for i in ids], 'fixture', stamp)
        finally:
            conn.close()

    def second_role(self):
        self.controller.save({'name': 'Ethan', 'username': 'ethan_test', 'provider_id': 'fake',
                              'stance': 'Supports Yes.', 'voice': 'Plain.'})
        ethan = next(a['id'] for a in self.controller.snapshot()['agents'] if a['name'] == 'Ethan')
        self.controller.store.change(lambda data, conn: data['agents'][ethan].update(
            account_id=5556, account_state='ready', mode='auto', enabled=True))
        return ethan

    def window_queries(self):
        """Patch agent_observation.connect so the three local-window queries are counted."""
        queries, real = [], agent_observation.connect

        class Counting:
            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, *args):
                if 'ORDER BY COALESCE(ctn_timestamp(p.edited_at)' in sql or 'FROM news_articles n' in sql:
                    queries.append(sql)
                return self.conn.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.conn, name)
        patcher = patch('ctn_data.agent_observation.connect', side_effect=lambda path: Counting(real(path)))
        patcher.start()
        self.addCleanup(patcher.stop)
        return queries

    def revisions(self, data, roles):
        return [agent_observation.pending_revision(self.controller, data['agents'][aid],
                                                   data['memory'].get(aid, {}), data) for aid in roles]

    def in_one_tick(self):
        self.controller._tick_local = {'thread': threading.get_ident(), 'now': self.now[0], 'rows': {}, 'sources': {}}
        self.addCleanup(setattr, self.controller, '_tick_local', None)

    def test_one_tick_builds_post_sources_once_and_each_role_gets_its_own_copies(self):
        roles = [self.agent_id, self.second_role()]
        self.posts(1, 2, 3)
        data = self.controller.store.read()

        def local(aid):
            return agent_observation._local(self.controller, data['agents'][aid], data['memory'].get(aid, {}))[0]
        plain = [local(aid) for aid in roles]
        self.in_one_tick()
        with patch('ctn_data.agent_observation._post', wraps=agent_observation._post) as built:
            shared = [local(aid) for aid in roles]
        self.assertEqual(shared, plain)
        self.assertEqual(built.call_count, 3)  # three posts, converted once for both roles (not six times)
        shared[0][0]['title'] = 'changed by one role'
        self.assertNotEqual(shared[1][0]['title'], 'changed by one role')
        self.assertEqual(local(roles[1]), plain[1])  # a later role in the same tick still gets the original

    def test_one_tick_queries_the_local_window_once_and_every_revision_is_unchanged(self):
        roles = [self.agent_id, self.second_role()]
        self.posts(1, 2, 3)
        data = self.controller.store.read()
        queries = self.window_queries()
        plain = self.revisions(data, roles)
        self.assertEqual(len(queries), 6)  # before: three queries for each role
        self.assertTrue(all(plain))
        queries.clear()
        self.in_one_tick()
        self.assertEqual(self.revisions(data, roles), plain)
        self.assertEqual(len(queries), 3)  # posts, news-account posts, news articles: once for both roles

    def test_each_tick_queries_again_and_sees_a_new_post_at_once(self):
        self.quiet()
        self.posts(1)
        seen, real = [], agent_observation.pending_revision

        def record(*args, **kwargs):
            seen.append(real(*args, **kwargs))
            return seen[-1]
        with patch.object(self.controller, '_competition_tick'), \
                patch('ctn_data.agent_observation.pending_revision', side_effect=record), \
                patch('ctn_data.agent_schedule.gate', return_value=False), \
                patch.object(self.controller.generations, 'submit'):
            self.controller.tick()
            self.posts(2)
            self.controller.tick()
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1])  # the second tick queried again and saw post 2
        self.assertIsNone(self.controller._tick_local)

    def test_a_new_notice_in_the_inbox_changes_the_revision_within_the_same_tick(self):
        self.posts(1)
        self.in_one_tick()
        before = self.revisions(self.controller.store.read(), [self.agent_id])
        self.controller.store.change(lambda data, conn: data['memory'].setdefault(self.agent_id, {}).update(
            inbox=[{'id': 77, 'post_id': 1, 'status': 'unresolved'}]))
        after = self.revisions(self.controller.store.read(), [self.agent_id])
        self.assertNotEqual(before, after)

    def test_other_threads_never_use_the_tick_rows(self):
        self.in_one_tick()
        seen = []
        thread = threading.Thread(target=lambda: seen.append(agent_observation._tick_rows(self.controller)))
        thread.start()
        thread.join()
        self.assertEqual(seen, [None])
        self.assertIsNotNone(agent_observation._tick_rows(self.controller))

    def test_the_loop_waits_longer_after_an_idle_tick(self):
        self.assertEqual((agent_control.SCHEDULER_BUSY_SECONDS, agent_control.SCHEDULER_IDLE_SECONDS), (2, 5))
        waits = []
        results = iter([True, False])

        def tick():
            value = next(results, None)
            if value is None:
                self.controller._stop.set()
                return False
            return value
        with patch.object(self.controller, 'tick', side_effect=tick), \
                patch.object(self.controller._stop, 'wait', side_effect=lambda seconds: waits.append(seconds)), \
                patch('ctn_data.fact_extraction.start'), patch('ctn_data.post_attribution.start'):
            self.controller.start()
            self.controller._thread.join(5)
        self.assertEqual(waits[:2], [2, 5])


if __name__ == '__main__':
    unittest.main()
