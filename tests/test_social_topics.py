"""Public topic persistence and news-feed coverage without network access."""
import json
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from ctn_data.collector import collect_once, collect_stage
from ctn_data.scheduler import CollectionScheduler
from ctn_data.social_topics import (ALL_KEY, NEWS_USERNAMES, POPULAR_KEY, _account_key,
    _all_tags, _get, _news_account, _popular, collect_social_context,
    read_social_context, topical_context)
from ctn_data.transport import BudgetExceeded, ReadOnlyTransport, RequestDeferred
from tests.test_collection import BaseDatabase, FakeClock, Opener, Response


def post(post_id, author=1):
    return {'post_id': post_id, 'author_id': author, 'kind': 'post',
            'body': 'News #' + str(post_id), 'visibility': 'public',
            'created_at': f'2026-10-20T00:{post_id:02d}:00Z'}


class PublicTransport:
    def __init__(self, conn=None, **kwargs):
        self.attempts = 0
        self.waited = 0
        self.request_budget = kwargs.get('request_budget', 160)
        self.routes = {}
        self.calls = []

    def get_json(self, service, path, **kwargs):
        if self.attempts >= self.request_budget:
            raise BudgetExceeded('test request budget exhausted')
        self.attempts += 1
        self.calls.append(path)
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        route = (parsed.path, query.get('cursor', [None])[0], query.get('q', [None])[0])
        value = self.routes.get(route)
        if isinstance(value, BaseException):
            raise value
        if value is not None:
            return value
        if parsed.path == '/api/accounts':
            username = query['q'][0]
            return {'items': [{'account_id': NEWS_USERNAMES.index(username) + 1,
                              'username': username, 'verified': True}], 'next_cursor': None}
        return {'items': [], 'next_cursor': None}

    def page(self, path, items, cursor=None, next_cursor=None, q=None):
        self.routes[(path, cursor, q)] = {'items': items, 'next_cursor': next_cursor}


class SocialTopicsTests(BaseDatabase):
    def setUp(self):
        super().setUp()
        self.transport = PublicTransport()
        self.result = {'errors': [], 'accounts': 0}

    def test_empty_read_model_has_no_fabricated_observation(self):
        result = read_social_context(self.conn)
        self.assertEqual(result['items'], [])
        self.assertIsNone(result['observed_at'])
        self.assertIsNone(result['all_tags']['completed_at'])
        self.assertEqual(len(result['news_accounts']), 2)
        self.assertIsNone(result['news_accounts'][0]['account_id'])

    def test_tag_cursor_survives_reopen_and_finishes_complete_catalogue(self):
        self.transport.page('/api/tags', [{'tag_id': 2, 'name': 'zebra'}], next_cursor='older')
        _all_tags(self.conn, self.transport, self.result, 50)
        first = read_social_context(self.conn)
        self.assertEqual(first['all_tags']['cursor'], 'older')
        self.assertIsNone(first['all_tags']['completed_at'])
        self.conn.close()
        from ctn_data.db import connect
        self.conn = connect(self.path)
        self.transport.page('/api/tags', [{'tag_id': 1, 'name': 'alpha'}], cursor='older')
        _all_tags(self.conn, self.transport, self.result, 50)
        final = read_social_context(self.conn)
        self.assertEqual([row['name'] for row in final['items']], ['alpha', 'zebra'])
        self.assertIsNone(final['all_tags']['cursor'])
        self.assertTrue(final['all_tags']['completed_at'])
        self.assertEqual(read_social_context(self.conn, q='#ALP', limit=1)['total'], 1)
        self.assertEqual(read_social_context(self.conn, limit=1, offset=1)['items'][0]['name'], 'zebra')

    def test_completed_next_cycle_removes_absent_tags_without_erasing_partial_catalog(self):
        self.transport.page('/api/tags', [{'tag_id': 1, 'name': 'old'}, {'tag_id': 2, 'name': 'keep'}])
        _all_tags(self.conn, self.transport, self.result, 50)
        self.transport.page('/api/tags', [{'tag_id': 2, 'name': 'keep'}], next_cursor='next')
        _all_tags(self.conn, self.transport, self.result, 50)
        self.assertEqual(read_social_context(self.conn)['total'], 2)
        self.transport.page('/api/tags', [], cursor='next')
        _all_tags(self.conn, self.transport, self.result, 50)
        self.assertEqual([item['name'] for item in read_social_context(self.conn)['items']], ['keep'])

    def test_popular_is_independent_ranked_observation_and_empty_clears_it(self):
        self.transport.page('/api/tags/popular', [{'tag_id': 9, 'name': 'news', 'post_count': 82}])
        _popular(self.conn, self.transport, self.result)
        result = read_social_context(self.conn, popular=True)
        self.assertEqual((result['items'][0]['popular_rank'], result['items'][0]['post_count']), (1, 82))
        self.assertEqual(result['popular_items'], result['items'])
        self.assertIsNone(result['all_tags']['completed_at'])
        self.assertEqual(topical_context(self.conn)['tags'][0]['name'], 'news')
        self.transport.page('/api/tags/popular', [])
        _popular(self.conn, self.transport, self.result)
        self.assertEqual(read_social_context(self.conn, popular=True)['items'], [])
        self.assertTrue(read_social_context(self.conn)['popular_tags']['observed_at'])

    def test_catalogue_count_carries_its_own_popular_observation_time(self):
        self.transport.page('/api/tags', [{'tag_id': 1, 'name': 'topic'}])
        with patch('ctn_data.social_topics.utc_now', return_value='2026-10-20T01:00:00Z'):
            _all_tags(self.conn, self.transport, self.result, 50)
        self.transport.page('/api/tags/popular', [{'tag_id': 1, 'name': 'topic', 'post_count': 4}])
        with patch('ctn_data.social_topics.utc_now', return_value='2026-10-20T02:00:00Z'):
            _popular(self.conn, self.transport, self.result)
        row = read_social_context(self.conn)['items'][0]
        self.assertEqual(row['post_count'], 4)
        self.assertEqual(row['observed_at'], '2026-10-20T01:00:00Z')
        self.assertEqual(row['popular_observed_at'], '2026-10-20T02:00:00Z')

    def test_invalid_tag_page_cannot_partially_replace_last_good_state(self):
        self.transport.page('/api/tags/popular', [{'tag_id': 1, 'name': 'valid', 'post_count': 10}])
        _popular(self.conn, self.transport, self.result)
        before = _get(self.conn, 'meta', POPULAR_KEY)
        self.transport.page('/api/tags/popular', [{'tag_id': 2, 'name': 'bad', 'post_count': -1}])
        with self.assertRaises(ValueError):
            _popular(self.conn, self.transport, self.result)
        self.assertEqual(_get(self.conn, 'meta', POPULAR_KEY), before)

    def test_repeated_cursor_rejected_without_advancing_checkpoint(self):
        self.transport.page('/api/tags', [{'tag_id': 1, 'name': 'one'}], next_cursor='same')
        _all_tags(self.conn, self.transport, self.result, 50)
        before = _get(self.conn, 'checkpoints', ALL_KEY)
        self.transport.page('/api/tags', [{'tag_id': 2, 'name': 'two'}], cursor='same', next_cursor='same')
        with self.assertRaises(ValueError):
            _all_tags(self.conn, self.transport, self.result, 50)
        self.assertEqual(_get(self.conn, 'checkpoints', ALL_KEY), before)

    def test_news_discovery_requires_exact_username_and_validates_post_author(self):
        self.transport.page('/api/accounts', [{'account_id': 99, 'username': 'DailyKingstoniaFan'},
            {'account_id': 1, 'username': 'DailyKingstonia', 'verified': True}], q='DailyKingstonia')
        self.transport.page('/api/accounts/1/posts', [post(1, author=99)])
        with self.assertRaises(ValueError):
            _news_account(self.conn, self.transport, self.result, 'DailyKingstonia', 50)
        self.assertEqual(self.conn.execute('SELECT username FROM accounts').fetchone()[0], 'DailyKingstonia')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 0)

    def test_news_history_and_multi_page_live_gap_progress_independently(self):
        self.transport.page('/api/accounts/1/posts', [post(5)], next_cursor='history')
        _news_account(self.conn, self.transport, self.result, 'DailyKingstonia', 50)
        self.transport.page('/api/accounts/1/posts', [post(10)], next_cursor='gap')
        self.transport.page('/api/accounts/1/posts', [post(4)], cursor='history', next_cursor='older')
        _news_account(self.conn, self.transport, self.result, 'DailyKingstonia', 50)
        state = _get(self.conn, 'checkpoints', _account_key('DailyKingstonia'))
        self.assertEqual((state['live_cursor'], state['cursor']), ('gap', 'older'))
        self.transport.page('/api/accounts/1/posts', [post(11)], next_cursor='newhead')
        self.transport.page('/api/accounts/1/posts', [post(9), post(8)], cursor='gap', next_cursor='gap2')
        self.transport.page('/api/accounts/1/posts', [post(3), post(2), post(1)], cursor='older')
        _news_account(self.conn, self.transport, self.result, 'DailyKingstonia', 50)
        self.transport.page('/api/accounts/1/posts', [post(7), post(6), post(5)], cursor='gap2', next_cursor='unused')
        _news_account(self.conn, self.transport, self.result, 'DailyKingstonia', 50)
        final = _get(self.conn, 'checkpoints', _account_key('DailyKingstonia'))
        self.assertIsNone(final['live_cursor'])
        self.assertEqual(final['watermark'][1], 10)
        self.assertTrue(final['completed_at'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 11)
        self.assertEqual(sum('/api/accounts?' in path for path in self.transport.calls), 1)

    def test_news_history_cross_page_cursor_loop_preserves_last_good_page(self):
        self.transport.page('/api/accounts/1/posts', [post(5)], next_cursor='A')
        _news_account(self.conn, self.transport, self.result, 'DailyKingstonia', 50)
        self.transport.page('/api/accounts/1/posts', [post(4)], cursor='A', next_cursor='B')
        _news_account(self.conn, self.transport, self.result, 'DailyKingstonia', 50)
        self.transport.page('/api/accounts/1/posts', [post(3)], cursor='B', next_cursor='A')
        with self.assertRaises(ValueError):
            _news_account(self.conn, self.transport, self.result, 'DailyKingstonia', 50)
        state = _get(self.conn, 'checkpoints', _account_key('DailyKingstonia'))
        self.assertEqual(state['cursor'], 'B')
        self.assertEqual(state['history_visited_cursors'], ['A'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 2)

    def test_news_live_cross_page_cursor_loop_preserves_last_good_page(self):
        self.transport.page('/api/accounts/1/posts', [post(5)])
        _news_account(self.conn, self.transport, self.result, 'DailyKingstonia', 50)
        self.transport.page('/api/accounts/1/posts', [post(10)], next_cursor='A')
        _news_account(self.conn, self.transport, self.result, 'DailyKingstonia', 50)
        self.transport.page('/api/accounts/1/posts', [post(9)], cursor='A', next_cursor='B')
        _news_account(self.conn, self.transport, self.result, 'DailyKingstonia', 50)
        self.transport.page('/api/accounts/1/posts', [post(8)], cursor='B', next_cursor='A')
        with self.assertRaises(ValueError):
            _news_account(self.conn, self.transport, self.result, 'DailyKingstonia', 50)
        state = _get(self.conn, 'checkpoints', _account_key('DailyKingstonia'))
        self.assertEqual(state['live_cursor'], 'B')
        self.assertEqual(state['live_visited_cursors'], ['A'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 3)

    def test_popular_cooldown_does_not_block_all_tags_or_news(self):
        self.transport.routes[('/api/tags/popular', None, None)] = RequestDeferred('shared cooldown', status=429)
        self.transport.page('/api/tags', [{'tag_id': 1, 'name': 'usable'}])
        self.transport.page('/api/accounts/1/posts', [post(1)])
        collect_social_context(self.conn, self.transport, {'page_limit': 50}, self.result)
        self.assertEqual(read_social_context(self.conn)['total'], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 1)
        self.assertEqual(self.result['errors'][0]['stage'], 'popular_tags')
        self.assertEqual(self.result['accounts'], 2)

    def test_real_transport_saved_popular_cooldown_does_not_exhaust_other_routes(self):
        clock = FakeClock()
        origin = 'https://social.legitreal.com'
        responses = [
            Response({'items': [{'tag_id': 1, 'name': 'usable'}], 'next_cursor': None}, origin + '/api/tags'),
            Response({'items': [{'account_id': 1, 'username': 'DailyKingstonia', 'verified': True}],
                      'next_cursor': None}, origin + '/api/accounts?q=DailyKingstonia&limit=50'),
            Response({'items': [post(1)], 'next_cursor': None}, origin + '/api/accounts/1/posts'),
            Response({'items': [{'account_id': 2, 'username': 'KingstoniaHerald', 'verified': True}],
                      'next_cursor': None}, origin + '/api/accounts?q=KingstoniaHerald&limit=50'),
            Response({'items': [post(2, author=2)], 'next_cursor': None}, origin + '/api/accounts/2/posts'),
        ]
        opener = Opener(responses)
        transport = ReadOnlyTransport(self.conn, opener=opener, sleeper=clock.sleep, clock=clock,
            max_retries=0, max_wait_seconds=.4, request_budget=8, deadline_seconds=12)
        cooldown = {'not_before_epoch': transport.epoch_base + 120}
        with self.conn:
            self.conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)',
                ('throttle:social:/api/tags/popular', json.dumps(cooldown), '2026-10-20T00:00:00Z'))
        collect_social_context(self.conn, transport, {'page_limit': 50}, self.result)
        self.assertEqual(transport.attempts, 5)
        self.assertEqual(clock.waits, [])
        self.assertEqual(len(opener.calls), 5)
        self.assertEqual([urlsplit(call[0].full_url).path for call in opener.calls],
            ['/api/tags', '/api/accounts', '/api/accounts/1/posts', '/api/accounts', '/api/accounts/2/posts'])
        self.assertEqual(read_social_context(self.conn)['total'], 1)
        self.assertEqual([item['posts_collected'] for item in read_social_context(self.conn)['news_accounts']], [1, 1])
        self.assertEqual(self.result['errors'][0]['stage'], 'popular_tags')
        self.assertTrue(self.result['errors'][0]['deferred'])
        self.assertEqual(_get(self.conn, 'checkpoints', 'throttle:social:/api/tags/popular'), cooldown)

    def test_real_transport_actual_deadline_exhaustion_stops_remaining_routes(self):
        clock, opener = FakeClock(), Opener([])
        transport = ReadOnlyTransport(self.conn, opener=opener, sleeper=clock.sleep, clock=clock,
            max_retries=0, max_wait_seconds=.4, request_budget=8, deadline_seconds=12)
        clock.now = transport.deadline
        with self.assertRaises(BudgetExceeded):
            collect_social_context(self.conn, transport, {'page_limit': 50}, self.result)
        self.assertEqual(opener.calls, [])
        self.assertEqual(_get(self.conn, 'checkpoints', ALL_KEY), {})

    def test_stage_uses_public_egress_and_bounded_budget(self):
        transport = PublicTransport(request_budget=8)
        transport.page('/api/tags', [{'tag_id': 1, 'name': 'topic'}])
        transport.page('/api/accounts/1/posts', [post(1)])
        route = {'proxy_url': 'http://127.0.0.1:18881', 'actor': 'proxy:test'}
        with patch('ctn_data.egress.choose_public_egress', return_value=route), \
             patch('ctn_data.collector.ReadOnlyTransport', return_value=transport) as factory:
            result = collect_stage(self.path, {}, 'social_context')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['posts'], 1)
        self.assertEqual(factory.call_args.kwargs['request_budget'], 8)
        self.assertEqual(factory.call_args.kwargs['actor'], 'proxy:test')
        json.dumps(result)

    def test_budget_exhaustion_is_deferred_and_retains_completed_tag_page(self):
        transport = PublicTransport(request_budget=2)
        transport.page('/api/tags', [{'tag_id': 1, 'name': 'one'}], next_cursor='later')
        with patch('ctn_data.egress.choose_public_egress', return_value={}), \
             patch('ctn_data.collector.ReadOnlyTransport', return_value=transport):
            result = collect_stage(self.path, {}, 'social_context')
        self.assertEqual(result['status'], 'deferred')
        self.assertEqual(result['requests'], 2)
        self.assertEqual(read_social_context(self.conn)['all_tags']['cursor'], 'later')
        self.assertEqual(read_social_context(self.conn)['total'], 1)
        self.assertIsNone(read_social_context(self.conn)['all_tags']['completed_at'])

    def test_tight_budget_rotates_first_route_across_all_four_jobs(self):
        first_routes = []
        for index in range(4):
            transport = PublicTransport(request_budget=1)
            with self.assertRaises(BudgetExceeded):
                collect_social_context(self.conn, transport, {'page_limit': 50}, {'errors': []})
            first_routes.append(urlsplit(transport.calls[0]).path)
        self.assertEqual(first_routes, ['/api/tags/popular', '/api/tags', '/api/accounts', '/api/accounts'])
        self.assertEqual([row['account_id'] for row in read_social_context(self.conn)['news_accounts']], [1, 2])

    def test_full_collect_includes_topics_and_serializable_news_post_metrics(self):
        transport = PublicTransport()
        transport.page('/api/accounts/1/posts', [post(1)])
        with patch('ctn_data.collector.ReadOnlyTransport', return_value=transport):
            result = collect_once(self.path, {'collect_news': False, 'collect_trending': False,
                'account_limit': 0, 'counter_limit': 0})
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['posts'], 1)
        self.assertEqual(result['accounts'], 2)
        self.assertEqual(read_social_context(self.conn)['news_accounts'][0]['posts_collected'], 1)
        json.dumps(result)

    def test_scheduler_seeds_independent_three_minute_task(self):
        scheduler = CollectionScheduler(self.path, {})
        scheduler._seed(self.conn)
        row = self.conn.execute("SELECT kind FROM collection_tasks WHERE task_key='periodic:social_context'").fetchone()
        self.assertEqual(row['kind'], 'social_context')
        self.assertEqual(scheduler._period('social_context'), 180)
