"""Recent actor inputs and durable notification acknowledgements, using fakes."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ctn_data.agent_control import AgentController
from ctn_data import agent_observation as observation
from ctn_data.db import connect
from ctn_data.ingest import save_accounts, save_news, save_posts


class FakeSocial:
    def __init__(self):
        self.notices, self.home, self.posts, self.resolved = [], [], {}, []
        self.calls = []
        self.notice_error = self.resolve_error = None
        self.account_id = 71
        self.controller = None

    def _network(self, name):
        # A second writer can obtain a lock during every simulated SDK call.
        conn = connect(self.controller.db_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.rollback()
        finally:
            conn.close()
        self.calls.append(name)

    def factory(self, **kwargs):
        return self

    def notifications(self, **kwargs):
        self._network(('notifications', kwargs))
        if self.notice_error:
            raise self.notice_error
        return self.notices

    def feed_home(self, **kwargs):
        self._network(('home', kwargs))
        return self.home

    def get_post(self, post_id):
        self._network(('get_post', post_id))
        return self.posts[post_id]

    def resolve_notification(self, notification_id):
        self._network(('resolve', notification_id))
        if self.resolve_error:
            raise self.resolve_error
        self.resolved.append(notification_id)


class AgentObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1792450000.0
        self.social = FakeSocial()
        self.controller = AgentController(Path(self.temp.name) / 'test.sqlite3', Path(self.temp.name) / 'secrets',
                                          provider_store=SimpleNamespace(), sdk_factory=self.social.factory, clock=lambda: self.now)
        self.social.controller = self.controller
        self.controller.save({'name': 'Fictional Resident', 'username': 'fictional_yes', 'interests': ['housing']})
        self.agent_id = self.controller.snapshot()['agents'][0]['id']
        patch.object(self.controller, '_account', return_value=self.social).start()
        self.patch_budget = patch.object(observation, '_call', side_effect=lambda controller, bucket, method, *a, **kw: method(*a, **kw))
        self.budget = self.patch_budget.start()
        # These tests exercise collection and bookkeeping. The AI selector and
        # its real integration have their own fake-model regression suites.
        def select(controller, agent, token, candidates):
            ordered = observation._reading_order(candidates)
            return ordered, {'status': 'selected' if ordered else 'empty',
                             'candidate_count': len(candidates), 'selected_count': len(ordered),
                             'cache_hit': False}
        patch('ctn_data.reading_selection.select', side_effect=select).start()
        self.addCleanup(patch.stopall)

    def agent(self):
        return self.controller.store.read()['agents'][self.agent_id]

    def bind(self):
        def update(data, conn):
            data['agents'][self.agent_id].update(account_state='ready', account_id=71)
        self.controller.store.change(update)

    def post(self, post_id, text, age=0, observed_age=0, **extra):
        raw = {'post_id': post_id, 'author_id': 90, 'kind': 'post', 'body': text,
               'visibility': 'public', 'created_at': observation._at(self.now - age), **extra}
        conn = connect(self.controller.db_path)
        try:
            with conn:
                save_posts(conn, [raw], 'trending', observed_at=observation._at(self.now - observed_age))
        finally:
            conn.close()
        return raw

    def news(self, slug, body, *, first_age=0, published='1920-01-01T00:00:00Z'):
        conn = connect(self.controller.db_path)
        try:
            with conn:
                save_news(conn, [{'site': 'fictional', 'slug': slug, 'title': slug, 'body': body, 'published_at': published}],
                          observed_at=observation._at(self.now - first_age))
        finally:
            conn.close()

    def read(self, refresh=False, for_decision=False):
        with self.controller._claim(self.agent_id) as token:
            return observation.observe(self.controller, self.agent(), token, refresh=refresh, for_decision=for_decision)

    def memory(self):
        return self.controller.store.read()['memory'].get(self.agent_id, {})

    def test_recent_posts_do_not_treat_old_refreshed_hot_posts_as_new(self):
        self.post(1, 'Old trending housing', age=86400)
        self.post(2, 'Recent transport discussion', age=60)
        sources = self.read()
        self.assertEqual([source['post_id'] for source in sources], [2])

    def test_news_accounts_keep_a_day_of_posts_and_identify_the_original_source(self):
        conn = connect(self.controller.db_path)
        try:
            with conn:
                save_accounts(conn, [{'account_id': 90, 'username': 'DailyKingstonia', 'private': False}],
                              observed_at=observation._at(self.now))
        finally:
            conn.close()
        self.post(1, 'Housing report from the newspaper.', age=7200)
        self.post(2, 'An older report.', age=90000)
        self.post(3, 'An ordinary older post.', age=7200, author_id=91)
        sources = self.read()
        self.assertEqual([source['post_id'] for source in sources], [1])
        self.assertEqual(sources[0]['source_role'], 'news_account')
        self.assertEqual(sources[0]['news_account'], 'DailyKingstonia')
        self.assertEqual(sources[0]['url'], 'https://social.legitreal.com/posts/1')
        self.assertEqual(self.read(), [])

    def test_busy_recent_feed_leaves_room_for_a_news_source(self):
        conn = connect(self.controller.db_path)
        try:
            with conn:
                save_accounts(conn, [{'account_id': 91, 'username': 'KingstoniaHerald'}],
                              observed_at=observation._at(self.now))
        finally:
            conn.close()
        for index in range(1, 27):
            self.post(index, 'Housing discussion.', age=index)
        self.post(100, 'Housing report.', age=7200, author_id=91)
        self.assertIn('post:100', {source['id'] for source in self.read()})

    def test_busy_inbox_keeps_news_and_interests_in_the_same_context(self):
        self.bind()
        for index in range(1, 24):
            self.post(index, 'A direct question.', age=index)
        self.post(100, 'A public housing discussion.', age=60)
        self.news('housing-report', 'A recent housing article.')
        def inbox(data, conn):
            data['memory'][self.agent_id] = {'inbox': [
                {'id': index, 'post_id': index, 'status': 'unresolved'} for index in range(1, 24)]}
        self.controller.store.change(inbox)
        sources = self.read(for_decision=True)
        self.assertLessEqual(len(sources), 20)
        self.assertEqual(sources[0]['origin'], 'notification')
        self.assertIn('post:100', {source['id'] for source in sources})
        self.assertIn('news:fictional/housing-report', {source['id'] for source in sources})

    def test_article_provenance_preserves_title_only_and_truncation(self):
        self.news('headline', '')
        self.news('full', 'Detailed article text. ' * 100)
        sources = {source['id']: source for source in self.read()}
        headline = sources['news:fictional/headline']
        self.assertEqual(headline['coverage'], 'title_only')
        self.assertFalse(headline['text_truncated'])
        full = sources['news:fictional/full']
        self.assertEqual(full['coverage'], 'excerpt')
        self.assertTrue(full['text_truncated'])
        self.assertEqual(full['url'], 'https://sites.legitreal.com/fictional/articles/full.json')
        self.assertNotIn('post_id', full)

    def test_news_uses_actual_first_observation_not_fictional_publication_date(self):
        self.news('newly-seen', 'A fictional 1920 story just entered the collector.')
        self.news('old-with-future-date', 'Already stale.', first_age=172800, published='2099-01-01T00:00:00Z')
        sources = self.read()
        self.assertEqual([source['id'] for source in sources], ['news:fictional/newly-seen'])
        self.assertEqual(sources[0]['published_at'], '1920-01-01T00:00:00Z')
        self.assertEqual(observation._epoch(sources[0]['freshness_at']), self.now)

    def test_source_versions_deduplicate_and_body_edits_are_read_again(self):
        self.post(4, 'A first housing argument.')
        self.news('versioned', 'Original article text.')
        self.assertEqual(len(self.read()), 2)
        self.assertEqual(self.read(), [])
        self.now += 120
        self.post(4, 'A revised housing argument.', age=120, edited_at=observation._at(self.now))
        self.news('versioned', 'Updated article text.')
        self.assertEqual({source['id'] for source in self.read()}, {'post:4', 'news:fictional/versioned'})
        self.assertEqual(self.read(), [])

    def test_news_last_seen_or_publication_metadata_changes_do_not_reread_content(self):
        self.news('unchanged', 'Same article body.')
        self.assertEqual(len(self.read()), 1)
        self.now += 200
        self.news('unchanged', 'Same article body.', published='2050-01-01T00:00:00Z')
        self.assertEqual(self.read(), [])

    def test_unknown_old_article_with_only_new_publication_metadata_stays_stale(self):
        self.news('stale-metadata', 'Same old article body.', first_age=172800)
        self.news('stale-metadata', 'Same old article body.', published='2099-01-01T00:00:00Z')
        self.assertEqual(self.read(), [])
        self.assertEqual(self.memory()['news_versions']['news:fictional/stale-metadata']['observed_at'],
                         observation._at(self.now - 172800).replace('.000000Z', 'Z'))

    def test_known_article_reverting_to_an_old_body_is_a_new_observed_change(self):
        self.news('reverted', 'The old original body.', first_age=172800)
        self.assertEqual(self.read(), [])
        self.news('reverted', 'An updated body.')
        self.assertEqual(len(self.read()), 1)
        self.now += 61
        self.news('reverted', 'The old original body.')
        sources = self.read()
        self.assertEqual(len(sources), 1)
        self.assertEqual(observation._epoch(sources[0]['freshness_at']), self.now)

    def test_persona_interests_prioritize_equally_fresh_posts(self):
        self.post(1, 'Sport this afternoon.')
        self.post(2, 'Housing costs for local families.')
        self.assertEqual([source['post_id'] for source in self.read()], [2, 1])

    def test_refresh_is_separate_from_read_and_never_resolves_notifications(self):
        self.bind()
        target = self.post(5, 'Please explain your housing view.', age=86400)
        self.social.notices = [{'notification_id': 9, 'type': 'reply', 'post_id': 5, 'post': target}]
        self.social.home = [self.post(6, 'Current housing news.')]
        with self.controller._claim(self.agent_id) as token:
            observation.refresh(self.controller, self.agent(), token)
        self.assertNotIn('read_versions', self.memory())
        self.assertEqual(self.social.resolved, [])
        first = self.read()
        self.assertEqual(first[0]['post_id'], 5)
        self.assertEqual(first[0]['notification_ids'], [9])
        self.assertEqual(self.memory()['inbox'][0]['status'], 'unresolved')
        self.assertEqual(self.read(), [])
        self.assertEqual(self.social.resolved, [])
        self.assertEqual(self.budget.call_args_list[0].args[1], 'notification')
        self.assertEqual(self.budget.call_args_list[1].args[1], 'feed')

    def test_inbox_keeps_first_seen_time_while_observed_at_follows_each_refresh(self):
        self.bind()
        target = self.post(5, 'Please explain your housing view.', age=600)
        self.social.notices = [{'notification_id': 9, 'type': 'reply', 'post_id': 5, 'post': target}]
        with self.controller._claim(self.agent_id) as token:
            observation.refresh(self.controller, self.agent(), token)
        first = observation._at(self.now)
        self.assertEqual(self.memory()['inbox'][0]['first_seen_at'], first)
        self.now += 61
        with self.controller._claim(self.agent_id) as token:
            observation.refresh(self.controller, self.agent(), token)
        notice = self.memory()['inbox'][0]
        self.assertEqual(notice['first_seen_at'], first)
        self.assertEqual(notice['observed_at'], observation._at(self.now))

    def test_persistent_ttl_reuses_home_and_inbox_across_refreshes(self):
        self.bind()
        self.social.home = [self.post(10, 'Housing.')]
        with self.controller._claim(self.agent_id) as token:
            observation.refresh(self.controller, self.agent(), token)
        self.assertEqual(len(self.social.calls), 2)
        self.controller.owner = 'a-different-worker'
        with self.controller._claim(self.agent_id) as token:
            observation.refresh(self.controller, self.agent(), token)
        self.assertEqual(len(self.social.calls), 2)
        self.now += 61
        with self.controller._claim(self.agent_id) as token:
            observation.refresh(self.controller, self.agent(), token)
        self.assertEqual(len(self.social.calls), 4)

    def test_refresh_failure_preserves_inbox_and_redacts_upstream_details(self):
        self.bind()
        self.social.notices = [{'notification_id': 3, 'post_id': 3, 'type': 'mention'}]
        with self.controller._claim(self.agent_id) as token:
            observation.refresh(self.controller, self.agent(), token)
        self.now += 61
        self.social.notice_error = RuntimeError('Bearer SECRET_UPSTREAM_CREDENTIAL')
        with self.controller._claim(self.agent_id) as token:
            observation.refresh(self.controller, self.agent(), token)
        memory = self.memory()
        self.assertEqual(memory['inbox'][0]['id'], 3)
        self.assertEqual(memory['observation']['notifications_status'], 'error')
        self.assertNotIn('SECRET_UPSTREAM_CREDENTIAL', json.dumps(memory))

    def test_notification_target_missing_locally_is_read_and_thread_context_is_added(self):
        self.bind()
        self.post(20, 'Original thread.', age=86400)
        self.social.notices = [{'notification_id': 5, 'post_id': 21, 'type': 'reply'}]
        self.social.posts[21] = {'post_id': 21, 'author_id': 90, 'body': 'A reply to you.', 'kind': 'reply',
                                 'root_post_id': 20, 'reply_to_post_id': 20}
        sources = self.read(refresh=True)
        self.assertEqual(sources[0]['post_id'], 21)
        self.assertIn(('get_post', 21), self.social.calls)
        self.assertIn('thread', [source['origin'] for source in sources])

    def test_new_notification_for_already_read_post_is_delivered_once(self):
        self.bind()
        self.post(30, 'A housing question.')
        self.assertEqual(len(self.read()), 1)
        self.social.notices = [{'notification_id': 8, 'post_id': 30, 'type': 'mention'}]
        sources = self.read(refresh=True)
        self.assertEqual(sources[0]['notification_ids'], [8])
        self.assertEqual(self.read(), [])

    def test_selected_resolution_is_durable_and_individual_and_read_does_not_ack(self):
        self.bind()
        self.social.notices = [{'notification_id': 8, 'post_id': 30, 'type': 'mention'},
                               {'notification_id': 9, 'post_id': 31, 'type': 'reply'}]
        self.post(30, 'A question.')
        self.post(31, 'Another question.')
        self.read(refresh=True)
        self.social.resolve_error = TimeoutError('SECRET_INTERNAL_URL')
        with self.controller._claim(self.agent_id) as token:
            self.assertFalse(observation.resolve_selected(self.controller, self.agent(), token, [8]))
        memory = self.memory()
        self.assertEqual(memory['notification_outbox']['8']['state'], 'pending')
        self.assertEqual({row['id']: row['status'] for row in memory['inbox']}, {8: 'ack_pending', 9: 'unresolved'})
        self.assertNotIn('SECRET_INTERNAL_URL', json.dumps(memory))
        self.now += 10
        self.social.resolve_error = None
        with self.controller._claim(self.agent_id) as token:
            self.assertTrue(observation.resolve_selected(self.controller, self.agent(), token, [8]))
        self.assertEqual(self.social.resolved, [8])
        self.assertEqual(self.memory()['notification_outbox']['8']['state'], 'resolved')
        self.assertEqual(self.read(), [])

    def test_unknown_notification_is_never_resolved(self):
        self.bind()
        with self.controller._claim(self.agent_id) as token:
            self.assertFalse(observation.resolve_selected(self.controller, self.agent(), token, [999]))
        self.assertEqual(self.social.resolved, [])

    def test_context_and_source_count_are_bounded(self):
        for post_id in range(1, 35):
            self.post(post_id, 'A long housing discussion. ' * 200)
        sources = self.read()
        self.assertLessEqual(len(sources), 20)
        self.assertLessEqual(len(json.dumps(sources, ensure_ascii=False)), 12000)
        self.assertGreater(len(sources), 0)

    def test_reading_does_not_consume_decision_and_failed_generation_can_retry(self):
        self.post(70, 'Housing material for the decision.')
        self.assertEqual(len(self.read()), 1)
        self.assertEqual(self.read(), [])
        first = self.read(for_decision=True)
        self.assertEqual(len(first), 1)
        # Merely observing during a failed model attempt has not consumed it.
        self.assertEqual(len(self.read(for_decision=True)), 1)
        def accepted(data, conn):
            data['memory'][self.agent_id]['decision_versions'] = {row['id']: row['version'] for row in first}
        self.controller.store.change(accepted)
        self.assertEqual(self.read(for_decision=True), [])

    def test_deferred_refresh_preserves_existing_inbox_and_local_sources(self):
        from ctn_data.agent_budget import Deferred
        self.bind()
        self.post(90, 'A current housing update.')
        self.social.notices = [{'notification_id': 10, 'post_id': 90, 'type': 'mention'}]
        self.read(refresh=True)
        self.now += 61
        self.social.notice_error = Deferred('notification', 47)
        sources = self.read(refresh=True, for_decision=True)
        self.assertEqual(sources[0]['notification_ids'], [10])
        memory = self.memory()
        self.assertEqual(memory['inbox'][0]['id'], 10)
        self.assertEqual(memory['observation']['notifications_status'], 'deferred')
        self.assertEqual(memory['observation']['notifications_next_at'], self.now + 47)
        self.assertEqual(self.social.resolved, [])

    def test_pending_ack_keeps_its_backoff_and_does_not_ack_unselected_notification(self):
        from ctn_data.agent_budget import Deferred
        self.bind()
        self.post(95, 'One question.')
        self.post(96, 'Another question.')
        self.social.notices = [{'notification_id': 11, 'post_id': 95}, {'notification_id': 12, 'post_id': 96}]
        self.read(refresh=True)
        self.social.resolve_error = Deferred('resolve', 37)
        with self.controller._claim(self.agent_id) as token:
            self.assertFalse(observation.resolve_selected(self.controller, self.agent(), token, [11]))
        initial = len(self.social.calls)
        with self.controller._claim(self.agent_id) as token:
            self.assertFalse(observation.resolve_selected(self.controller, self.agent(), token, [11]))
        self.assertEqual(len(self.social.calls), initial)
        self.assertEqual(self.memory()['notification_outbox']['11']['next_retry_at'], self.now + 37)
        self.assertNotIn('12', self.memory()['notification_outbox'])

    def test_one_post_can_leave_another_notification_for_a_later_decision(self):
        self.bind()
        self.post(98, 'A shared discussion.')
        self.social.notices = [{'notification_id': 21, 'post_id': 98}, {'notification_id': 22, 'post_id': 98}]
        first = self.read(refresh=True, for_decision=True)
        self.assertEqual(first[0]['notification_ids'], [21, 22])
        def decided(data, conn):
            memory = data['memory'][self.agent_id]
            memory['decision_versions'] = {first[0]['id']: first[0]['version']}
            memory['decision_notification_ids'] = [21]
        self.controller.store.change(decided)
        next_sources = self.read(for_decision=True)
        self.assertEqual(next_sources[0]['notification_ids'], [22])

    def test_inbox_history_limit_does_not_drop_a_pending_action_notification(self):
        self.bind()
        def pending(data, conn):
            data['memory'][self.agent_id] = {'inbox': [
                {'id': value, 'notification_id': value, 'post_id': value, 'status': 'unresolved'}
                for value in range(1, 201)]}
            data['drafts']['pending-example'] = {'id': 'pending-example', 'agent_id': self.agent_id,
                'state': 'pending', 'created_at': observation._at(self.now), 'notification_ids': [1]}
        self.controller.store.change(pending)
        self.social.notices = [{'notification_id': value, 'post_id': value} for value in range(201, 251)]
        with self.controller._claim(self.agent_id) as token:
            observation.refresh(self.controller, self.agent(), token)
        ids = {row['id'] for row in self.memory()['inbox']}
        self.assertIn(1, ids)
        self.assertIn(250, ids)
        self.assertEqual(len(ids), 200)

    def test_private_posts_cannot_leak_between_actor_local_collections(self):
        self.post(80, 'Private content.', visibility='private')
        self.post(81, 'Public content.')
        self.assertEqual([source['post_id'] for source in self.read()], [81])


if __name__ == '__main__':
    unittest.main()
